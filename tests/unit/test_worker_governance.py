"""
Purpose: Validate worker guidance persistence, decision-tree annotations, and suggestion approvals.
Input/Output: Exercises the worker governance service with isolated runtime paths and synthetic worker results.
Important invariants: Guidance stays worker-specific, suggestions deduplicate, and decision trees remain visible to the UI.
How to debug: If a test fails, inspect the stored JSON files in the temporary data directory for normalization drift.
"""

from __future__ import annotations

from services.shared.agentic_lab.config import get_settings
from services.shared.agentic_lab.schemas import (
    ImprovementSuggestionDecisionRequest,
    ImprovementSuggestionStatus,
    WorkerRequest,
    WorkerResponse,
)
from services.shared.agentic_lab.worker_governance import WorkerGovernanceService


def _sample_request(worker_name: str = "research") -> WorkerRequest:
    return WorkerRequest(
        task_id="task-1",
        goal="Research the latest official FastAPI version and summarize the repository context.",
        repository="Feberdin/example-repo",
        repo_url="https://github.com/Feberdin/example-repo.git",
        local_repo_path="/workspace/example-repo",
        base_branch="main",
        metadata={"worker_guidance_map": WorkerGovernanceService(get_settings()).guidance_map(), "current_worker_name": worker_name},
    )


def test_guidance_registry_loads_seed_workers() -> None:
    service = WorkerGovernanceService(get_settings())

    registry = service.load_guidance_registry()

    assert any(worker.worker_name == "coding" for worker in registry.workers)
    assert any(worker.worker_name == "research" for worker in registry.workers)


def test_annotate_worker_response_adds_guidance_and_decision_tree() -> None:
    service = WorkerGovernanceService(get_settings())
    request = _sample_request("reviewer")
    response = WorkerResponse(
        worker="reviewer",
        summary="Diff review completed.",
        outputs={"findings": ["Code files changed without test update."]},
        warnings=["Tests should be reviewed."],
        risk_flags=["infrastructure_change"],
        requires_human_approval=True,
        approval_reason="Risky changes detected.",
    )

    annotated = service.annotate_worker_response("reviewer", request, response)

    assert "applied_guidance" in annotated.outputs
    assert "decision_tree" in annotated.outputs
    assert annotated.outputs["decision_tree"]["worker_name"] == "reviewer"


def test_register_worker_suggestions_deduplicates_titles() -> None:
    service = WorkerGovernanceService(get_settings())
    request = _sample_request("reviewer")
    response = WorkerResponse(
        worker="reviewer",
        summary="Diff review completed.",
        outputs={"findings": ["Code files changed without any obvious corresponding test update."]},
    )

    first = service.register_worker_suggestions(worker_name="reviewer", request=request, response=response)
    second = service.register_worker_suggestions(worker_name="reviewer", request=request, response=response)

    assert len(first) == 1
    assert second == []


def test_suggestion_decision_updates_registry() -> None:
    service = WorkerGovernanceService(get_settings())
    request = _sample_request("security")
    response = WorkerResponse(
        worker="security",
        summary="Security review completed.",
        outputs={},
        risk_flags=["security_manual_review"],
    )
    created = service.register_worker_suggestions(worker_name="security", request=request, response=response)

    registry = service.decide_suggestion(
        created[0].id,
        ImprovementSuggestionDecisionRequest(
            decision=ImprovementSuggestionStatus.APPROVED,
            actor="ceo-dashboard",
            note="Approved for next governance cycle.",
        ),
    )

    decided = next(item for item in registry.suggestions if item.id == created[0].id)
    assert decided.status is ImprovementSuggestionStatus.APPROVED
    assert decided.actor == "ceo-dashboard"
