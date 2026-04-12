"""
Unit tests for the self-improvement service.
Covers risk classification, error classification, daily limits, state transitions, and cycle guards.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.shared.agentic_lab.config import Settings
from services.shared.agentic_lab.schemas import TaskCreateRequest
from services.shared.agentic_lab.self_improvement import (
    CycleStatus,
    ProblemClass,
    RiskLevel,
    SelfImprovementError,
    SelfImprovementService,
    classify_error_text,
    classify_risk,
)
from services.shared.agentic_lab.task_service import TaskService


def _self_improvement_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    **overrides: str,
) -> Settings:
    """Build isolated settings for service-level self-improvement tests."""

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("TASK_WORKSPACE_ROOT", str(tmp_path / "workspace" / ".task-workspaces"))
    monkeypatch.setenv("SELF_IMPROVEMENT_TARGET_REPO", "Feberdin/local-multi-agent-company")
    monkeypatch.setenv("SELF_IMPROVEMENT_LOCAL_REPO_PATH", str(tmp_path / "workspace" / "local-multi-agent-company"))
    monkeypatch.setenv("SELF_IMPROVEMENT_POLICY_PATH", str(tmp_path / "missing-policy.yaml"))
    for key, value in overrides.items():
        monkeypatch.setenv(key, value)
    return Settings()

# ---------------------------------------------------------------------------
# classify_risk
# ---------------------------------------------------------------------------


def test_classify_risk_low_for_benign_goal():
    level, reason = classify_risk("Verbessere die Fehlerbehandlung in requirements_worker")
    assert level == RiskLevel.LOW
    assert reason is None


def test_classify_risk_critical_for_self_improvement():
    level, reason = classify_risk("Aendere die self-improvement Logik")
    assert level == RiskLevel.CRITICAL
    assert reason is not None


def test_classify_risk_high_for_auth():
    level, reason = classify_risk("Refactore das auth-Modul fuer bessere JWT-Validierung")
    assert level == RiskLevel.HIGH
    assert reason is not None


def test_classify_risk_high_for_secret():
    level, reason = classify_risk("Passe die Handhabung von api_key und password an")
    assert level == RiskLevel.HIGH
    assert reason is not None


def test_classify_risk_high_for_deploy():
    level, reason = classify_risk("Verbessere das CI/CD-Deployment-Skript")
    assert level == RiskLevel.HIGH
    assert reason is not None


def test_classify_risk_medium_for_docker():
    level, reason = classify_risk("Aktualisiere das Dockerfile fuer kleinere Images")
    assert level == RiskLevel.MEDIUM
    assert reason is not None


def test_classify_risk_high_for_database():
    level, reason = classify_risk("Add a new column via database migration")
    assert level == RiskLevel.HIGH
    assert reason is not None


def test_classify_risk_first_match_wins():
    # self-improvement comes before auth in the pattern list → CRITICAL
    level, reason = classify_risk("Verbessere self-improvement auth handling")
    assert level == RiskLevel.CRITICAL


# ---------------------------------------------------------------------------
# classify_error_text
# ---------------------------------------------------------------------------


def test_classify_error_timeout():
    assert classify_error_text("Request timed out after 30 seconds") == ProblemClass.TIMEOUT


def test_classify_error_unreachable():
    assert classify_error_text("Connection refused to http://localhost:8001") == ProblemClass.UNREACHABLE_ENDPOINT


def test_classify_error_git():
    assert classify_error_text("fatal: not a git repository") == ProblemClass.GIT_ERROR


def test_classify_error_json():
    assert classify_error_text("Model did not return valid json in response") == ProblemClass.INVALID_RESPONSE_SCHEMA


def test_classify_error_validation():
    assert classify_error_text("ValidationError: string_too_long for field query") == ProblemClass.INVALID_RESPONSE_SCHEMA


def test_classify_error_deploy():
    assert classify_error_text("deploy failed due to missing compose file") == ProblemClass.DEPLOYMENT_FAILURE


def test_classify_error_template():
    assert classify_error_text("Jinja2 template rendering failed") == ProblemClass.UI_RENDERING_PROBLEM


def test_classify_error_unknown():
    assert classify_error_text("something completely unexpected happened") == ProblemClass.UNKNOWN


def test_classify_error_case_insensitive():
    assert classify_error_text("TIMEOUT exceeded for stage") == ProblemClass.TIMEOUT


# ---------------------------------------------------------------------------
# CycleStatus sets
# ---------------------------------------------------------------------------


def test_terminal_statuses_are_disjoint_from_active():
    terminal = {CycleStatus.COMPLETED, CycleStatus.FAILED, CycleStatus.PAUSED}
    active = {
        CycleStatus.ANALYZING,
        CycleStatus.PLANNING,
        CycleStatus.IMPLEMENTING,
        CycleStatus.VALIDATING,
        CycleStatus.DEPLOYING,
        CycleStatus.POST_DEPLOY_TESTING,
        CycleStatus.AWAITING_MANUAL_REVIEW,
    }
    assert not terminal & active


def test_all_cycle_statuses_covered():
    all_statuses = set(CycleStatus)
    terminal = {CycleStatus.COMPLETED, CycleStatus.FAILED, CycleStatus.PAUSED}
    active = {
        CycleStatus.ANALYZING,
        CycleStatus.PLANNING,
        CycleStatus.IMPLEMENTING,
        CycleStatus.VALIDATING,
        CycleStatus.DEPLOYING,
        CycleStatus.POST_DEPLOY_TESTING,
        CycleStatus.AWAITING_MANUAL_REVIEW,
    }
    # IDLE is the initial/rest state — not terminal, not active
    assert terminal | active | {CycleStatus.IDLE} == all_statuses


# ---------------------------------------------------------------------------
# SelfImprovementError
# ---------------------------------------------------------------------------


def test_self_improvement_error_is_runtime_error():
    exc = SelfImprovementError("test message")
    assert isinstance(exc, RuntimeError)
    assert str(exc) == "test message"


# ---------------------------------------------------------------------------
# ProblemClass values
# ---------------------------------------------------------------------------


def test_all_problem_classes_have_string_values():
    for cls in ProblemClass:
        assert isinstance(cls.value, str)
        assert len(cls.value) > 0


def test_risk_level_ordering():
    # Just verify the four expected values exist
    assert RiskLevel.LOW in RiskLevel
    assert RiskLevel.MEDIUM in RiskLevel
    assert RiskLevel.HIGH in RiskLevel
    assert RiskLevel.CRITICAL in RiskLevel


@pytest.mark.asyncio
async def test_run_pipeline_manual_critical_goal_waits_for_human_approval(
    isolated_session_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _self_improvement_settings(
        tmp_path,
        monkeypatch,
        SELF_IMPROVEMENT_ENABLED="true",
        SELF_IMPROVEMENT_MODE="manual",
    )
    task_service = TaskService(isolated_session_factory, settings)
    service = SelfImprovementService(task_service, object(), settings=settings, session_factory=isolated_session_factory)
    cycle = service._create_record(trigger="manual", problem_hint="self-improvement")  # noqa: SLF001

    async def fake_analyze(*args, **kwargs):  # type: ignore[no-untyped-def]
        return (
            "Aendere die self-improvement Logik im eigenen Repository.",
            ProblemClass.CODE_QUALITY,
            "Die Self-Improvement-Steuerung selbst soll angepasst werden.",
        )

    async def fake_send_email(**kwargs):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr("services.shared.agentic_lab.self_improvement.analyze_problems", fake_analyze)
    monkeypatch.setattr(service, "_send_cycle_email", fake_send_email)

    await service._run_pipeline(cycle.id, "self-improvement", None)  # noqa: SLF001
    await asyncio.sleep(0)

    refreshed = service.get_cycle(cycle.id)
    assert refreshed is not None
    assert refreshed.status == CycleStatus.AWAITING_MANUAL_REVIEW.value
    assert refreshed.task_id is None
    assert (refreshed.metadata_json or {})["governance_status"] == "awaiting_approval"


@pytest.mark.asyncio
async def test_approve_risky_cycle_resumes_existing_task_gate(
    isolated_session_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _self_improvement_settings(
        tmp_path,
        monkeypatch,
        SELF_IMPROVEMENT_ENABLED="true",
        SELF_IMPROVEMENT_MODE="assisted",
    )
    task_service = TaskService(isolated_session_factory, settings)
    service = SelfImprovementService(task_service, object(), settings=settings, session_factory=isolated_session_factory)

    task = task_service.create_task(
        TaskCreateRequest(
            goal="Verbessere die Self-Improvement-Statusanzeige im eigenen Repository.",
            repository="Feberdin/local-multi-agent-company",
            local_repo_path=str(tmp_path / "workspace" / "local-multi-agent-company"),
            allow_repository_modifications=True,
            metadata={"allow_repository_modifications": True},
        )
    )
    task_service.set_approval_required(
        task.id,
        "Riskante Veroeffentlichung muss freigegeben werden.",
        "github",
        gate_name="self-improvement-risk-review",
    )
    cycle = service._create_record(trigger="manual", problem_hint=None)  # noqa: SLF001
    service._update(  # noqa: SLF001
        cycle.id,
        status=CycleStatus.AWAITING_MANUAL_REVIEW.value,
        task_id=task.id,
        goal="Verbessere die Self-Improvement-Statusanzeige im eigenen Repository.",
        risk_level=RiskLevel.HIGH.value,
        metadata_json={"current_gate_name": "self-improvement-risk-review"},
    )

    resumed_tasks: list[str] = []

    async def fake_run_task(task_id: str) -> None:
        resumed_tasks.append(task_id)

    response = await service.approve_risky_cycle(
        cycle.id,
        actor="operator@example.com",
        reason="Die vorbereitete Aenderung darf weiterlaufen.",
        run_task_fn=fake_run_task,
    )
    await asyncio.sleep(0)

    refreshed_task = task_service.get_task(task.id)
    assert response.status == CycleStatus.IMPLEMENTING.value
    assert refreshed_task.approval_required is False
    assert refreshed_task.current_approval_gate_name is None
    assert resumed_tasks == [task.id]


@pytest.mark.asyncio
async def test_failed_cycle_creates_incident_and_rollback_task(
    isolated_session_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _self_improvement_settings(
        tmp_path,
        monkeypatch,
        SELF_IMPROVEMENT_ENABLED="true",
        SELF_IMPROVEMENT_MODE="automatic",
        SELF_IMPROVEMENT_AUTO_ROLLBACK="true",
    )
    task_service = TaskService(isolated_session_factory, settings)
    service = SelfImprovementService(task_service, object(), settings=settings, session_factory=isolated_session_factory)
    cycle = service._create_record(trigger="automatic", problem_hint="rollback")  # noqa: SLF001
    service._update(  # noqa: SLF001
        cycle.id,
        status=CycleStatus.IMPLEMENTING.value,
        goal="Fixe einen fehlgeschlagenen Self-Improvement-Deploy.",
        problem_hypothesis="Ein fehlerhafter Commit hat den letzten Lauf kaputt gemacht.",
        risk_level=RiskLevel.HIGH.value,
        task_id="task-failed",
        metadata_json={},
    )

    failed_task = SimpleNamespace(
        id="task-failed",
        latest_error="deploy failed after healthcheck",
        branch_name="feature/self-improvement-fix",
        worker_results={
            "coding": {
                "outputs": {
                    "branch_name": "feature/self-improvement-fix",
                    "changed_files": ["services/orchestrator/app.py"],
                }
            },
            "github": {"outputs": {"commit_sha": "abc123def456"}},
        },
    )

    started_tasks: list[str] = []

    async def fake_run_task(task_id: str) -> None:
        started_tasks.append(task_id)

    handled_task_id = await service._handle_failed_task(  # noqa: SLF001
        cycle_id=cycle.id,
        task=failed_task,
        run_task_fn=fake_run_task,
    )
    await asyncio.sleep(0)

    assert handled_task_id is None
    incidents = service.list_incidents()
    assert len(incidents) == 1
    incident = incidents[0]
    assert incident.cycle_id == cycle.id
    assert incident.rollback_task_id is not None
    assert incident.rollback_status == "rollback_running"
    assert started_tasks == [incident.rollback_task_id]

    rollback_task = task_service.get_task(incident.rollback_task_id)
    assert rollback_task.metadata["rollback_commit_sha"] == "abc123def456"
    assert rollback_task.metadata["rollback_incident_id"] == incident.id

    refreshed_cycle = service.get_cycle(cycle.id)
    assert refreshed_cycle is not None
    assert (refreshed_cycle.metadata_json or {})["incident_id"] == incident.id
    assert (refreshed_cycle.metadata_json or {})["rollback_task_id"] == incident.rollback_task_id
