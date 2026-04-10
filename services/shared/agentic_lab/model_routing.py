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
    return {
        "requirements": _route(
            "mistral",
            None,
            temperature=0.1,
            max_tokens=1400,
            budget_tokens=5000,
            request_timeout_seconds=45.0,
            reasoning="low",
            purpose="Requirements extraction and clarification.",
        ),
        "research": _route(
            "qwen",
            "mistral",
            temperature=0.1,
            max_tokens=2200,
            budget_tokens=9000,
            request_timeout_seconds=120.0,
            reasoning="high",
            purpose="Repository and optional web research.",
        ),
        "architecture": _route(
            "qwen",
            "mistral",
            temperature=0.1,
            max_tokens=2200,
            budget_tokens=9000,
            request_timeout_seconds=120.0,
            reasoning="high",
            purpose="Architecture, interfaces, deployment design, and implementation plan.",
        ),
        "coding": _route(
            "qwen",
            "mistral",
            temperature=0.05,
            max_tokens=2600,
            budget_tokens=12000,
            request_timeout_seconds=150.0,
            reasoning="high",
            purpose="Code generation and safe file updates.",
        ),
        "reviewer": _route(
            "mistral",
            None,
            temperature=0.0,
            max_tokens=1800,
            budget_tokens=8000,
            request_timeout_seconds=60.0,
            reasoning="medium",
            purpose="Code review, correctness, maintainability, and architecture guardrails.",
        ),
        "tester": _route(
            "mistral",
            None,
            temperature=0.0,
            max_tokens=1200,
            budget_tokens=4000,
            request_timeout_seconds=45.0,
            reasoning="low",
            purpose="Test planning and output summarization.",
        ),
        "qa": _route(
            "mistral",
            None,
            temperature=0.0,
            max_tokens=1200,
            budget_tokens=4000,
            request_timeout_seconds=45.0,
            reasoning="low",
            purpose="QA report summaries and smoke-check interpretation when a model is needed in the future.",
        ),
        "security": _route(
            "qwen",
            "mistral",
            temperature=0.0,
            max_tokens=1800,
            budget_tokens=7000,
            request_timeout_seconds=90.0,
            reasoning="high",
            purpose="Security, prompt injection, dependency, and shell risk review.",
        ),
        "validation": _route(
            "qwen",
            "mistral",
            temperature=0.0,
            max_tokens=1600,
            budget_tokens=6000,
            request_timeout_seconds=90.0,
            reasoning="high",
            purpose="Validation against the original Auftrag and acceptance criteria.",
        ),
        "documentation": _route(
            "mistral",
            None,
            temperature=0.1,
            max_tokens=1800,
            budget_tokens=6000,
            request_timeout_seconds=60.0,
            reasoning="medium",
            purpose="Operator-facing and developer-facing documentation updates.",
        ),
        "memory": _route(
            "mistral",
            None,
            temperature=0.0,
            max_tokens=1200,
            budget_tokens=3000,
            request_timeout_seconds=45.0,
            reasoning="low",
            purpose="Decision capture and long-term memory entries.",
        ),
        "data": _route(
            "mistral",
            None,
            temperature=0.0,
            max_tokens=1400,
            budget_tokens=5000,
            request_timeout_seconds=60.0,
            reasoning="medium",
            purpose="Data extraction, normalization, and classification.",
        ),
        "ux": _route(
            "mistral",
            None,
            temperature=0.2,
            max_tokens=1600,
            budget_tokens=5000,
            request_timeout_seconds=60.0,
            reasoning="medium",
            purpose="UI/UX suggestions and flow improvements.",
        ),
        "cost": _route(
            "mistral",
            None,
            temperature=0.0,
            max_tokens=900,
            budget_tokens=2500,
            request_timeout_seconds=40.0,
            reasoning="low",
            purpose="Resource and model budget estimation.",
        ),
        "human_resources": _route(
            "mistral",
            None,
            temperature=0.0,
            max_tokens=1000,
            budget_tokens=2500,
            request_timeout_seconds=40.0,
            reasoning="low",
            purpose="Team allocation and worker fit suggestions.",
        ),
        "default": _route(
            safe_default_provider,
            None,
            temperature=0.1,
            max_tokens=1800,
            budget_tokens=6000,
            request_timeout_seconds=60.0,
            reasoning="medium",
            purpose="Fallback route for uncategorized work.",
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
