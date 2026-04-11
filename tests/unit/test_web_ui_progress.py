"""
Purpose: Verify that the web UI presents long-running task progress in a readable, operator-friendly way.
Input/Output: Tests feed raw task payloads into the UI decoration helpers and inspect the derived timeline state.
Important invariants: Slow stages must look active instead of frozen, and heartbeat events must remain visible.
How to debug: If this fails, inspect `services/web_ui/app.py` and compare the derived context with the stored task events.
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta

import httpx
from fastapi.testclient import TestClient


def _prepare_web_ui_module(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("STAGING_STACK_ROOT", str(tmp_path / "staging-stacks"))
    from services.shared.agentic_lab.config import get_settings

    get_settings.cache_clear()
    app_module = importlib.import_module("services.web_ui.app")
    return importlib.reload(app_module)


def test_decorate_task_marks_long_running_requirements_stage_as_active(tmp_path, monkeypatch) -> None:
    app_module = _prepare_web_ui_module(tmp_path, monkeypatch)

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
    assert decorated["current_worker_label"] == "Requirements"
    assert decorated["current_stage_label"] == "Requirements"
    assert decorated["worker_timeline"][0]["state"] == "running"
    assert decorated["worker_cast"][0]["bubble_kind"] == "thought"
    assert decorated["events"][-1]["is_heartbeat"] is True
    assert decorated["auto_refresh_seconds"] > 0


def test_task_detail_page_handles_sparse_runtime_payloads_without_500(tmp_path, monkeypatch) -> None:
    app_module = _prepare_web_ui_module(tmp_path, monkeypatch)
    now = datetime.now(UTC).isoformat()

    async def fake_api_request(method: str, path: str, *, json_payload=None):
        if path == "/api/tasks/task-1":
            return httpx.Response(
                200,
                json={
                    "id": "task-1",
                    "goal": "Keep the dashboard readable even if some persisted runtime data is sparse.",
                    "repository": "Feberdin/local-multi-agent-company",
                    "repo_url": None,
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
                    "metadata": None,
                    "created_at": now,
                    "updated_at": now,
                    "worker_results": {"requirements": "Noch kein strukturierter Result-Block vorhanden."},
                    "risk_flags": None,
                    "events": [
                        {
                            "id": 1,
                            "task_id": "task-1",
                            "level": "INFO",
                            "stage": "REQUIREMENTS",
                            "message": "Requirements laeuft noch.",
                            "details": None,
                            "created_at": now,
                        }
                    ],
                    "approvals": [],
                    "smoke_checks": [],
                    "deployment": None,
                },
            )
        if path == "/api/suggestions/registry":
            return httpx.Response(
                200,
                json={
                    "suggestions": [
                        {
                            "id": "suggestion-1",
                            "worker_name": "research",
                            "task_id": "task-1",
                            "title": "Repo-Walkthrough zuerst sichern",
                            "summary": "Ein kurzer Architektur-Ueberblick spart spaeter Recherchezeit.",
                            "rationale": "Der Requirements-Worker hat noch keinen stabilen Ergebnisblock geliefert.",
                            "suggested_action": "Kurz die wichtigsten Module notieren.",
                            "status": "pending",
                            "created_at": now,
                            "updated_at": now,
                        }
                    ]
                },
            )
        raise AssertionError(f"Unexpected path: {path}")

    app_module._api_request = fake_api_request

    with TestClient(app_module.app) as client:
        response = client.get("/tasks/task-1")

    assert response.status_code == 200
    assert "Worker-Theater" in response.text
    assert "Requirements laeuft noch." in response.text
    assert "Repo-Walkthrough zuerst sichern" in response.text
