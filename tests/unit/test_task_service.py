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


def test_task_service_marks_repository_modification_permission_after_approval(isolated_session_factory) -> None:
    service = TaskService(session_factory=isolated_session_factory)
    summary = service.create_task(
        TaskCreateRequest(
            goal="Analyse the repository first and only change it after explicit approval.",
            repository="Feberdin/example-repo",
            local_repo_path="/workspace/example-repo",
            allow_repository_modifications=False,
        )
    )

    gated = service.set_approval_required(
        summary.id,
        "Explicit repository modification approval is required.",
        "coding",
        gate_name="repository-modification",
    )
    assert gated.current_approval_gate_name == "repository-modification"
    assert gated.allow_repository_modifications is False

    approved = service.record_approval(
        summary.id,
        ApprovalRequest(gate_name="repository-modification", decision=ApprovalDecision.APPROVE, actor="operator"),
    )
    assert approved.allow_repository_modifications is True
    assert approved.current_approval_gate_name is None


def test_task_service_append_event_keeps_long_running_stage_visible(isolated_session_factory) -> None:
    service = TaskService(session_factory=isolated_session_factory)
    summary = service.create_task(
        TaskCreateRequest(
            goal="Run a slow local requirements extraction and keep the UI informed with heartbeat events.",
            repository="Feberdin/example-repo",
            local_repo_path="/workspace/example-repo",
        )
    )

    before = service.get_task(summary.id)
    updated = service.append_event(
        summary.id,
        stage="REQUIREMENTS",
        message="Requirements stage still running.",
        details={"worker_name": "requirements", "heartbeat": True},
    )

    assert updated.events[-1].message == "Requirements stage still running."
    assert updated.events[-1].details["heartbeat"] is True
    assert updated.updated_at >= before.updated_at
