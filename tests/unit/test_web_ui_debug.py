"""
Purpose: Verify that the web UI debug center exposes reproducible downloads for system state and task artifacts.
Input/Output: Tests mount temporary data and report directories, then inspect HTML, JSON downloads, and ZIP bundles.
Important invariants: Missing Docker-host logs are explained, task reports are bundled, and exports stay available even for failures.
How to debug: If this fails, inspect the debug routes in services/web_ui/app.py
and compare the generated bundle names with the template links.
"""

from __future__ import annotations

import importlib
import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

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


def _install_fake_api(app_module, now: str) -> None:
    async def fake_api_request(method: str, path: str, *, json_payload=None):
        del method, json_payload
        if path == "/health":
            return httpx.Response(200, json={"service": "orchestrator", "status": "ok"})
        if path == "/api/tasks":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "task-1",
                        "goal": "Analyse the repository and collect useful debug artifacts.",
                        "repository": "Feberdin/local-multi-agent-company",
                        "local_repo_path": "/workspace/local-multi-agent-company",
                        "base_branch": "main",
                        "branch_name": "feature/task-1",
                        "status": "FAILED",
                        "resume_target": None,
                        "current_approval_gate_name": None,
                        "approval_required": False,
                        "approval_reason": None,
                        "allow_repository_modifications": False,
                        "pull_request_url": None,
                        "latest_error": "requirements timeout",
                        "metadata": {},
                        "created_at": now,
                        "updated_at": now,
                        "worker_results": {},
                        "risk_flags": [],
                        "events": [],
                        "approvals": [],
                        "smoke_checks": [],
                        "deployment": None,
                    }
                ],
            )
        if path == "/api/tasks/task-1":
            return httpx.Response(
                200,
                json={
                    "id": "task-1",
                    "goal": "Analyse the repository and collect useful debug artifacts.",
                    "repository": "Feberdin/local-multi-agent-company",
                    "local_repo_path": "/workspace/local-multi-agent-company",
                    "base_branch": "main",
                    "branch_name": "feature/task-1",
                    "status": "FAILED",
                    "resume_target": None,
                    "current_approval_gate_name": None,
                    "approval_required": False,
                    "approval_reason": None,
                    "allow_repository_modifications": False,
                    "pull_request_url": None,
                    "latest_error": "requirements timeout",
                    "metadata": {"worker_project_label": "Feberdin worker project"},
                    "created_at": now,
                    "updated_at": now,
                    "worker_results": {
                        "requirements": {
                            "summary": "Requirements timed out while waiting for a local model.",
                            "outputs": {"acceptance_criteria": ["Readable debug bundle"]},
                            "warnings": [],
                            "errors": ["ReadTimeout"],
                            "risk_flags": ["timeout"],
                            "artifacts": [],
                        }
                    },
                    "risk_flags": ["timeout"],
                    "events": [
                        {
                            "id": 1,
                            "task_id": "task-1",
                            "level": "INFO",
                            "stage": "REQUIREMENTS",
                            "message": "Requirements gestartet.",
                            "details": {"worker_name": "requirements", "event_kind": "stage_started"},
                            "created_at": now,
                        },
                        {
                            "id": 2,
                            "task_id": "task-1",
                            "level": "ERROR",
                            "stage": "REQUIREMENTS",
                            "message": "ReadTimeout beim lokalen Modell.",
                            "details": {"worker_name": "requirements", "event_kind": "stage_failed"},
                            "created_at": now,
                        },
                    ],
                    "approvals": [],
                    "smoke_checks": [],
                    "deployment": None,
                },
            )
        if path == "/api/settings/repository-access":
            return httpx.Response(200, json={"allowed_repositories": ["Feberdin/local-multi-agent-company"]})
        if path == "/api/settings/trusted-sources":
            return httpx.Response(200, json={"profiles": [], "active_profile_id": None})
        if path == "/api/settings/web-search":
            return httpx.Response(
                200,
                json={
                    "providers": [],
                    "primary_web_search_provider": "",
                    "fallback_web_search_provider": "",
                    "require_trusted_sources_first": True,
                    "allow_general_web_search_fallback": False,
                    "provider_host_allowlist": [],
                },
            )
        if path == "/api/settings/worker-guidance":
            return httpx.Response(200, json={"workers": []})
        if path == "/api/suggestions/registry":
            return httpx.Response(
                200,
                json={
                    "suggestions": [
                        {
                            "id": "suggestion-1",
                            "worker_name": "reviewer",
                            "task_id": "task-1",
                            "title": "Requirements timeout analysieren",
                            "summary": "Timeouts und Heartbeats enger sichtbar machen.",
                            "rationale": "Die Aufgabe endete ohne ausreichend sichtbare Diagnose.",
                            "suggested_action": "Debug-Center-Bundle pruefen.",
                            "status": "pending",
                            "created_at": now,
                            "updated_at": now,
                        }
                    ]
                },
            )
        raise AssertionError(f"Unexpected path: {path}")

    app_module._api_request = fake_api_request


def _create_debug_files(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    reports_dir = tmp_path / "reports" / "task-1"
    data_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    (data_dir / "repository-access-policy.json").write_text(
        json.dumps({"allowed_repositories": ["Feberdin/local-multi-agent-company"]}, indent=2),
        encoding="utf-8",
    )
    (data_dir / "worker_guidance.json").write_text(json.dumps({"workers": []}, indent=2), encoding="utf-8")
    (reports_dir / "requirements.json").write_text(json.dumps({"summary": "timed out"}, indent=2), encoding="utf-8")
    (reports_dir / "research-notes.md").write_text("# Notes\n\nFollow-up required.\n", encoding="utf-8")


def test_debug_center_page_lists_downloads_and_reports(tmp_path, monkeypatch) -> None:
    app_module = _prepare_web_ui_module(tmp_path, monkeypatch)
    now = datetime.now(UTC).isoformat()
    _create_debug_files(tmp_path)
    _install_fake_api(app_module, now)

    with TestClient(app_module.app) as client:
        response = client.get("/debug?task_id=task-1")

    assert response.status_code == 200
    assert "Debug-Center" in response.text
    assert "System-Bundle herunterladen" in response.text
    assert "/debug/tasks/task-1/bundle.zip" in response.text
    assert "repository-access-policy.json" in response.text
    assert "requirements.json" in response.text


def test_task_debug_bundle_contains_snapshots_and_reports(tmp_path, monkeypatch) -> None:
    app_module = _prepare_web_ui_module(tmp_path, monkeypatch)
    now = datetime.now(UTC).isoformat()
    _create_debug_files(tmp_path)
    _install_fake_api(app_module, now)

    with TestClient(app_module.app) as client:
        response = client.get("/debug/tasks/task-1/bundle.zip")

    assert response.status_code == 200
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    names = sorted(archive.namelist())
    assert "tasks/task-1/task-detail.json" in names
    assert "tasks/task-1/task-ui-state.json" in names
    assert "tasks/task-1/task-suggestions.json" in names
    assert "tasks/task-1/reports/requirements.json" in names
    assert "tasks/task-1/reports/research-notes.md" in names

    suggestions_payload = json.loads(archive.read("tasks/task-1/task-suggestions.json").decode("utf-8"))
    assert suggestions_payload["payload"]["count"] == 1


def test_combined_debug_bundle_contains_system_and_task_sections(tmp_path, monkeypatch) -> None:
    app_module = _prepare_web_ui_module(tmp_path, monkeypatch)
    now = datetime.now(UTC).isoformat()
    _create_debug_files(tmp_path)
    _install_fake_api(app_module, now)

    with TestClient(app_module.app) as client:
        response = client.get("/debug/bundle.zip?task_id=task-1")

    assert response.status_code == 200
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    names = sorted(archive.namelist())
    assert "system/tasks.json" in names
    assert "system/runtime-summary.json" in names
    assert "system/persisted/repository-access-policy.json" in names
    assert "tasks/task-1/task-worker-results.json" in names
    assert "tasks/task-1/reports/requirements.json" in names
