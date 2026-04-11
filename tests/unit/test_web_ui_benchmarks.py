"""
Purpose: Verify that the worker benchmark page turns persisted task details into readable worker performance summaries.
Input/Output: Tests feed the web UI mocked task payloads and inspect both the HTML page and JSON export.
Important invariants: Benchmarks must stay human-readable, degrade gracefully, and expose structured raw data for follow-up work.
How to debug: If this fails, inspect `services/web_ui/app.py` and compare the fake task payload with the generated benchmark report.
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


def test_benchmark_page_and_export_show_worker_metrics(tmp_path, monkeypatch) -> None:
    app_module = _prepare_web_ui_module(tmp_path, monkeypatch)
    now = datetime.now(UTC).replace(microsecond=0)

    async def fake_api_request(method: str, path: str, *, json_payload=None):
        del method, json_payload
        if path == "/api/tasks":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "task-1",
                        "goal": "Verbessere den Coding-Worker und dokumentiere die Aenderungen.",
                        "repository": "Feberdin/local-multi-agent-company",
                        "local_repo_path": "/workspace/local-multi-agent-company",
                        "base_branch": "main",
                        "branch_name": "feature/task-1",
                        "status": "DONE",
                        "metadata": {},
                        "created_at": now.isoformat(),
                        "updated_at": now.isoformat(),
                    },
                    {
                        "id": "task-2",
                        "goal": "Analysiere eine Research-Stage mit langsamem Modell und Fehlerfall.",
                        "repository": "Feberdin/local-multi-agent-company",
                        "local_repo_path": "/workspace/local-multi-agent-company",
                        "base_branch": "main",
                        "branch_name": "feature/task-2",
                        "status": "FAILED",
                        "metadata": {},
                        "created_at": now.isoformat(),
                        "updated_at": now.isoformat(),
                    },
                ],
            )
        if path == "/api/tasks/task-1":
            return httpx.Response(
                200,
                json={
                    "id": "task-1",
                    "goal": "Verbessere den Coding-Worker und dokumentiere die Aenderungen.",
                    "repository": "Feberdin/local-multi-agent-company",
                    "repo_url": None,
                    "local_repo_path": "/workspace/local-multi-agent-company",
                    "base_branch": "main",
                    "branch_name": "feature/task-1",
                    "status": "DONE",
                    "resume_target": None,
                    "current_approval_gate_name": None,
                    "approval_required": False,
                    "approval_reason": None,
                    "allow_repository_modifications": True,
                    "pull_request_url": None,
                    "latest_error": None,
                    "metadata": {
                        "worker_progress": {
                            "coding": {
                                "state": "complete",
                                "current_instruction": "Setze einen kleinen, sicheren Fix im Coding-Worker um.",
                                "current_prompt_summary": "Coding soll einen kleinen, sicheren Fix umsetzen.",
                                "last_result_summary": "Der Coding-Worker wurde stabilisiert und die Fehlerbehandlung erweitert.",
                                "progress_message": "Coding wurde erfolgreich abgeschlossen.",
                                "elapsed_seconds": 182.0,
                                "started_at": (now - timedelta(minutes=8)).isoformat(),
                                "updated_at": (now - timedelta(minutes=5)).isoformat(),
                                "model_route": {
                                    "provider": "mistral",
                                    "model_name": "mistral-small3.2:latest",
                                    "base_url": "http://mistral.local/v1",
                                    "request_timeout_seconds": 1500,
                                },
                            }
                        }
                    },
                    "created_at": (now - timedelta(minutes=10)).isoformat(),
                    "updated_at": (now - timedelta(minutes=4)).isoformat(),
                    "worker_results": {
                        "coding": {
                            "worker": "coding",
                            "summary": "Coding-Fix umgesetzt.",
                            "success": True,
                            "outputs": {},
                            "artifacts": [{"name": "patch", "path": "/reports/task-1/patch.diff", "description": "Diff"}],
                            "warnings": [],
                            "errors": [],
                            "risk_flags": [],
                        }
                    },
                    "risk_flags": [],
                    "events": [
                        {
                            "id": 1,
                            "task_id": "task-1",
                            "level": "INFO",
                            "stage": "CODING",
                            "message": "Coding abgeschlossen.",
                            "details": {"worker_name": "coding", "event_kind": "stage_completed"},
                            "created_at": (now - timedelta(minutes=5)).isoformat(),
                        }
                    ],
                    "approvals": [],
                    "smoke_checks": [],
                    "deployment": None,
                },
            )
        if path == "/api/tasks/task-2":
            return httpx.Response(
                200,
                json={
                    "id": "task-2",
                    "goal": "Analysiere eine Research-Stage mit langsamem Modell und Fehlerfall.",
                    "repository": "Feberdin/local-multi-agent-company",
                    "repo_url": None,
                    "local_repo_path": "/workspace/local-multi-agent-company",
                    "base_branch": "main",
                    "branch_name": "feature/task-2",
                    "status": "FAILED",
                    "resume_target": None,
                    "current_approval_gate_name": None,
                    "approval_required": False,
                    "approval_reason": None,
                    "allow_repository_modifications": False,
                    "pull_request_url": None,
                    "latest_error": "Research-Worker meldete HTTP 500.",
                    "metadata": {
                        "worker_progress": {
                            "research": {
                                "state": "failed",
                                "current_instruction": "Analysiere Repo-Dateien und valide Quellen ohne zu raten.",
                                "current_prompt_summary": "Research soll Repository und Quellenlage analysieren.",
                                "last_result_summary": "Recherche brach vor dem Abschlussbericht ab.",
                                "progress_message": "Research ist mit einem Worker-Fehler abgebrochen.",
                                "waiting_for": "Antwort des Worker-Services",
                                "last_error": "Research-Worker meldete HTTP 500.",
                                "elapsed_seconds": 421.0,
                                "started_at": (now - timedelta(minutes=12)).isoformat(),
                                "updated_at": (now - timedelta(minutes=4)).isoformat(),
                                "model_route": {
                                    "provider": "qwen",
                                    "model_name": "qwen3.5:35b-a3b",
                                    "base_url": "http://qwen.local/v1",
                                    "request_timeout_seconds": 1500,
                                },
                            }
                        }
                    },
                    "created_at": (now - timedelta(minutes=14)).isoformat(),
                    "updated_at": (now - timedelta(minutes=4)).isoformat(),
                    "worker_results": {
                        "research": {
                            "worker": "research",
                            "summary": "Repository research failed before the report could be completed.",
                            "success": False,
                            "outputs": {},
                            "artifacts": [],
                            "warnings": ["Nur Teilkontext verfuegbar."],
                            "errors": ["Worker at http://research-worker:8091 returned HTTP 500."],
                            "risk_flags": ["research-runtime"],
                        }
                    },
                    "risk_flags": ["research-runtime"],
                    "events": [
                        {
                            "id": 2,
                            "task_id": "task-2",
                            "level": "ERROR",
                            "stage": "RESEARCHING",
                            "message": "Research fehlgeschlagen.",
                            "details": {"worker_name": "research", "event_kind": "stage_failed"},
                            "created_at": (now - timedelta(minutes=4)).isoformat(),
                        }
                    ],
                    "approvals": [],
                    "smoke_checks": [],
                    "deployment": None,
                },
            )
        raise AssertionError(f"Unexpected path: {path}")

    app_module._api_request = fake_api_request

    with TestClient(app_module.app) as client:
        page_response = client.get("/benchmarks")
        export_response = client.get("/benchmarks/export.json")

    assert page_response.status_code == 200
    assert "Worker-Benchmarks" in page_response.text
    assert "Coding" in page_response.text
    assert "Recherche" in page_response.text
    assert "mistral / mistral-small3.2:latest" in page_response.text
    assert "qwen / qwen3.5:35b-a3b" in page_response.text
    assert "sichtbarer Input" in page_response.text

    assert export_response.status_code == 200
    payload = export_response.json()
    assert payload["total_tasks"] == 2
    assert payload["total_runs"] >= 2
    coding_summary = next(item for item in payload["worker_summaries"] if item["worker_name"] == "coding")
    research_summary = next(item for item in payload["worker_summaries"] if item["worker_name"] == "research")
    assert coding_summary["run_count"] == 1
    assert research_summary["failed_count"] == 1
