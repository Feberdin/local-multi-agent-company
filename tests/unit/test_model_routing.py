"""
Purpose: Verify that worker-to-model routing stays configurable and resolves stable provider defaults.
Input/Output: Tests load the routing config with temporary overrides and resolve providers for representative workers.
Important invariants: Die meisten Worker sollen standardmaessig auf das robustere JSON-/Text-Modell gehen.
Qwen bleibt als expliziter Fallback fuer semantisch schwierigere Faelle verfuegbar.
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
    assert route.request_timeout_seconds == 900.0
    assert route.reasoning == "medium"
    assert route.output_contract == "json"


def test_resolve_worker_route_prefers_mistral_for_research_by_default(monkeypatch) -> None:
    monkeypatch.setenv("MODEL_ROUTING_CONFIG", "/tmp/nonexistent-model-routing.yaml")
    settings = Settings()

    provider, route = resolve_worker_route(settings, "research")

    assert provider.name == "mistral"
    assert route.primary_provider == "mistral"
    assert route.fallback_provider == "qwen"
    assert route.reasoning == "high"
    assert route.output_contract == "text"


def test_resolve_worker_route_prefers_mistral_for_coding_and_other_structured_workers(monkeypatch) -> None:
    monkeypatch.setenv("MODEL_ROUTING_CONFIG", "/tmp/nonexistent-model-routing.yaml")
    settings = Settings()

    requirements_provider, requirements_route = resolve_worker_route(settings, "requirements")
    reviewer_provider, reviewer_route = resolve_worker_route(settings, "reviewer")
    architecture_provider, architecture_route = resolve_worker_route(settings, "architecture")
    coding_provider, coding_route = resolve_worker_route(settings, "coding")
    security_provider, security_route = resolve_worker_route(settings, "security")
    ux_provider, ux_route = resolve_worker_route(settings, "ux")

    assert requirements_provider.name == "mistral"
    assert requirements_route.fallback_provider == "qwen"
    assert requirements_route.request_timeout_seconds == 900.0
    assert requirements_route.output_contract == "json"
    assert reviewer_provider.name == "mistral"
    assert reviewer_route.fallback_provider == "qwen"
    assert reviewer_route.request_timeout_seconds == 1200.0
    assert reviewer_route.output_contract == "json"
    assert architecture_provider.name == "mistral"
    assert architecture_route.fallback_provider == "qwen"
    assert architecture_route.output_contract == "json"
    assert coding_provider.name == "mistral"
    assert coding_route.fallback_provider == "qwen"
    assert coding_route.output_contract == "edit_plan"
    assert "JSON-Modell" in coding_route.routing_note or "robustere" in coding_route.routing_note
    assert security_provider.name == "mistral"
    assert security_route.fallback_provider == "qwen"
    assert security_route.output_contract == "json"
    assert ux_provider.name == "mistral"
    assert ux_route.fallback_provider == "qwen"
    assert ux_route.output_contract == "json"
