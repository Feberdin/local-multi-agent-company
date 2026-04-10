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
    reasoning: str,
    purpose: str,
) -> WorkerModelRoute:
    return WorkerModelRoute(
        primary_provider=primary_provider,
        fallback_provider=fallback_provider,
        temperature=temperature,
        max_tokens=max_tokens,
        budget_tokens=budget_tokens,
        reasoning=reasoning,
        purpose=purpose,
    )


def _default_worker_routes() -> dict[str, WorkerModelRoute]:
    return {
        "requirements": _route(
            "mistral",
            "qwen",
            temperature=0.1,
            max_tokens=1400,
            budget_tokens=5000,
            reasoning="low",
            purpose="Requirements extraction and clarification.",
        ),
        "research": _route(
            "qwen",
            "mistral",
            temperature=0.1,
            max_tokens=2200,
            budget_tokens=9000,
            reasoning="high",
            purpose="Repository and optional web research.",
        ),
        "architecture": _route(
            "qwen",
            "mistral",
            temperature=0.1,
            max_tokens=2200,
            budget_tokens=9000,
            reasoning="high",
            purpose="Architecture, interfaces, deployment design, and implementation plan.",
        ),
        "coding": _route(
            "qwen",
            "mistral",
            temperature=0.05,
            max_tokens=2600,
            budget_tokens=12000,
            reasoning="high",
            purpose="Code generation and safe file updates.",
        ),
        "reviewer": _route(
            "qwen",
            "mistral",
            temperature=0.0,
            max_tokens=1800,
            budget_tokens=8000,
            reasoning="high",
            purpose="Code review, correctness, maintainability, and architecture guardrails.",
        ),
        "tester": _route(
            "mistral",
            "qwen",
            temperature=0.0,
            max_tokens=1200,
            budget_tokens=4000,
            reasoning="low",
            purpose="Test planning and output summarization.",
        ),
        "security": _route(
            "qwen",
            "mistral",
            temperature=0.0,
            max_tokens=1800,
            budget_tokens=7000,
            reasoning="high",
            purpose="Security, prompt injection, dependency, and shell risk review.",
        ),
        "validation": _route(
            "qwen",
            "mistral",
            temperature=0.0,
            max_tokens=1600,
            budget_tokens=6000,
            reasoning="high",
            purpose="Validation against the original Auftrag and acceptance criteria.",
        ),
        "documentation": _route(
            "mistral",
            "qwen",
            temperature=0.1,
            max_tokens=1800,
            budget_tokens=6000,
            reasoning="medium",
            purpose="Operator-facing and developer-facing documentation updates.",
        ),
        "memory": _route(
            "mistral",
            "qwen",
            temperature=0.0,
            max_tokens=1200,
            budget_tokens=3000,
            reasoning="low",
            purpose="Decision capture and long-term memory entries.",
        ),
        "data": _route(
            "mistral",
            "qwen",
            temperature=0.0,
            max_tokens=1400,
            budget_tokens=5000,
            reasoning="medium",
            purpose="Data extraction, normalization, and classification.",
        ),
        "ux": _route(
            "mistral",
            "qwen",
            temperature=0.2,
            max_tokens=1600,
            budget_tokens=5000,
            reasoning="medium",
            purpose="UI/UX suggestions and flow improvements.",
        ),
        "cost": _route(
            "mistral",
            "qwen",
            temperature=0.0,
            max_tokens=900,
            budget_tokens=2500,
            reasoning="low",
            purpose="Resource and model budget estimation.",
        ),
        "human_resources": _route(
            "mistral",
            "qwen",
            temperature=0.0,
            max_tokens=1000,
            budget_tokens=2500,
            reasoning="low",
            purpose="Team allocation and worker fit suggestions.",
        ),
        "default": _route(
            "qwen",
            "mistral",
            temperature=0.1,
            max_tokens=1800,
            budget_tokens=6000,
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
    workers = _default_worker_routes()

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
    if route.primary_provider not in routing.providers:
        raise ValueError(f"Unknown primary model provider `{route.primary_provider}` for worker `{worker_name}`.")
    return routing.providers[route.primary_provider], route


def resolve_fallback_provider(settings: Settings, worker_name: str) -> ModelProvider | None:
    """Return the fallback provider for a worker if one is configured and resolvable."""

    routing = get_model_routing(settings)
    route = routing.workers.get(worker_name, routing.workers["default"])
    if not route.fallback_provider:
        return None
    return routing.providers.get(route.fallback_provider)
