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
