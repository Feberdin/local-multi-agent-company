"""
Purpose: Verify that the readiness runner always produces a structured report, even when checks
fail, are skipped, or raise exceptions internally.
Input/Output: Tests patch the check definition list so report aggregation can be exercised without
real network, Git, or model traffic.
Important invariants: One broken check must not crash the report, category summaries stay
consistent, and timing fields remain plausible.
How to debug: If these tests fail, inspect `services/shared/agentic_lab/readiness_checks.py`
because that module now owns the aggregation and safety net behavior.
"""

from __future__ import annotations

from pathlib import Path

from services.shared.agentic_lab.config import Settings
from services.shared.agentic_lab.readiness_checks import (
    ReadinessCheckDefinition,
    ReadinessContext,
    ReadinessServices,
    _secrets_files,
    build_catastrophic_readiness_report,
    build_readiness_report,
)
from services.shared.agentic_lab.readiness_models import (
    ReadinessCheckStatus,
    ReadinessMode,
    ReadinessSeverity,
)


def _settings(tmp_path: Path) -> Settings:
    data_dir = tmp_path / "data"
    reports_dir = tmp_path / "reports"
    workspace_root = tmp_path / "workspace"
    staging_stack_root = tmp_path / "staging"
    for path in (data_dir, reports_dir, workspace_root, staging_stack_root):
        path.mkdir(parents=True, exist_ok=True)
    return Settings(
        DATA_DIR=data_dir,
        REPORTS_DIR=reports_dir,
        WORKSPACE_ROOT=workspace_root,
        STAGING_STACK_ROOT=staging_stack_root,
        ORCHESTRATOR_DB_PATH=data_dir / "orchestrator.db",
        SELF_IMPROVEMENT_LOCAL_REPO_PATH=str(workspace_root / "local-multi-agent-company"),
        DEFAULT_LOCAL_REPO_PATH=str(workspace_root / "example-repo"),
    )


async def test_readiness_report_aggregates_partial_failures_without_crashing(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)

    async def backend_ok(_ctx):
        return {
            "status": ReadinessCheckStatus.OK,
            "severity": ReadinessSeverity.INFO,
            "message": "Backend steht.",
        }

    async def worker_warning(_ctx):
        return {
            "status": ReadinessCheckStatus.WARNING,
            "severity": ReadinessSeverity.MEDIUM,
            "message": "Worker ist langsam, aber erreichbar.",
            "hint": "Timeouts beobachten.",
        }

    monkeypatch.setattr(
        "services.shared.agentic_lab.readiness_checks._definitions_for_context",
        lambda _ctx: [
            ReadinessCheckDefinition("backend-ok", "backend", "Backend", backend_ok),
            ReadinessCheckDefinition("worker-warning", "workers", "Worker", worker_warning),
        ],
    )

    report = await build_readiness_report(settings, mode=ReadinessMode.QUICK)

    assert report.overall_status is ReadinessCheckStatus.WARNING
    assert report.summary.total == 2
    assert report.summary.ok == 1
    assert report.summary.warning == 1
    backend_category = next(item for item in report.categories if item.id == "backend")
    workers_category = next(item for item in report.categories if item.id == "workers")
    assert backend_category.ok == 1
    assert workers_category.warning == 1
    assert report.recommendations[0].message == "Timeouts beobachten."
    assert report.started_at <= report.finished_at
    assert report.duration_ms >= 0


async def test_readiness_report_serializes_check_exceptions_into_failed_results(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)

    async def crashing_check(_ctx):
        raise RuntimeError("simulierter Check-Crash")

    monkeypatch.setattr(
        "services.shared.agentic_lab.readiness_checks._definitions_for_context",
        lambda _ctx: [
            ReadinessCheckDefinition("backend-crash", "backend", "Abgestuerzter Check", crashing_check),
        ],
    )

    report = await build_readiness_report(settings, mode=ReadinessMode.QUICK)

    assert report.overall_status is ReadinessCheckStatus.FAIL
    assert report.summary.fail == 1
    assert report.checks[0].status is ReadinessCheckStatus.FAIL
    assert "RuntimeError" in report.checks[0].detail
    assert "Andere Checks wurden trotzdem weiter ausgewertet" in report.checks[0].hint


async def test_quick_mode_marks_deep_checks_as_skipped(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)

    async def deep_check(_ctx):
        return {
            "status": ReadinessCheckStatus.OK,
            "severity": ReadinessSeverity.INFO,
            "message": "Sollte im Schnellcheck nicht laufen.",
        }

    monkeypatch.setattr(
        "services.shared.agentic_lab.readiness_checks._definitions_for_context",
        lambda _ctx: [
            ReadinessCheckDefinition("deep-only", "llm", "Nur tief", deep_check, deep_only=True),
        ],
    )

    report = await build_readiness_report(settings, mode=ReadinessMode.QUICK)

    assert report.summary.skipped == 1
    assert report.checks[0].status is ReadinessCheckStatus.SKIPPED
    assert report.checks[0].message == "Nur im Tiefencheck aktiv."


def test_catastrophic_report_is_always_renderable(tmp_path) -> None:
    settings = _settings(tmp_path)

    report = build_catastrophic_readiness_report(
        settings,
        mode=ReadinessMode.DEEP,
        exc=ValueError("harter Gesamtfehler"),
    )

    assert report.overall_status is ReadinessCheckStatus.FAIL
    assert report.summary.fail == 1
    assert report.checks[0].severity is ReadinessSeverity.CRITICAL
    assert "ValueError" in report.checks[0].detail
    assert report.mode is ReadinessMode.DEEP


async def test_secrets_files_check_marks_empty_env_values_as_ignored(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MODEL_API_KEY_FILE", "   ")
    settings = _settings(tmp_path)
    ctx = ReadinessContext(settings=settings, mode=ReadinessMode.QUICK, services=ReadinessServices())

    payload = await _secrets_files(ctx)

    assert payload["status"] is ReadinessCheckStatus.OK
    assert payload["raw_value"]["default_model_api_key_file"]["state"] == "empty_ignored"


async def test_secrets_files_check_reports_directory_path_as_failure(tmp_path, monkeypatch) -> None:
    secret_dir = tmp_path / "secret-dir"
    secret_dir.mkdir()
    monkeypatch.setenv("MISTRAL_API_KEY_FILE", str(secret_dir))
    settings = _settings(tmp_path)
    ctx = ReadinessContext(settings=settings, mode=ReadinessMode.QUICK, services=ReadinessServices())

    payload = await _secrets_files(ctx)

    assert payload["status"] is ReadinessCheckStatus.FAIL
    assert payload["raw_value"]["mistral_api_key_file"]["state"] == "directory"
    assert "Verzeichnis" in payload["detail"]
