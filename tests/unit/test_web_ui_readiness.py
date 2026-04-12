"""
Purpose: Verify that the readiness dashboard renders a structured report and keeps the browser on
the same page instead of degrading into a generic error view.
Input/Output: Tests stub the readiness context returned by the web UI backend and inspect the
rendered HTML plus the JSON export endpoint.
Important invariants: Warnings and failures stay visible, worker groups remain readable, and the
JSON export keeps the full report payload available for operators.
How to debug: If this fails, inspect `services/web_ui/templates/readiness.html`,
`services/web_ui/templates/readiness_panel.html`, and the readiness context builder in the web UI app.
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime

from fastapi.testclient import TestClient


def _prepare_web_ui_module(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("STAGING_STACK_ROOT", str(tmp_path / "staging-stacks"))
    monkeypatch.setenv("WEB_UI_INTERNAL_URL", "http://web-ui:8088")
    from services.shared.agentic_lab.config import get_settings

    get_settings.cache_clear()
    app_module = importlib.import_module("services.web_ui.app")
    return importlib.reload(app_module)


def _sample_report(now: str) -> dict:
    return {
        "mode": "quick",
        "overall_status": "warning",
        "started_at": now,
        "finished_at": now,
        "duration_ms": 1650.0,
        "summary": {"total": 3, "ok": 1, "warning": 1, "fail": 1, "skipped": 0, "running": 0},
        "categories": [
            {
                "id": "backend",
                "label": "Backend / Core",
                "status": "fail",
                "total": 1,
                "ok": 0,
                "warning": 0,
                "fail": 1,
                "skipped": 0,
                "running": 0,
            },
            {
                "id": "workers",
                "label": "Worker",
                "status": "warning",
                "total": 1,
                "ok": 0,
                "warning": 1,
                "fail": 0,
                "skipped": 0,
                "running": 0,
            },
            {
                "id": "llm",
                "label": "LLM / Modelle",
                "status": "ok",
                "total": 1,
                "ok": 1,
                "warning": 0,
                "fail": 0,
                "skipped": 0,
                "running": 0,
            },
        ],
        "checks": [
            {
                "id": "backend-database",
                "category": "backend",
                "name": "Datenbank erreichbar",
                "status": "fail",
                "severity": "high",
                "started_at": now,
                "finished_at": now,
                "duration_ms": 120.0,
                "message": "Datenbank ist vorhanden, aber nicht lesbar.",
                "detail": "sqlite3.OperationalError: database is locked",
                "hint": "Pruefe Dateirechte und ob ein anderer Prozess die DB blockiert.",
                "target": "/data/orchestrator.db",
                "raw_value": {"path": "/data/orchestrator.db"},
            },
            {
                "id": "worker-coding",
                "category": "workers",
                "name": "Coding Worker",
                "status": "warning",
                "severity": "medium",
                "started_at": now,
                "finished_at": now,
                "duration_ms": 85.0,
                "message": "Coding Worker ist erreichbar und wartet aktuell auf lokales Modell.",
                "detail": "Patch-Plan wird vorbereitet.",
                "hint": "Wenn die Wartezeit weiter steigt, pruefe das Modellrouting.",
                "target": "http://coding-worker:8093/health",
                "raw_value": {
                    "state": "waiting",
                    "state_group": "wartend",
                    "waiting_for": "lokales Modell",
                    "current_action": "Patch-Plan erstellen",
                    "updated_at": now,
                    "elapsed_seconds": 35,
                    "task_id": "task-1",
                    "goal": "Self-Improvement Patch vorbereiten",
                },
            },
            {
                "id": "llm-default-provider",
                "category": "llm",
                "name": "Default-Provider aufloesbar",
                "status": "ok",
                "severity": "info",
                "started_at": now,
                "finished_at": now,
                "duration_ms": 15.0,
                "message": "DEFAULT_MODEL_PROVIDER zeigt auf `qwen`.",
                "detail": "",
                "hint": "",
                "target": None,
                "raw_value": {"provider": "qwen"},
            },
        ],
        "environment_overview": {
            "mode": "quick",
            "default_model_provider": "qwen",
            "orchestrator_internal_url": "http://orchestrator:8080",
            "web_ui_internal_url": "http://web-ui:8088",
            "workspace_root": "/workspace",
            "task_workspace_root": "/workspace/.task-workspaces",
            "runtime_home_dir": "/tmp/agent-home",
            "primary_repo_path": "/workspace/local-multi-agent-company",
        },
        "recommendations": [
            {
                "id": "recommendation-backend-database",
                "priority": 100,
                "category": "backend",
                "title": "Datenbank erreichbar",
                "message": "Pruefe Dateirechte und ob ein anderer Prozess die DB blockiert.",
                "related_check_ids": ["backend-database"],
            }
        ],
        "ready_for_workflows": True,
        "headline": "Bereit mit Warnungen",
        "summary_message": "Das System ist nutzbar, aber einzelne Komponenten brauchen Aufmerksamkeit.",
    }


def test_system_check_page_renders_structured_dashboard(tmp_path, monkeypatch) -> None:
    app_module = _prepare_web_ui_module(tmp_path, monkeypatch)
    now = datetime.now(UTC).isoformat()
    decorated = app_module._decorate_readiness_report(_sample_report(now))

    async def fake_load_readiness_context(*, mode):
        return {
            "report": decorated,
            "report_json": app_module._safe_json(decorated),
            "mode": mode.value,
            "deep_timeout_display": "45s",
            "llm_smoke_timeout_display": "4m 00s",
            "slow_warning_display": "20s",
        }

    monkeypatch.setattr(app_module, "_load_readiness_context", fake_load_readiness_context)

    with TestClient(app_module.app) as client:
        response = client.get("/system-check")

    assert response.status_code == 200
    assert "System-Check-Dashboard" in response.text
    assert "Bereit mit Warnungen" in response.text
    assert "Priorisierte Empfehlungen" in response.text
    assert "Worker-Uebersicht" in response.text
    assert "Coding Worker" in response.text
    assert "Datenbank ist vorhanden, aber nicht lesbar." in response.text
    assert "Bereitschaftspruefung schlug fehl" not in response.text


def test_system_check_panel_and_json_export_render_even_with_failures(tmp_path, monkeypatch) -> None:
    app_module = _prepare_web_ui_module(tmp_path, monkeypatch)
    now = datetime.now(UTC).isoformat()
    decorated = app_module._decorate_readiness_report(_sample_report(now))

    async def fake_load_readiness_context(*, mode):
        return {
            "report": decorated,
            "report_json": app_module._safe_json(decorated),
            "mode": mode.value,
            "deep_timeout_display": "45s",
            "llm_smoke_timeout_display": "4m 00s",
            "slow_warning_display": "20s",
        }

    monkeypatch.setattr(app_module, "_load_readiness_context", fake_load_readiness_context)

    with TestClient(app_module.app) as client:
        panel_response = client.get("/system-check/panel")
        json_response = client.get("/system-check/report.json")

    assert panel_response.status_code == 200
    assert "Technischer Fehlertext" in panel_response.text
    assert "Patch-Plan erstellen" in panel_response.text
    assert json_response.status_code == 200
    assert json_response.json()["overall_status"] == "warning"
    assert json_response.json()["checks"][0]["id"] == "backend-database"
