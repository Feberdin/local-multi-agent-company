"""
Purpose: Smoke-test the orchestrator API contract for task creation and retrieval.
Input/Output: The test client exercises the FastAPI app with an isolated temporary database.
Important invariants: Core endpoints must remain stable because the web UI and scripts depend on them.
How to debug: If this test fails, compare the returned JSON with the shared response models.
"""

from __future__ import annotations

import importlib

from fastapi.testclient import TestClient

from services.shared.agentic_lab.db import Base, configure_database


def test_create_and_fetch_task(tmp_path) -> None:
    engine = configure_database(f"sqlite:///{tmp_path / 'api.db'}")
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    app_module = importlib.import_module("services.orchestrator.app")
    app_module = importlib.reload(app_module)

    with TestClient(app_module.app) as client:
        create_response = client.post(
            "/api/tasks",
            json={
                "goal": "Add a smoke test endpoint and update the staging deployment notes.",
                "repository": "Feberdin/example-repo",
                "local_repo_path": "/workspace/example-repo",
            },
        )
        assert create_response.status_code == 201
        task_id = create_response.json()["id"]

        fetch_response = client.get(f"/api/tasks/{task_id}")
        assert fetch_response.status_code == 200
        assert fetch_response.json()["repository"] == "Feberdin/example-repo"

        restart_response = client.post(
            f"/api/tasks/{task_id}/restart-stage",
            json={"worker_name": "requirements", "actor": "test-suite", "run_immediately": False},
        )
        assert restart_response.status_code == 200
        assert restart_response.json()["resume_target"] == "requirements"
        assert restart_response.json()["status"] == "REQUIREMENTS"

        archive_conflict_response = client.post(
            f"/api/tasks/{task_id}/archive",
            json={"actor": "test-suite", "reason": "Noch aktiv."},
        )
        assert archive_conflict_response.status_code == 409

        client.post(
            f"/api/tasks/{task_id}/approvals",
            json={"gate_name": "risk-review", "decision": "REJECT", "actor": "test-suite", "reason": "Archivtest"},
        )

        archive_response = client.post(
            f"/api/tasks/{task_id}/archive",
            json={"actor": "test-suite", "reason": "Alte Aufgabe aufraeumen."},
        )
        assert archive_response.status_code == 200
        assert archive_response.json()["archived"] is True

        visible_tasks_response = client.get("/api/tasks")
        assert visible_tasks_response.status_code == 200
        assert visible_tasks_response.json() == []

        archived_tasks_response = client.get("/api/tasks?only_archived=true")
        assert archived_tasks_response.status_code == 200
        assert archived_tasks_response.json()[0]["id"] == task_id

        restore_response = client.post(
            f"/api/tasks/{task_id}/restore",
            json={"actor": "test-suite", "reason": "Zur Kontrolle wieder sichtbar."},
        )
        assert restore_response.status_code == 200
        assert restore_response.json()["archived"] is False

        trusted_sources_response = client.get("/api/settings/trusted-sources")
        assert trusted_sources_response.status_code == 200
        assert trusted_sources_response.json()["active_profile_id"] == "trusted_coding"

        dry_run_response = client.post(
            "/api/settings/trusted-sources/dry-run",
            json={"query": "latest FastAPI version on PyPI"},
        )
        assert dry_run_response.status_code == 200
        assert dry_run_response.json()["trusted_matches"][0]["domain"] == "pypi.org"

        invalid_source_response = client.post(
            "/api/settings/trusted-sources/sources",
            json={
                "id": "bad",
                "name": "Wildcard Source",
                "domain": "*.example.org",
                "category": "official_docs",
                "enabled": False,
                "priority": 100,
                "source_type": "docs",
                "preferred_access": "html",
                "base_url": "https://example.org",
                "allowed_paths": [],
                "deny_paths": [],
                "tags": [],
            },
        )
        assert invalid_source_response.status_code == 400

        web_search_settings_response = client.get("/api/settings/web-search")
        assert web_search_settings_response.status_code == 200
        assert web_search_settings_response.json()["primary_web_search_provider"] == "searxng"

        worker_guidance_response = client.get("/api/settings/worker-guidance")
        assert worker_guidance_response.status_code == 200
        guidance_workers = worker_guidance_response.json()["workers"]
        assert len(guidance_workers) == 17
        coding_policy = next(item for item in guidance_workers if item["worker_name"] == "coding")
        assert coding_policy["display_name"] == "Coding Worker"
        assert coding_policy["role_description"].startswith("Setzt minimal-invasive")

        suggestions_response = client.get("/api/suggestions")
        assert suggestions_response.status_code == 200
        assert suggestions_response.json() == []

        async def fake_readiness(_settings, *, mode, services=None):
            return app_module.ReadinessReport.model_validate(
                {
                    "mode": mode.value,
                    "overall_status": "ok",
                    "started_at": "2026-04-12T00:00:00+00:00",
                    "finished_at": "2026-04-12T00:00:01+00:00",
                    "duration_ms": 1000.0,
                    "summary": {"total": 1, "ok": 1, "warning": 0, "fail": 0, "skipped": 0, "running": 0},
                    "categories": [
                        {
                            "id": "backend",
                            "label": "Backend / Core",
                            "status": "ok",
                            "total": 1,
                            "ok": 1,
                            "warning": 0,
                            "fail": 0,
                            "skipped": 0,
                            "running": 0,
                        }
                    ],
                    "checks": [
                        {
                            "id": "backend-ok",
                            "category": "backend",
                            "name": "Backend",
                            "status": "ok",
                            "severity": "info",
                            "started_at": "2026-04-12T00:00:00+00:00",
                            "finished_at": "2026-04-12T00:00:01+00:00",
                            "duration_ms": 1000.0,
                            "message": "Backend antwortet.",
                            "detail": "",
                            "hint": "",
                            "target": "http://orchestrator:8080/health",
                            "raw_value": None,
                            "depends_on": [],
                        }
                    ],
                    "environment_overview": {"mode": mode.value},
                    "recommendations": [],
                    "ready_for_workflows": True,
                    "headline": "Bereit",
                    "summary_message": "Alles gut.",
                }
            )

        app_module.run_system_readiness_check = fake_readiness
        readiness_response = client.get("/api/system/readiness")
        assert readiness_response.status_code == 200
        assert readiness_response.json()["overall_status"] == "ok"


def test_web_ui_import_and_health(tmp_path) -> None:
    engine = configure_database(f"sqlite:///{tmp_path / 'web-ui.db'}")
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    app_module = importlib.import_module("services.web_ui.app")
    app_module = importlib.reload(app_module)

    tasks_route = next(
        route
        for route in app_module.app.routes
        if getattr(route, "path", None) == "/tasks" and "POST" in getattr(route, "methods", set())
    )
    assert tasks_route.response_model is None

    with TestClient(app_module.app) as client:
        health_response = client.get("/health")
        assert health_response.status_code == 200
        assert health_response.json()["service"] == "web-ui"
