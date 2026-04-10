"""
Purpose: Validate task lifecycle persistence and approval bookkeeping.
Input/Output: Tests create tasks, store worker results, and record approvals against an isolated SQLite database.
Important invariants: Status, branch naming, worker results, and approval state must stay internally consistent.
How to debug: If these tests fail, inspect the TaskService methods and the SQLAlchemy record mappings they use.
"""

from __future__ import annotations

from services.shared.agentic_lab.schemas import ApprovalDecision, ApprovalRequest, TaskCreateRequest, WorkerResponse
from services.shared.agentic_lab.task_service import TaskService


def test_task_service_creates_tasks_and_stores_worker_results(isolated_session_factory) -> None:
    service = TaskService(session_factory=isolated_session_factory)
    summary = service.create_task(
        TaskCreateRequest(
            goal="Add a health endpoint and corresponding tests for the API service.",
            repository="Feberdin/example-repo",
            local_repo_path="/workspace/example-repo",
            test_commands=["pytest -q"],
        )
    )

    detail = service.get_task(summary.id)
    assert detail.status.value == "NEW"
    assert detail.branch_name is not None and detail.branch_name.startswith("feature/")
    assert detail.metadata["test_commands"] == ["pytest -q"]

    updated = service.store_worker_result(
        summary.id,
        "research",
        WorkerResponse(worker="research", summary="Research done.", outputs={"notes": "ok"}),
    )
    assert updated.worker_results["research"]["summary"] == "Research done."


def test_task_service_tracks_approval_gates(isolated_session_factory) -> None:
    service = TaskService(session_factory=isolated_session_factory)
    summary = service.create_task(
        TaskCreateRequest(
            goal="Change docker-compose staging wiring without touching production.",
            repository="Feberdin/example-repo",
            local_repo_path="/workspace/example-repo",
        )
    )

    gated = service.set_approval_required(summary.id, "Infrastructure changes need manual review.", "testing")
    assert gated.approval_required is True
    assert gated.resume_target == "testing"

    approved = service.record_approval(
        summary.id,
        ApprovalRequest(gate_name="risk-review", decision=ApprovalDecision.APPROVE, actor="tester"),
    )
    assert approved.approval_required is False
    assert len(approved.approvals) == 1
