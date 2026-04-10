"""
Purpose: Verify that the web UI presents long-running task progress in a readable, operator-friendly way.
Input/Output: Tests feed raw task payloads into the UI decoration helpers and inspect the derived timeline state.
Important invariants: Slow stages must look active instead of frozen, and heartbeat events must remain visible.
How to debug: If this fails, inspect `services/web_ui/app.py` and compare the derived context with the stored task events.
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta


def test_decorate_task_marks_long_running_requirements_stage_as_active(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("STAGING_STACK_ROOT", str(tmp_path / "staging-stacks"))
    from services.shared.agentic_lab.config import get_settings

    get_settings.cache_clear()
    app_module = importlib.import_module("services.web_ui.app")
    app_module = importlib.reload(app_module)

    now = datetime.now(UTC)
    task = {
        "id": "task-1",
        "goal": "Analyse the repository and keep the operator informed during slow local inference.",
        "repository": "Feberdin/local-multi-agent-company",
        "local_repo_path": "/workspace/local-multi-agent-company",
        "base_branch": "main",
        "branch_name": "feature/task-1",
        "status": "REQUIREMENTS",
        "resume_target": None,
        "current_approval_gate_name": None,
        "approval_required": False,
        "approval_reason": None,
        "allow_repository_modifications": False,
        "pull_request_url": None,
        "latest_error": None,
        "metadata": {},
        "created_at": (now - timedelta(minutes=6)).isoformat(),
        "updated_at": (now - timedelta(seconds=20)).isoformat(),
        "worker_results": {},
        "risk_flags": [],
        "events": [
            {
                "id": 1,
                "task_id": "task-1",
                "level": "INFO",
                "stage": "REQUIREMENTS",
                "message": "Requirements gestartet.",
                "details": {"worker_name": "requirements"},
                "created_at": (now - timedelta(minutes=5)).isoformat(),
            },
            {
                "id": 2,
                "task_id": "task-1",
                "level": "INFO",
                "stage": "REQUIREMENTS",
                "message": "Requirements laeuft weiter.",
                "details": {"worker_name": "requirements", "heartbeat": True},
                "created_at": (now - timedelta(seconds=30)).isoformat(),
            },
        ],
        "approvals": [],
        "smoke_checks": [],
        "deployment": None,
    }

    decorated = app_module._decorate_task(task)

    assert decorated["is_active"] is True
    assert decorated["current_worker_name"] == "requirements"
    assert decorated["current_stage_label"] == "Requirements"
    assert decorated["worker_timeline"][0]["state"] == "running"
    assert decorated["events"][-1]["is_heartbeat"] is True
    assert decorated["auto_refresh_seconds"] > 0
