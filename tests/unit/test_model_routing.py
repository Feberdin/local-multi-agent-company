"""
Purpose: Verify that worker-to-model routing stays configurable and resolves stable provider defaults.
Input/Output: Tests load the routing config with temporary overrides and resolve providers for representative workers.
Important invariants: Lightweight stages should prefer the faster local model by default.
Heavy reasoning stages can still opt into the larger backend.
How to debug: If these tests fail, inspect `config/model-routing.example.yaml` and `services/shared/agentic_lab/model_routing.py`.
"""

from __future__ import annotations

from pathlib import Path

from services.shared.agentic_lab.config import Settings
from services.shared.agentic_lab.model_routing import load_model_routing, resolve_worker_route


def test_load_model_routing_applies_worker_override(tmp_path: Path, monkeypatch) -> None:
    routing_file = tmp_path / "model-routing.yaml"
    routing_file.write_text(
        (
            "workers:\n"
            "  requirements:\n"
            "    primary_provider: qwen\n"
            "    fallback_provider: mistral\n"
            "    temperature: 0.3\n"
            "    max_tokens: 777\n"
            "    budget_tokens: 3333\n"
            "    reasoning: medium\n"
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("MODEL_ROUTING_CONFIG", str(routing_file))
    settings = Settings()

    routing = load_model_routing(settings)
    route = routing.workers["requirements"]

    assert route.primary_provider == "qwen"
    assert route.fallback_provider == "mistral"
    assert route.temperature == 0.3
    assert route.max_tokens == 777
    assert route.budget_tokens == 3333
    assert route.request_timeout_seconds == 45.0
    assert route.reasoning == "medium"


def test_resolve_worker_route_keeps_heavy_stage_on_qwen_by_default(monkeypatch) -> None:
    monkeypatch.setenv("MODEL_ROUTING_CONFIG", "/tmp/nonexistent-model-routing.yaml")
    settings = Settings()

    provider, route = resolve_worker_route(settings, "security")

    assert provider.name == "qwen"
    assert route.primary_provider == "qwen"
    assert route.reasoning == "high"


def test_resolve_worker_route_prefers_mistral_for_requirements_and_reviewer(monkeypatch) -> None:
    monkeypatch.setenv("MODEL_ROUTING_CONFIG", "/tmp/nonexistent-model-routing.yaml")
    settings = Settings()

    requirements_provider, requirements_route = resolve_worker_route(settings, "requirements")
    reviewer_provider, reviewer_route = resolve_worker_route(settings, "reviewer")

    assert requirements_provider.name == "mistral"
    assert requirements_route.fallback_provider is None
    assert requirements_route.request_timeout_seconds == 45.0
    assert reviewer_provider.name == "mistral"
    assert reviewer_route.fallback_provider is None
    assert reviewer_route.request_timeout_seconds == 60.0
