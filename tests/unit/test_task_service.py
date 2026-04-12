"""
Purpose: Validate task lifecycle persistence and approval bookkeeping.
Input/Output: Tests create tasks, store worker results, and record approvals against an isolated SQLite database.
Important invariants: Status, branch naming, worker results, and approval state must stay internally consistent.
How to debug: If these tests fail, inspect the TaskService methods and the SQLAlchemy record mappings they use.
"""

from __future__ import annotations

from services.shared.agentic_lab.schemas import (
    ApprovalDecision,
    ApprovalRequest,
    TaskArchiveRequest,
    TaskCreateRequest,
    TaskStageRestartRequest,
    TaskStatus,
    WorkerResponse,
    WorkflowWorkerName,
)
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
    assert ".task-workspaces" in detail.local_repo_path
    assert detail.metadata["source_local_repo_path"] == "/workspace/example-repo"
    assert detail.metadata["workspace_strategy"] == "task_isolated_checkout"

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


def test_task_service_persists_structured_worker_progress_in_metadata(isolated_session_factory) -> None:
    service = TaskService(session_factory=isolated_session_factory)
    summary = service.create_task(
        TaskCreateRequest(
            goal="Keep the UI informed while a slow coding stage waits on a local model.",
            repository="Feberdin/example-repo",
            local_repo_path="/workspace/example-repo",
        )
    )

    updated = service.append_event(
        summary.id,
        stage="CODING",
        message="Coding wartet auf das lokale Modell.",
        details={
            "worker_name": "coding",
            "state": "waiting",
            "current_instruction": "Bereite eine kleine, sichere Codeaenderung vor.",
            "waiting_for": "Lokales Modell",
            "progress_message": "Coding wartet auf Modellantwort.",
            "elapsed_seconds": 33.5,
        },
    )

    coding_progress = updated.metadata["worker_progress"]["coding"]
    assert coding_progress["state"] == "waiting"
    assert coding_progress["waiting_for"] == "Lokales Modell"
    assert coding_progress["progress_message"] == "Coding wartet auf Modellantwort."


def test_task_service_can_restart_one_worker_segment_without_creating_a_new_task(isolated_session_factory) -> None:
    service = TaskService(session_factory=isolated_session_factory)
    summary = service.create_task(
        TaskCreateRequest(
            goal="Analysiere das Repository, liefere einen Coding-Vorschlag und erlaube spaeter einen gezielten Neustart.",
            repository="Feberdin/example-repo",
            local_repo_path="/workspace/example-repo",
        )
    )

    service.store_worker_result(
        summary.id,
        "requirements",
        WorkerResponse(worker="requirements", summary="Requirements liegen vor.", risk_flags=["scope-known"]),
    )
    service.store_worker_result(
        summary.id,
        "research",
        WorkerResponse(worker="research", summary="Recherche wurde abgeschlossen.", risk_flags=["docs-reviewed"]),
    )
    service.store_worker_result(
        summary.id,
        "coding",
        WorkerResponse(worker="coding", summary="Coding ist fehlgeschlagen.", errors=["Git clone failed."]),
    )
    service.append_event(
        summary.id,
        stage="CODING",
        message="Coding ist am Git-Setup gescheitert.",
        details={
            "worker_name": "coding",
            "state": "failed",
            "current_instruction": "Behebe den Git-Fehler und starte diesen Teil erneut.",
            "last_error": "Git clone failed.",
        },
        level="WARNING",
    )
    service.update_status(
        summary.id,
        TaskStatus.FAILED,
        message="Coding ist fehlgeschlagen.",
        latest_error="Git clone failed.",
    )

    restarted = service.restart_from_worker(
        summary.id,
        TaskStageRestartRequest(
            worker_name=WorkflowWorkerName.RESEARCH,
            actor="dashboard",
            reason="Git-Umgebung ist korrigiert, bitte ab Recherche neu laufen lassen.",
        ),
    )

    assert restarted.status == TaskStatus.RESEARCHING
    assert restarted.resume_target == "research"
    assert restarted.latest_error is None
    assert restarted.worker_results["requirements"]["summary"] == "Requirements liegen vor."
    assert "research" not in restarted.worker_results
    assert "coding" not in restarted.worker_results
    assert restarted.metadata["worker_progress"]["research"]["state"] == "waiting"
    assert restarted.metadata["last_restart_request"]["worker_name"] == "research"
    assert restarted.metadata["last_restart_request"]["actor"] == "dashboard"
    assert restarted.events[-1].details["event_kind"] == "stage_restart_requested"


def test_task_service_archives_tasks_and_hides_them_from_default_list(isolated_session_factory) -> None:
    service = TaskService(session_factory=isolated_session_factory)
    summary = service.create_task(
        TaskCreateRequest(
            goal="Archivieren soll alte Aufgaben aus dem Standard-Dashboard entfernen.",
            repository="Feberdin/example-repo",
            local_repo_path="/workspace/example-repo",
        )
    )
    service.update_status(summary.id, TaskStatus.DONE, message="Aufgabe abgeschlossen.")

    archived = service.archive_task(
        summary.id,
        TaskArchiveRequest(actor="dashboard", reason="Bereits sauber abgeschlossen."),
    )

    assert archived.archived is True
    assert archived.archived_by == "dashboard"
    assert archived.archived_reason == "Bereits sauber abgeschlossen."
    assert service.list_tasks() == []
    assert service.list_tasks(only_archived=True)[0].id == summary.id


def test_task_service_restores_archived_tasks_back_into_default_list(isolated_session_factory) -> None:
    service = TaskService(session_factory=isolated_session_factory)
    summary = service.create_task(
        TaskCreateRequest(
            goal="Wiederherstellung soll archivierte Aufgaben erneut sichtbar machen.",
            repository="Feberdin/example-repo",
            local_repo_path="/workspace/example-repo",
        )
    )
    service.update_status(summary.id, TaskStatus.FAILED, message="Aufgabe fehlgeschlagen.", latest_error="Testfehler.")
    service.archive_task(summary.id, TaskArchiveRequest(actor="dashboard", reason="Altlast."))

    restored = service.restore_task(
        summary.id,
        TaskArchiveRequest(actor="dashboard", reason="Zur Nachpruefung wieder sichtbar machen."),
    )

    assert restored.archived is False
    assert service.list_tasks()[0].id == summary.id
    assert service.list_tasks(only_archived=True) == []


def test_task_service_rejects_archiving_running_tasks(isolated_session_factory) -> None:
    service = TaskService(session_factory=isolated_session_factory)
    summary = service.create_task(
        TaskCreateRequest(
            goal="Laufende Aufgaben duerfen nicht still archiviert werden.",
            repository="Feberdin/example-repo",
            local_repo_path="/workspace/example-repo",
        )
    )
    service.update_status(summary.id, TaskStatus.CODING, message="Coding laeuft noch.")

    try:
        service.archive_task(summary.id, TaskArchiveRequest(actor="dashboard", reason="Zu frueh."))
    except ValueError as exc:
        assert "abgeschlossene" in str(exc)
    else:  # pragma: no cover - defensive guard
        raise AssertionError("Running tasks must not be archivable.")
