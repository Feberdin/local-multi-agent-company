"""
Purpose: Verify that the web UI presents long-running task progress in a readable, operator-friendly way.
Input/Output: Tests feed raw task payloads into the UI decoration helpers and inspect the derived timeline state.
Important invariants: Slow stages must look active instead of frozen, and heartbeat events must remain visible.
How to debug: If this fails, inspect `services/web_ui/app.py` and compare the derived context with the stored task events.
"""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime, timedelta

import httpx
from fastapi.testclient import TestClient


def _prepare_web_ui_module(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("STAGING_STACK_ROOT", str(tmp_path / "staging-stacks"))
    monkeypatch.setenv("UI_TIMEZONE", "Europe/Berlin")
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
        "metadata": {
            "worker_progress": {
                "requirements": {
                    "state": "waiting",
                    "current_instruction": "Strukturiere Anforderungen und warte auf das lokale Modell.",
                    "waiting_for": "Lokales Modell",
                    "progress_message": "Anforderungen warten auf Modellantwort.",
                    "elapsed_seconds": 30.0,
                    "started_at": (now - timedelta(minutes=5)).isoformat(),
                    "updated_at": (now - timedelta(seconds=20)).isoformat(),
                }
            }
        },
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
    assert decorated["current_worker_label"] == "Anforderungen"
    assert decorated["current_stage_label"] == "Anforderungen"
    assert decorated["current_stage_state"] == "waiting"
    assert decorated["worker_timeline"][0]["state"] == "waiting"
    assert decorated["worker_cast"][0]["bubble_kind"] == "coffee"
    assert decorated["worker_cast_groups"][1]["workers"][0]["worker_name"] == "requirements"
    assert decorated["current_instruction"] == "Strukturiere Anforderungen und warte auf das lokale Modell."
    assert decorated["events"][-1]["is_heartbeat"] is True
    assert decorated["auto_refresh_seconds"] > 0
    assert decorated["can_restart_partially"] is True
    assert decorated["restartable_stage_options"][0]["worker_name"] == "requirements"


def test_dashboard_shows_version_badge_in_top_navigation(tmp_path, monkeypatch) -> None:
    app_module = _prepare_web_ui_module(tmp_path, monkeypatch)
    app_module.templates.env.globals["ui_build"] = {
        "app_version": "9.9.9",
        "git_sha": "deadbeefcafe",
        "git_branch": "main",
        "repo_path": "/app",
        "build_timestamp_utc": "2026-04-13T17:02:19Z",
        "build_timestamp_display": "Mo 13.04.2026 19:02:19 CEST",
        "build_commit_sha": "deadbeefcafe",
        "build_git_ref": "main",
        "display": "Version 9.9.9 · deadbeefcafe · Build Mo 13.04.2026 19:02:19 CEST",
        "full_label": (
            "Version 9.9.9 · Branch main · Laufender Commit deadbeefcafe · "
            "Build-Ref main · Build-Commit deadbeefcafe · Gebaut Mo 13.04.2026 19:02:19 CEST · Repo /app"
        ),
    }

    async def fake_api_request(method: str, path: str, *, json_payload=None):
        if path in {"/api/tasks", "/api/tasks?only_archived=true", "/api/suggestions"}:
            return httpx.Response(200, json=[])
        if path == "/api/settings/repository-access":
            return httpx.Response(200, json={"allowed_repositories": []})
        raise AssertionError(f"Unexpected path: {path}")

    app_module._api_request = fake_api_request

    with TestClient(app_module.app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "Version 9.9.9 · deadbeefcafe · Build Mo 13.04.2026 19:02:19 CEST" in response.text


def test_ui_build_info_reads_baked_build_metadata_and_formats_local_time(tmp_path, monkeypatch) -> None:
    build_info_path = tmp_path / "build-info.json"
    build_info_path.write_text(
        json.dumps(
            {
                "build_commit_sha": "deadbeefcafe",
                "build_git_ref": "main",
                "build_timestamp_utc": "2026-04-13T17:02:19Z",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("FEBERDIN_BUILD_INFO_PATH", str(build_info_path))
    app_module = _prepare_web_ui_module(tmp_path, monkeypatch)

    def fake_git_metadata(_repo_path, *args):
        if args == ("rev-parse", "--short=12", "HEAD"):
            return "deadbeefcafe"
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return "main"
        return None

    monkeypatch.setattr(app_module, "_run_git_metadata_command", fake_git_metadata)
    app_module._ui_build_info.cache_clear()

    info = app_module._ui_build_info()

    assert info["git_sha"] == "deadbeefcafe"
    assert info["build_commit_sha"] == "deadbeefcafe"
    assert info["build_git_ref"] == "main"
    assert info["build_timestamp_display"] == "Mo 13.04.2026 19:02:19 CEST"
    assert info["display"] == "Version 0.1.0 · deadbeefcafe · Build Mo 13.04.2026 19:02:19 CEST"


def test_ui_build_info_highlights_when_running_repo_head_differs_from_built_image(tmp_path, monkeypatch) -> None:
    build_info_path = tmp_path / "build-info.json"
    build_info_path.write_text(
        json.dumps(
            {
                "build_commit_sha": "built12345678",
                "build_git_ref": "main",
                "build_timestamp_utc": "2026-04-13T17:02:19Z",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("FEBERDIN_BUILD_INFO_PATH", str(build_info_path))
    app_module = _prepare_web_ui_module(tmp_path, monkeypatch)

    def fake_git_metadata(_repo_path, *args):
        if args == ("rev-parse", "--short=12", "HEAD"):
            return "live99999999"
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return "main"
        return None

    monkeypatch.setattr(app_module, "_run_git_metadata_command", fake_git_metadata)
    app_module._ui_build_info.cache_clear()

    info = app_module._ui_build_info()

    assert info["build_commit_sha"] == "built12345678"
    assert info["git_sha"] == "live99999999"
    assert info["build_mismatch"] == "true"
    assert "Host live99999999" in info["display"]
    assert "Warnung Host-Checkout und laufender Build unterscheiden sich" in info["full_label"]


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


def test_decorate_task_normalizes_naive_timestamps_from_older_rows(tmp_path, monkeypatch) -> None:
    app_module = _prepare_web_ui_module(tmp_path, monkeypatch)
    now = datetime(2026, 1, 12, 12, 0, 0, tzinfo=UTC)
    naive_now = now.replace(tzinfo=None)

    task = {
        "id": "task-naive",
        "goal": "Handle older persisted timestamps without crashing the detail page.",
        "repository": "Feberdin/local-multi-agent-company",
        "local_repo_path": "/workspace/local-multi-agent-company",
        "base_branch": "main",
        "branch_name": "feature/task-naive",
        "status": "REQUIREMENTS",
        "resume_target": None,
        "current_approval_gate_name": None,
        "approval_required": False,
        "approval_reason": None,
        "allow_repository_modifications": False,
        "pull_request_url": None,
        "latest_error": None,
        "metadata": {},
        "created_at": naive_now.isoformat(),
        "updated_at": naive_now.isoformat(),
        "worker_results": {},
        "risk_flags": [],
        "events": [
            {
                "id": 1,
                "task_id": "task-naive",
                "level": "INFO",
                "stage": "REQUIREMENTS",
                "message": "Requirements started from an older row.",
                "details": {"worker_name": "requirements"},
                "created_at": naive_now.isoformat(),
            }
        ],
        "approvals": [],
        "smoke_checks": [],
        "deployment": None,
    }

    decorated = app_module._decorate_task(task)

    assert decorated["is_active"] is True
    assert decorated["running_for_display"] != "unbekannt"
    assert decorated["running_since_display"] == "2026-01-12 13:00:00 CET"


def test_format_timestamp_uses_configured_ui_timezone(tmp_path, monkeypatch) -> None:
    app_module = _prepare_web_ui_module(tmp_path, monkeypatch)

    formatted = app_module._format_timestamp("2026-04-12T19:51:00+00:00")

    assert formatted == "2026-04-12 21:51:00 CEST"


def test_task_detail_fallback_surfaces_exception_details(tmp_path, monkeypatch) -> None:
    app_module = _prepare_web_ui_module(tmp_path, monkeypatch)

    async def fake_api_request(method: str, path: str, *, json_payload=None):
        if path == "/api/tasks/task-1":
            raise TypeError("simulated detail payload bug")
        if path == "/api/tasks":
            return httpx.Response(200, json=[])
        if path == "/api/tasks?only_archived=true":
            return httpx.Response(200, json=[])
        if path == "/api/settings/repository-access":
            return httpx.Response(200, json={"allowed_repositories": []})
        if path == "/api/suggestions":
            return httpx.Response(200, json=[])
        raise AssertionError(f"Unexpected path: {path}")

    app_module._api_request = fake_api_request

    with TestClient(app_module.app) as client:
        response = client.get("/tasks/task-1")

    assert response.status_code == 200
    assert "TypeError: simulated detail payload bug" in response.text
    assert "docker logs --tail=200 fmac-web" in response.text


def test_restart_stage_form_posts_selected_worker_name(tmp_path, monkeypatch) -> None:
    app_module = _prepare_web_ui_module(tmp_path, monkeypatch)
    captured: dict[str, object] = {}

    async def fake_api_request(method: str, path: str, *, json_payload=None):
        captured["method"] = method
        captured["path"] = path
        captured["json_payload"] = json_payload
        return httpx.Response(200, json={"ok": True})

    app_module._api_request = fake_api_request

    with TestClient(app_module.app) as client:
        response = client.post(
            "/tasks/task-1/restart-stage",
            data={"worker_name": "research", "reason": "Git-Fehler wurde behoben."},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/tasks/task-1"
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/tasks/task-1/restart-stage"
    assert captured["json_payload"] == {
        "worker_name": "research",
        "actor": "dashboard",
        "reason": "Git-Fehler wurde behoben.",
        "run_immediately": True,
    }


def test_dashboard_hides_archived_tasks_by_default_and_links_to_dedicated_archive_page(tmp_path, monkeypatch) -> None:
    app_module = _prepare_web_ui_module(tmp_path, monkeypatch)
    now = datetime.now(UTC).isoformat()

    async def fake_api_request(method: str, path: str, *, json_payload=None):
        del method, json_payload
        if path == "/api/tasks":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "task-visible",
                        "goal": "Aktive Aufgabe bleibt im Dashboard sichtbar.",
                        "repository": "Feberdin/local-multi-agent-company",
                        "local_repo_path": "/workspace/local-multi-agent-company",
                        "base_branch": "main",
                        "branch_name": "feature/task-visible",
                        "status": "DONE",
                        "metadata": {},
                        "created_at": now,
                        "updated_at": now,
                    }
                ],
            )
        if path == "/api/tasks?only_archived=true":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "task-archived",
                        "goal": "Archivierte Aufgabe soll nur auf Wunsch sichtbar sein.",
                        "repository": "Feberdin/local-multi-agent-company",
                        "local_repo_path": "/workspace/local-multi-agent-company",
                        "base_branch": "main",
                        "branch_name": "feature/task-archived",
                        "status": "DONE",
                        "archived": True,
                        "archived_at": now,
                        "archived_reason": "Altlast",
                        "metadata": {"archived": True, "archived_at": now, "archived_reason": "Altlast"},
                        "created_at": now,
                        "updated_at": now,
                    }
                ],
            )
        if path == "/api/settings/repository-access":
            return httpx.Response(200, json={"allowed_repositories": []})
        if path == "/api/suggestions":
            return httpx.Response(200, json=[])
        raise AssertionError(f"Unexpected path: {path}")

    app_module._api_request = fake_api_request

    with TestClient(app_module.app) as client:
        default_response = client.get("/")
        archive_response = client.get("/archive")

    assert default_response.status_code == 200
    assert "Aktive Aufgabe bleibt im Dashboard sichtbar." in default_response.text
    assert "Archivierte Aufgabe soll nur auf Wunsch sichtbar sein." not in default_response.text
    assert 'href="/archive"' in default_response.text
    assert archive_response.status_code == 200
    assert "Task-Archiv" in archive_response.text
    assert "Archivierte Aufgabe soll nur auf Wunsch sichtbar sein." in archive_response.text
    assert "Wiederherstellen" in archive_response.text


def test_task_archive_form_posts_dashboard_payload(tmp_path, monkeypatch) -> None:
    app_module = _prepare_web_ui_module(tmp_path, monkeypatch)
    captured: dict[str, object] = {}

    async def fake_api_request(method: str, path: str, *, json_payload=None):
        captured["method"] = method
        captured["path"] = path
        captured["json_payload"] = json_payload
        return httpx.Response(200, json={"ok": True})

    app_module._api_request = fake_api_request

    with TestClient(app_module.app) as client:
        response = client.post(
            "/tasks/task-1/archive",
            data={"reason": "Alte Aufgabe aufraeumen."},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/tasks/task-1"
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/tasks/task-1/archive"
    assert captured["json_payload"] == {
        "actor": "dashboard",
        "reason": "Alte Aufgabe aufraeumen.",
    }


def test_task_restore_form_posts_dashboard_payload(tmp_path, monkeypatch) -> None:
    app_module = _prepare_web_ui_module(tmp_path, monkeypatch)
    captured: dict[str, object] = {}

    async def fake_api_request(method: str, path: str, *, json_payload=None):
        captured["method"] = method
        captured["path"] = path
        captured["json_payload"] = json_payload
        return httpx.Response(200, json={"ok": True})

    app_module._api_request = fake_api_request

    with TestClient(app_module.app) as client:
        response = client.post(
            "/tasks/task-1/restore",
            data={"reason": "Bitte wieder sichtbar machen."},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/archive"
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/tasks/task-1/restore"
    assert captured["json_payload"] == {
        "actor": "dashboard",
        "reason": "Bitte wieder sichtbar machen.",
    }


def test_archive_page_highlights_requested_archived_task(tmp_path, monkeypatch) -> None:
    app_module = _prepare_web_ui_module(tmp_path, monkeypatch)
    now = datetime.now(UTC).isoformat()

    async def fake_api_request(method: str, path: str, *, json_payload=None):
        del method, json_payload
        if path == "/api/tasks?only_archived=true":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "task-archived",
                        "goal": "Archivierte Aufgabe fuer den History-Link.",
                        "repository": "Feberdin/local-multi-agent-company",
                        "local_repo_path": "/workspace/local-multi-agent-company",
                        "base_branch": "main",
                        "branch_name": "feature/task-archived",
                        "status": "FAILED",
                        "archived": True,
                        "archived_at": now,
                        "archived_reason": "Bereits untersucht",
                        "metadata": {"archived": True, "archived_at": now, "archived_reason": "Bereits untersucht"},
                        "created_at": now,
                        "updated_at": now,
                    }
                ],
            )
        raise AssertionError(f"Unexpected path: {path}")

    app_module._api_request = fake_api_request

    with TestClient(app_module.app) as client:
        response = client.get("/archive?task_id=task-archived")

    assert response.status_code == 200
    assert "Diese Aufgabe wurde aus einer Historienansicht markiert." in response.text


def test_self_improvement_page_marks_archived_task_references_as_archive_links(tmp_path, monkeypatch) -> None:
    app_module = _prepare_web_ui_module(tmp_path, monkeypatch)
    now = datetime.now(UTC).isoformat()

    async def fake_api_request(method: str, path: str, *, json_payload=None):
        del method, json_payload
        if path == "/api/self-improvement/status":
            return httpx.Response(
                200,
                json={
                    "enabled": True,
                    "can_start": True,
                    "daily_cycle_count": 1,
                    "max_cycles_per_day": 5,
                    "mode": "assisted",
                    "open_incident_count": 0,
                    "active_cycle": None,
                    "last_cycle": None,
                    "pending_review_cycles": [],
                },
            )
        if path == "/api/self-improvement/cycles":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "cycle-1",
                        "cycle_number": 11,
                        "status": "failed",
                        "trigger": "manual",
                        "task_id": "task-archived",
                        "risk_level": "low",
                        "retry_count": 0,
                        "max_retries": 3,
                        "started_at": now,
                        "completed_at": now,
                        "goal": "Historischen Self-Improvement-Lauf anzeigen.",
                    }
                ],
            )
        if path == "/api/settings/self-improvement":
            return httpx.Response(200, json={"mode": "assisted", "normalized_mode": "assisted"})
        if path == "/api/settings/self-improvement/policy":
            return httpx.Response(200, json={"repository": "Feberdin/local-multi-agent-company", "mode_rules": {}})
        if path == "/api/self-improvement/incidents":
            return httpx.Response(200, json=[])
        if path == "/api/tasks/task-archived":
            return httpx.Response(
                200,
                json={
                    "id": "task-archived",
                    "goal": "Archivierte historische Aufgabe.",
                    "repository": "Feberdin/local-multi-agent-company",
                    "local_repo_path": "/workspace/local-multi-agent-company",
                    "base_branch": "main",
                    "branch_name": "feature/task-archived",
                    "status": "FAILED",
                    "metadata": {"archived": True, "archived_at": now, "archived_reason": "Altlast"},
                    "archived": True,
                    "archived_at": now,
                    "archived_reason": "Altlast",
                    "created_at": now,
                    "updated_at": now,
                    "worker_results": {},
                    "risk_flags": [],
                    "events": [],
                    "approvals": [],
                    "smoke_checks": [],
                    "deployment": None,
                    "resume_target": None,
                    "current_approval_gate_name": None,
                    "approval_required": False,
                    "approval_reason": None,
                    "allow_repository_modifications": True,
                    "pull_request_url": None,
                    "latest_error": None,
                },
            )
        raise AssertionError(f"Unexpected path: {path}")

    app_module._api_request = fake_api_request

    with TestClient(app_module.app) as client:
        response = client.get("/self-improvement")

    assert response.status_code == 200
    assert "archiviert" in response.text
    assert '/archive?task_id=task-archived#task-task-archived' in response.text
    assert '/tasks/task-archived' not in response.text


def test_normalize_suggestions_shows_repository_wide_statuses_readably(tmp_path, monkeypatch) -> None:
    app_module = _prepare_web_ui_module(tmp_path, monkeypatch)
    now = datetime.now(UTC).isoformat()

    suggestions = app_module._normalize_suggestions(
        [
            {
                "id": "suggestion-1",
                "worker_name": "architecture",
                "task_id": "task-1",
                "repository": "Feberdin/local-multi-agent-company",
                "fingerprint": "abc123def4567890",
                "title": "Repository-spezifische Governance dokumentieren",
                "summary": "Die Repo-Governance ist bereits dokumentiert.",
                "rationale": "Historischer Testdatensatz.",
                "suggested_action": "Keine weitere Aktion noetig.",
                "status": "rejected",
                "scope": "repository_wide",
                "decision_note": "Bereits bewusst unterdrueckt.",
                "created_at": now,
                "updated_at": now,
            }
        ]
    )

    assert suggestions[0]["status"] == "dismissed"
    assert suggestions[0]["status_label"] == "Repo-weit verworfen"
    assert suggestions[0]["scope_label"] == "Ganzer Repository-Kontext"
    assert suggestions[0]["decision_note_display"] == "Bereits bewusst unterdrueckt."


def test_worker_guidance_form_posts_new_field_names(tmp_path, monkeypatch) -> None:
    app_module = _prepare_web_ui_module(tmp_path, monkeypatch)
    captured: dict[str, object] = {}

    async def fake_api_request(method: str, path: str, *, json_payload=None):
        captured["method"] = method
        captured["path"] = path
        captured["json_payload"] = json_payload
        return httpx.Response(200, json={"workers": []})

    app_module._api_request = fake_api_request

    with TestClient(app_module.app) as client:
        response = client.post(
            "/settings/worker-guidance",
            data={
                "worker_name": "coding",
                "display_name": "Coding Worker",
                "enabled": "true",
                "role_description": "Setzt minimal-invasive, sichere und nachvollziehbare Änderungen im freigegebenen Scope um.",
                "operator_recommendations_text": "Minimiere den Diff.\nArbeite schrittweise.",
                "decision_preferences_text": "Kleine sichere Schritte vor Big Bang.",
                "competence_boundary": "Keine Scope-Erweiterungen ohne Freigabe.",
                "escalate_out_of_scope": "true",
                "auto_submit_suggestions": "true",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/worker-guidance"
    assert captured["method"] == "PUT"
    assert captured["path"] == "/api/settings/worker-guidance/coding"
    assert captured["json_payload"] == {
        "worker_name": "coding",
        "display_name": "Coding Worker",
        "enabled": True,
        "role_description": "Setzt minimal-invasive, sichere und nachvollziehbare Änderungen im freigegebenen Scope um.",
        "operator_recommendations": ["Minimiere den Diff.", "Arbeite schrittweise."],
        "decision_preferences": ["Kleine sichere Schritte vor Big Bang."],
        "competence_boundary": "Keine Scope-Erweiterungen ohne Freigabe.",
        "escalate_out_of_scope": True,
        "auto_submit_suggestions": True,
    }
