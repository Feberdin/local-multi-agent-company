"""
Purpose: Load and resolve per-worker model routing, fallbacks, and token budgets for local LLM backends.
Input/Output: Workers ask for a route by name and receive a concrete provider, model, and runtime parameters.
Important invariants: Routing stays configurable, provider names must resolve to known endpoints, and safe defaults exist.
How to debug: If a worker hits the wrong model, inspect the resolved worker route and the routing YAML loaded here.
"""

from __future__ import annotations

from pathlib import Path

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel

from services.shared.agentic_lab.config import Settings


class ModelProvider(BaseModel):
    name: str
    base_url: str
    model_name: str
    api_key: str = ""


class WorkerModelRoute(BaseModel):
    primary_provider: str
    fallback_provider: str | None = None
    temperature: float = 0.1
    max_tokens: int = 1800
    budget_tokens: int = 12000
    request_timeout_seconds: float = 90.0
    reasoning: str = "medium"
    purpose: str = ""
    output_contract: str = "text"
    routing_note: str = ""


class ModelRoutingConfig(BaseModel):
    providers: dict[str, ModelProvider]
    workers: dict[str, WorkerModelRoute]


def _route(
    primary_provider: str,
    fallback_provider: str | None,
    *,
    temperature: float,
    max_tokens: int,
    budget_tokens: int,
    request_timeout_seconds: float,
    reasoning: str,
    purpose: str,
    output_contract: str = "text",
    routing_note: str = "",
) -> WorkerModelRoute:
    return WorkerModelRoute(
        primary_provider=primary_provider,
        fallback_provider=fallback_provider,
        temperature=temperature,
        max_tokens=max_tokens,
        budget_tokens=budget_tokens,
        request_timeout_seconds=request_timeout_seconds,
        reasoning=reasoning,
        purpose=purpose,
        output_contract=output_contract,
        routing_note=routing_note,
    )


def _preferred_provider(
    provider_names: set[str],
    *candidates: str | None,
    exclude: set[str] | None = None,
) -> str | None:
    excluded = exclude or set()
    for candidate in candidates:
        if candidate and candidate in provider_names and candidate not in excluded:
            return candidate
    for candidate in sorted(provider_names):
        if candidate not in excluded:
            return candidate
    return None


def _safe_default_primary_provider(settings: Settings, provider_names: set[str]) -> str:
    preferred = settings.default_model_provider if settings.default_model_provider != "qwen" else None
    chosen = _preferred_provider(provider_names, preferred, "mistral", settings.default_model_provider, "qwen")
    if chosen is None:
        raise ValueError("No model provider is configured. At least one provider must be available.")
    return chosen


def _default_worker_routes(settings: Settings, provider_names: set[str]) -> dict[str, WorkerModelRoute]:
    safe_default_provider = _safe_default_primary_provider(settings, provider_names)
    reasoning_provider = _preferred_provider(provider_names, "qwen", safe_default_provider, "mistral")
    structured_provider = _preferred_provider(provider_names, "mistral", safe_default_provider, "qwen")
    secondary_reasoning_provider = _preferred_provider(provider_names, "mistral", safe_default_provider, "qwen")
    secondary_structured_provider = _preferred_provider(provider_names, "qwen", safe_default_provider, "mistral")
    if reasoning_provider is None or structured_provider is None:
        raise ValueError("No model provider is configured. At least one provider must be available.")
    return {
        "requirements": _route(
            structured_provider,
            secondary_structured_provider,
            temperature=0.1,
            max_tokens=1400,
            budget_tokens=5000,
            request_timeout_seconds=900.0,
            reasoning="low",
            purpose="Requirements extraction and clarification.",
            output_contract="json",
            routing_note="Bevorzugt robusten, strikt parsebaren JSON-Output fuer Anforderungen.",
        ),
        "research": _route(
            structured_provider,
            secondary_structured_provider,
            temperature=0.1,
            max_tokens=2200,
            budget_tokens=9000,
            request_timeout_seconds=1800.0,
            reasoning="high",
            purpose="Repository and optional web research.",
            output_contract="text",
            routing_note=(
                "Bevorzugt den stabileren lokalen Text-Output fuer Repo-Kontext und Quellen; "
                "Qwen bleibt als semantischer Fallback verfuegbar."
            ),
        ),
        "architecture": _route(
            structured_provider,
            secondary_structured_provider,
            temperature=0.1,
            max_tokens=2200,
            budget_tokens=9000,
            request_timeout_seconds=1800.0,
            reasoning="high",
            purpose="Architecture, interfaces, deployment design, and implementation plan.",
            output_contract="json",
            routing_note=(
                "Bevorzugt den robusteren strukturierten JSON-Pfad fuer Architekturplaene; "
                "Qwen bleibt als semantischer Fallback verfuegbar."
            ),
        ),
        # Coding edit-plan generation has repeatedly failed harder when the
        # primary model returned empty content or near-valid but non-canonical
        # JSON. We therefore optimize defaults for the more stable structured
        # model first and keep the reasoning-heavy model only as fallback.
        "coding": _route(
            structured_provider,
            secondary_structured_provider,
            temperature=0.05,
            max_tokens=2600,
            budget_tokens=12000,
            request_timeout_seconds=1800.0,
            reasoning="medium",
            purpose="Code generation and safe file updates.",
            output_contract="edit_plan",
            routing_note=(
                "Bevorzugt das robustere JSON-Modell fuer konkrete Patch-Entscheidungen; "
                "Qwen bleibt fuer semantisch schwierige Faelle als Fallback verfuegbar."
            ),
        ),
        "rollback": _route(
            structured_provider,
            secondary_structured_provider,
            temperature=0.0,
            max_tokens=1200,
            budget_tokens=3000,
            request_timeout_seconds=600.0,
            reasoning="low",
            purpose="Deterministic rollback summaries and self-update watchdog operator notes.",
            output_contract="json",
            routing_note="Bevorzugt knappe, auditierbare Rollback- und Watchdog-Zusammenfassungen.",
        ),
        "reviewer": _route(
            structured_provider,
            secondary_structured_provider,
            temperature=0.0,
            max_tokens=1800,
            budget_tokens=8000,
            request_timeout_seconds=1200.0,
            reasoning="medium",
            purpose="Code review, correctness, maintainability, and architecture guardrails.",
            output_contract="json",
            routing_note="Bevorzugt strukturierte Findings und Warnungen statt freier Review-Prosa.",
        ),
        "tester": _route(
            structured_provider,
            secondary_structured_provider,
            temperature=0.0,
            max_tokens=1200,
            budget_tokens=4000,
            request_timeout_seconds=900.0,
            reasoning="low",
            purpose="Test planning and output summarization.",
            output_contract="json",
            routing_note="Bevorzugt parsebare Test- und Check-Zusammenfassungen.",
        ),
        "qa": _route(
            structured_provider,
            secondary_structured_provider,
            temperature=0.0,
            max_tokens=1200,
            budget_tokens=4000,
            request_timeout_seconds=900.0,
            reasoning="low",
            purpose="QA report summaries and smoke-check interpretation when a model is needed in the future.",
            output_contract="json",
            routing_note="Bevorzugt robuste, knappe QA-Summaries mit sauberem Format.",
        ),
        "security": _route(
            structured_provider,
            secondary_structured_provider,
            temperature=0.0,
            max_tokens=1800,
            budget_tokens=7000,
            request_timeout_seconds=1200.0,
            reasoning="medium",
            purpose="Security, prompt injection, dependency, and shell risk review.",
            output_contract="json",
            routing_note="Konservativ auf robusten strukturierten Output ausgerichtet, damit Security-Funde klar parsebar bleiben.",
        ),
        "validation": _route(
            structured_provider,
            secondary_structured_provider,
            temperature=0.0,
            max_tokens=1600,
            budget_tokens=6000,
            request_timeout_seconds=1200.0,
            reasoning="medium",
            purpose="Validation against the original Auftrag and acceptance criteria.",
            output_contract="json",
            routing_note="Bevorzugt strikte, evidenzbasierte Validierungsfelder statt freier Abschlussprosa.",
        ),
        "documentation": _route(
            structured_provider,
            secondary_structured_provider,
            temperature=0.1,
            max_tokens=1800,
            budget_tokens=6000,
            request_timeout_seconds=1200.0,
            reasoning="medium",
            purpose="Operator-facing and developer-facing documentation updates.",
            output_contract="text",
            routing_note="Bevorzugt kompakte, gut lesbare Betriebs- und Handover-Texte.",
        ),
        "memory": _route(
            structured_provider,
            secondary_structured_provider,
            temperature=0.0,
            max_tokens=1200,
            budget_tokens=3000,
            request_timeout_seconds=600.0,
            reasoning="low",
            purpose="Decision capture and long-term memory entries.",
            output_contract="json",
            routing_note="Bevorzugt knappe, sauber strukturierte Langzeitnotizen.",
        ),
        "data": _route(
            structured_provider,
            secondary_structured_provider,
            temperature=0.0,
            max_tokens=1400,
            budget_tokens=5000,
            request_timeout_seconds=900.0,
            reasoning="medium",
            purpose="Data extraction, normalization, and classification.",
            output_contract="json",
            routing_note="Bevorzugt konsistente Feld- und Statusstrukturen fuer Datenhygiene.",
        ),
        "ux": _route(
            reasoning_provider,
            secondary_reasoning_provider,
            temperature=0.2,
            max_tokens=1600,
            budget_tokens=5000,
            request_timeout_seconds=1200.0,
            reasoning="medium",
            purpose="UI/UX suggestions and flow improvements.",
            output_contract="json",
            routing_note="Bevorzugt staerkere semantische UX-Bewertung, behält aber einen strukturierten Fallback.",
        ),
        "cost": _route(
            structured_provider,
            secondary_structured_provider,
            temperature=0.0,
            max_tokens=900,
            budget_tokens=2500,
            request_timeout_seconds=600.0,
            reasoning="low",
            purpose="Resource and model budget estimation.",
            output_contract="json",
            routing_note="Bevorzugt konservative, gut parsebare Budget- und Routing-Schaetzungen.",
        ),
        "human_resources": _route(
            structured_provider,
            secondary_structured_provider,
            temperature=0.0,
            max_tokens=1000,
            budget_tokens=2500,
            request_timeout_seconds=600.0,
            reasoning="low",
            purpose="Team allocation and worker fit suggestions.",
            output_contract="json",
            routing_note="Bevorzugt klare Worker-Empfehlungen und Rollenlisten im sauberen Format.",
        ),
        "github": _route(
            structured_provider,
            secondary_structured_provider,
            temperature=0.0,
            max_tokens=1400,
            budget_tokens=5000,
            request_timeout_seconds=900.0,
            reasoning="low",
            purpose="Commit, branch, and draft-PR summaries when a model-backed helper is needed.",
            output_contract="json",
            routing_note="Bevorzugt kleine, klar strukturierte GitHub-Artefakte mit niedrigem Halluzinationsrisiko.",
        ),
        "deploy": _route(
            structured_provider,
            secondary_structured_provider,
            temperature=0.0,
            max_tokens=1200,
            budget_tokens=4000,
            request_timeout_seconds=900.0,
            reasoning="low",
            purpose="Deployment checklists, rollbacks, and structured staging summaries when a model is needed.",
            output_contract="json",
            routing_note="Bevorzugt strikte, reproduzierbare Deploy- und Rollback-Summaries.",
        ),
        "default": _route(
            safe_default_provider,
            _preferred_provider(provider_names, "mistral", "qwen", exclude={safe_default_provider}),
            temperature=0.1,
            max_tokens=1800,
            budget_tokens=6000,
            request_timeout_seconds=1200.0,
            reasoning="medium",
            purpose="Fallback route for uncategorized work.",
            output_contract="text",
            routing_note="Faellt konservativ auf den sichersten lokal verfuegbaren Standard zurueck.",
        ),
    }


def load_model_routing(settings: Settings) -> ModelRoutingConfig:
    """Load the worker routing file and merge it with safe local defaults."""

    providers = {
        name: ModelProvider(name=name, **provider_config)
        for name, provider_config in settings.model_provider_configs().items()
        if provider_config["base_url"] and provider_config["model_name"]
    }
    workers = _default_worker_routes(settings, set(providers))

    config_path = Path(settings.model_routing_config)
    if config_path.exists():
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        worker_overrides = raw.get("workers", {})
        for worker_name, override in worker_overrides.items():
            base_route = workers.get(worker_name, workers["default"])
            workers[worker_name] = base_route.model_copy(update=override)

    return ModelRoutingConfig(providers=providers, workers=workers)


def get_model_routing(settings: Settings) -> ModelRoutingConfig:
    """Return the resolved routing configuration for the current settings."""

    return load_model_routing(settings)


def resolve_worker_route(settings: Settings, worker_name: str) -> tuple[ModelProvider, WorkerModelRoute]:
    """Resolve the primary provider and route for a worker name."""

    routing = get_model_routing(settings)
    route = routing.workers.get(worker_name, routing.workers["default"])
    available_providers = set(routing.providers)
    resolved_primary = _preferred_provider(
        available_providers,
        route.primary_provider,
        route.fallback_provider,
        settings.default_model_provider,
        "mistral",
        "qwen",
    )
    if resolved_primary is None:
        raise ValueError(f"No model provider is available for worker `{worker_name}`.")
    resolved_fallback = None
    if route.fallback_provider:
        resolved_fallback = _preferred_provider(
            available_providers,
            route.fallback_provider,
            exclude={resolved_primary},
        )
    resolved_route = route.model_copy(
        update={
            "primary_provider": resolved_primary,
            "fallback_provider": resolved_fallback,
        }
    )
    return routing.providers[resolved_primary], resolved_route


def resolve_fallback_provider(settings: Settings, worker_name: str) -> ModelProvider | None:
    """Return the fallback provider for a worker if one is configured and resolvable."""

    routing = get_model_routing(settings)
    _, route = resolve_worker_route(settings, worker_name)
    if not route.fallback_provider:
        return None
    return routing.providers.get(route.fallback_provider)
