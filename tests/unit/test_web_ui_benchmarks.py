"""
Purpose: Verify that the worker benchmark page turns persisted task details into readable worker performance summaries.
Input/Output: Tests feed the web UI mocked task payloads and inspect both the HTML page and JSON export.
Important invariants: Benchmarks must stay human-readable, degrade gracefully, and expose structured raw data for follow-up work.
How to debug: If this fails, inspect `services/web_ui/app.py` and compare the fake task payload with the generated benchmark report.
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
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


def test_benchmark_page_and_export_show_worker_metrics(tmp_path, monkeypatch) -> None:
    app_module = _prepare_web_ui_module(tmp_path, monkeypatch)
    now = datetime.now(UTC).replace(microsecond=0)

    async def fake_api_request(method: str, path: str, *, json_payload=None):
        del json_payload
        if path == "/api/benchmarks/model-probe":
            return httpx.Response(
                200,
                json={
                    "runs": [
                        {
                            "id": "probe-1",
                            "status": "completed",
                            "probe_goal": "Teste schnelle Modellantworten fuer observability-orientierte Aufgaben.",
                            "created_at": now.isoformat(),
                            "started_at": now.isoformat(),
                            "updated_at": now.isoformat(),
                            "completed_at": now.isoformat(),
                            "active_worker_name": None,
                            "total_workers": 2,
                            "completed_workers": 2,
                            "failed_workers": 0,
                            "errors": [],
                            "results": [
                                {
                                    "worker_name": "requirements",
                                    "worker_label": "Anforderungen",
                                    "status": "ok",
                                    "output_contract": "json",
                                    "response_format": "json",
                                    "summary": "Knappe Requirements-Antwort.",
                                    "response_text": "{\n  \"summary\": \"Knappe Requirements-Antwort.\"\n}",
                                    "response_data": {"summary": "Knappe Requirements-Antwort."},
                                    "provider": "mistral",
                                    "model_name": "mistral-small3.2:latest",
                                    "base_url": "http://mistral.local/v1",
                                    "used_fallback": False,
                                    "repair_pass_used": False,
                                    "started_at": now.isoformat(),
                                    "completed_at": now.isoformat(),
                                    "elapsed_seconds": 8.0,
                                    "notes": ["Konfiguriertes Primärmodell: mistral / mistral-small3.2:latest"],
                                },
                                {
                                    "worker_name": "research",
                                    "worker_label": "Recherche",
                                    "status": "ok",
                                    "output_contract": "text",
                                    "response_format": "text",
                                    "summary": "Research beschreibt die ersten Prüfschritte.",
                                    "response_text": "First Checks\\n- README\\n- Healthchecks",
                                    "response_data": {},
                                    "provider": "qwen",
                                    "model_name": "qwen3.5:35b-a3b",
                                    "base_url": "http://qwen.local/v1",
                                    "used_fallback": False,
                                    "repair_pass_used": False,
                                    "started_at": now.isoformat(),
                                    "completed_at": now.isoformat(),
                                    "elapsed_seconds": 12.0,
                                    "notes": ["Konfiguriertes Primärmodell: qwen / qwen3.5:35b-a3b"],
                                },
                            ],
                        }
                    ]
                },
            )
        if path == "/api/tasks?include_archived=true":
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
    assert "Schnelltest Modellantworten" in page_response.text
    assert "Gesammelte Modellantwort anzeigen" in page_response.text
    assert "Guidance daraus ableiten" in page_response.text

    assert export_response.status_code == 200
    payload = export_response.json()
    assert payload["total_tasks"] == 2
    assert payload["total_runs"] >= 2
    coding_summary = next(item for item in payload["worker_summaries"] if item["worker_name"] == "coding")
    research_summary = next(item for item in payload["worker_summaries"] if item["worker_name"] == "research")
    assert coding_summary["run_count"] == 1
    assert research_summary["failed_count"] == 1
    assert research_summary["guidance_suggestion"] is not None


def test_benchmark_reset_starts_a_new_visible_window(tmp_path, monkeypatch) -> None:
    app_module = _prepare_web_ui_module(tmp_path, monkeypatch)
    now = datetime.now(UTC).replace(microsecond=0)
    older = (now - timedelta(minutes=2)).isoformat()

    async def fake_api_request(method: str, path: str, *, json_payload=None):
        del json_payload
        if path == "/api/benchmarks/model-probe":
            return httpx.Response(200, json={"runs": []})
        if path == "/api/tasks?include_archived=true":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "task-old",
                        "goal": "Alte Benchmark-Daten sollen nach dem Reset nicht mehr sichtbar sein.",
                        "repository": "Feberdin/local-multi-agent-company",
                        "local_repo_path": "/workspace/local-multi-agent-company",
                        "base_branch": "main",
                        "branch_name": "feature/task-old",
                        "status": "DONE",
                        "metadata": {},
                        "created_at": older,
                        "updated_at": older,
                    }
                ],
            )
        if path == "/api/tasks/task-old":
            return httpx.Response(
                200,
                json={
                    "id": "task-old",
                    "goal": "Alte Benchmark-Daten sollen nach dem Reset nicht mehr sichtbar sein.",
                    "repository": "Feberdin/local-multi-agent-company",
                    "repo_url": None,
                    "local_repo_path": "/workspace/local-multi-agent-company",
                    "base_branch": "main",
                    "branch_name": "feature/task-old",
                    "status": "DONE",
                    "resume_target": None,
                    "current_approval_gate_name": None,
                    "approval_required": False,
                    "approval_reason": None,
                    "allow_repository_modifications": True,
                    "pull_request_url": None,
                    "latest_error": None,
                    "metadata": {},
                    "created_at": older,
                    "updated_at": older,
                    "worker_results": {},
                    "risk_flags": [],
                    "events": [],
                    "approvals": [],
                    "smoke_checks": [],
                    "deployment": None,
                },
            )
        raise AssertionError(f"Unexpected path: {path}")

    app_module._api_request = fake_api_request
    state_path = Path(app_module.settings.data_dir) / "benchmark_state.json"

    with TestClient(app_module.app) as client:
        reset_response = client.post("/benchmarks/reset")
        export_response = client.get("/benchmarks/export.json")

    assert reset_response.status_code == 200
    assert "Benchmarks zurücksetzen" in reset_response.text
    assert "Die Benchmark-Auswertung wurde zurückgesetzt." in reset_response.text
    assert state_path.exists() is True
    assert export_response.status_code == 200
    payload = export_response.json()
    assert payload["benchmark_window_active"] is True
    assert payload["total_tasks"] == 0
    assert payload["hidden_tasks_before_reset"] == 1


def test_benchmarks_exclude_archived_tasks_from_general_metrics(tmp_path, monkeypatch) -> None:
    app_module = _prepare_web_ui_module(tmp_path, monkeypatch)
    now = datetime.now(UTC).replace(microsecond=0).isoformat()

    async def fake_api_request(method: str, path: str, *, json_payload=None):
        del json_payload
        if path == "/api/benchmarks/model-probe":
            return httpx.Response(200, json={"runs": []})
        if path == "/api/tasks?include_archived=true":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "task-live",
                        "goal": "Sichtbare Benchmarks sollen nur aktive Historie verwenden.",
                        "repository": "Feberdin/local-multi-agent-company",
                        "local_repo_path": "/workspace/local-multi-agent-company",
                        "base_branch": "main",
                        "branch_name": "feature/task-live",
                        "status": "DONE",
                        "metadata": {},
                        "created_at": now,
                        "updated_at": now,
                    },
                    {
                        "id": "task-archived",
                        "goal": "Archivierte Altlasten sollen nicht mehr in Benchmarks zaehlen.",
                        "repository": "Feberdin/local-multi-agent-company",
                        "local_repo_path": "/workspace/local-multi-agent-company",
                        "base_branch": "main",
                        "branch_name": "feature/task-archived",
                        "status": "DONE",
                        "archived": True,
                        "metadata": {"archived": True, "archived_reason": "Altlast"},
                        "created_at": now,
                        "updated_at": now,
                    },
                ],
            )
        if path == "/api/tasks/task-live":
            return httpx.Response(
                200,
                json={
                    "id": "task-live",
                    "goal": "Sichtbare Benchmarks sollen nur aktive Historie verwenden.",
                    "repository": "Feberdin/local-multi-agent-company",
                    "repo_url": None,
                    "local_repo_path": "/workspace/local-multi-agent-company",
                    "base_branch": "main",
                    "branch_name": "feature/task-live",
                    "status": "DONE",
                    "resume_target": None,
                    "current_approval_gate_name": None,
                    "approval_required": False,
                    "approval_reason": None,
                    "allow_repository_modifications": True,
                    "pull_request_url": None,
                    "latest_error": None,
                    "metadata": {},
                    "created_at": now,
                    "updated_at": now,
                    "worker_results": {},
                    "risk_flags": [],
                    "events": [],
                    "approvals": [],
                    "smoke_checks": [],
                    "deployment": None,
                },
            )
        raise AssertionError(f"Unexpected path: {path}")

    app_module._api_request = fake_api_request

    with TestClient(app_module.app) as client:
        response = client.get("/benchmarks/export.json")

    payload = response.json()
    assert payload["total_tasks"] == 1
    assert payload["recent_runs"] == []
    assert payload["worker_summaries"][0]["run_count"] == 0


def test_benchmark_page_can_start_worker_probe_from_ui(tmp_path, monkeypatch) -> None:
    app_module = _prepare_web_ui_module(tmp_path, monkeypatch)
    now = datetime.now(UTC).replace(microsecond=0)

    async def fake_api_request(method: str, path: str, *, json_payload=None):
        if method == "POST" and path == "/api/benchmarks/model-probe":
            assert json_payload == {
                "probe_goal": "Teste strukturierten Modell-Output fuer observability-lastige Worker.",
                "probe_mode": "full",
            }
            return httpx.Response(
                201,
                json={
                    "id": "probe-2",
                    "status": "queued",
                    "probe_goal": json_payload["probe_goal"],
                    "probe_mode": json_payload["probe_mode"],
                    "created_at": now.isoformat(),
                    "updated_at": now.isoformat(),
                    "results": [],
                    "errors": [],
                    "total_workers": 8,
                    "completed_workers": 0,
                    "failed_workers": 0,
                },
            )
        if method == "GET" and path == "/api/benchmarks/model-probe":
            return httpx.Response(200, json={"runs": []})
        if method == "GET" and path == "/api/tasks?include_archived=true":
            return httpx.Response(200, json=[])
        raise AssertionError(f"Unexpected call: {path}")

    app_module._api_request = fake_api_request

    with TestClient(app_module.app) as client:
        response = client.post(
            "/benchmarks/model-probe/start",
            data={"probe_goal": "Teste strukturierten Modell-Output fuer observability-lastige Worker."},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/benchmarks"


def test_benchmark_page_can_start_ok_contract_probe_from_ui(tmp_path, monkeypatch) -> None:
    app_module = _prepare_web_ui_module(tmp_path, monkeypatch)
    now = datetime.now(UTC).replace(microsecond=0)

    async def fake_api_request(method: str, path: str, *, json_payload=None):
        if method == "POST" and path == "/api/benchmarks/model-probe":
            assert json_payload == {
                "probe_goal": "Leerer OK-Kurztest fuer alle Worker-Vertraege ohne Repository-Aenderungen.",
                "probe_mode": "ok_contract",
            }
            return httpx.Response(
                201,
                json={
                    "id": "probe-ok",
                    "status": "queued",
                    "probe_goal": json_payload["probe_goal"],
                    "probe_mode": json_payload["probe_mode"],
                    "created_at": now.isoformat(),
                    "updated_at": now.isoformat(),
                    "results": [],
                    "errors": [],
                    "total_workers": 8,
                    "completed_workers": 0,
                    "failed_workers": 0,
                },
            )
        if method == "GET" and path == "/api/benchmarks/model-probe":
            return httpx.Response(200, json={"runs": []})
        if method == "GET" and path == "/api/tasks?include_archived=true":
            return httpx.Response(200, json=[])
        raise AssertionError(f"Unexpected call: {path}")

    app_module._api_request = fake_api_request

    with TestClient(app_module.app) as client:
        response = client.post(
            "/benchmarks/model-probe/start",
            data={
                "probe_goal": "Leerer OK-Kurztest fuer alle Worker-Vertraege ohne Repository-Aenderungen.",
                "probe_mode": "ok_contract",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/benchmarks"


def test_worker_tests_page_renders_targeted_actions_and_latest_run(tmp_path, monkeypatch) -> None:
    app_module = _prepare_web_ui_module(tmp_path, monkeypatch)
    now = datetime.now(UTC).replace(microsecond=0)
    app_module._recent_fix_focus_paths = lambda limit=6: [  # noqa: ARG005
        "services/coding_worker/app.py",
        "tests/unit/test_coding_worker.py",
    ]

    async def fake_api_request(method: str, path: str, *, json_payload=None):
        del method, json_payload
        if path == "/api/benchmarks/model-probe":
            return httpx.Response(
                200,
                json={
                    "runs": [
                        {
                            "id": "probe-targeted",
                            "status": "completed",
                            "probe_goal": "Pruefe nur Code und Architektur nach einem Fix.",
                            "probe_mode": "full",
                            "selected_workers": ["architecture", "coding"],
                            "focus_paths": ["services/coding_worker/app.py", "tests/unit/test_coding_worker.py"],
                            "created_at": now.isoformat(),
                            "started_at": now.isoformat(),
                            "updated_at": now.isoformat(),
                            "completed_at": now.isoformat(),
                            "active_worker_name": None,
                            "total_workers": 2,
                            "completed_workers": 2,
                            "failed_workers": 0,
                            "errors": [],
                            "results": [],
                        }
                    ]
                },
            )
        raise AssertionError(f"Unexpected call: {path}")

    app_module._api_request = fake_api_request

    with TestClient(app_module.app) as client:
        response = client.get("/worker-tests")

    assert response.status_code == 200
    assert "Teiltests pro Worker" in response.text
    assert "Diesen Worker testen" in response.text
    assert "Nur OK pruefen" in response.text
    assert "README-Mini-Fix" in response.text
    assert "Architektur, Code" in response.text
    assert "services/coding_worker/app.py" in response.text


def test_worker_tests_page_can_start_targeted_probe_with_selected_workers(tmp_path, monkeypatch) -> None:
    app_module = _prepare_web_ui_module(tmp_path, monkeypatch)
    now = datetime.now(UTC).replace(microsecond=0)

    async def fake_api_request(method: str, path: str, *, json_payload=None):
        if method == "POST" and path == "/api/benchmarks/model-probe":
            assert json_payload == {
                "probe_goal": "Pruefe nur Code und Architektur nach dem Fix.",
                "probe_mode": "full",
                "selected_workers": ["architecture", "coding"],
                "focus_paths": ["services/coding_worker/app.py", "tests/unit/test_coding_worker.py"],
            }
            return httpx.Response(
                201,
                json={
                    "id": "probe-targeted",
                    "status": "queued",
                    "probe_goal": json_payload["probe_goal"],
                    "probe_mode": json_payload["probe_mode"],
                    "selected_workers": json_payload["selected_workers"],
                    "created_at": now.isoformat(),
                    "updated_at": now.isoformat(),
                    "results": [],
                    "errors": [],
                    "total_workers": 2,
                    "completed_workers": 0,
                    "failed_workers": 0,
                },
            )
        if method == "GET" and path == "/api/benchmarks/model-probe":
            return httpx.Response(200, json={"runs": []})
        raise AssertionError(f"Unexpected call: {method} {path}")

    app_module._api_request = fake_api_request
    app_module._recent_fix_focus_paths = lambda limit=6: [  # noqa: ARG005
        "services/coding_worker/app.py",
        "tests/unit/test_coding_worker.py",
    ]

    with TestClient(app_module.app) as client:
        response = client.post(
            "/worker-tests/start",
            data={
                "probe_goal": "Pruefe nur Code und Architektur nach dem Fix.",
                "selected_workers": ["architecture", "coding"],
                "focus_paths_text": "services/coding_worker/app.py\ntests/unit/test_coding_worker.py",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/worker-tests"


def test_worker_tests_page_can_start_readme_micro_fix_probe(tmp_path, monkeypatch) -> None:
    app_module = _prepare_web_ui_module(tmp_path, monkeypatch)
    now = datetime.now(UTC).replace(microsecond=0)

    async def fake_api_request(method: str, path: str, *, json_payload=None):
        if method == "POST" and path == "/api/benchmarks/model-probe":
            assert json_payload == {
                "probe_goal": "README-Mini-Fix im Wegwerf-Repo pruefen.",
                "probe_mode": "micro_fix",
                "selected_workers": ["coding"],
                "focus_paths": ["README.md"],
            }
            return httpx.Response(
                201,
                json={
                    "id": "probe-micro-fix",
                    "status": "queued",
                    "probe_goal": json_payload["probe_goal"],
                    "probe_mode": json_payload["probe_mode"],
                    "selected_workers": json_payload["selected_workers"],
                    "created_at": now.isoformat(),
                    "updated_at": now.isoformat(),
                    "results": [],
                    "errors": [],
                    "total_workers": 1,
                    "completed_workers": 0,
                    "failed_workers": 0,
                },
            )
        if method == "GET" and path == "/api/benchmarks/model-probe":
            return httpx.Response(200, json={"runs": []})
        raise AssertionError(f"Unexpected call: {method} {path}")

    app_module._api_request = fake_api_request

    with TestClient(app_module.app) as client:
        response = client.post(
            "/worker-tests/start",
            data={
                "probe_goal": "README-Mini-Fix im Wegwerf-Repo pruefen.",
                "probe_mode": "micro_fix",
                "selected_workers": ["coding"],
                "focus_paths_text": "README.md",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/worker-tests"


def test_worker_guidance_page_can_apply_benchmark_hint_into_operator_recommendations(
    tmp_path,
    monkeypatch,
) -> None:
    app_module = _prepare_web_ui_module(tmp_path, monkeypatch)

    async def fake_api_request(method: str, path: str, *, json_payload=None):
        del method, json_payload
        if path == "/api/settings/worker-guidance":
            return httpx.Response(
                200,
                json={
                    "workers": [
                        {
                            "worker_name": "coding",
                            "display_name": "Coding Worker",
                            "enabled": True,
                            "role_description": "Setzt kleine sichere Aenderungen um.",
                            "operator_recommendations": ["Minimiere den Diff."],
                            "decision_preferences": ["Kleine sichere Schritte."],
                            "competence_boundary": "Keine Scope-Erweiterung ohne Freigabe.",
                            "escalate_out_of_scope": True,
                            "auto_submit_suggestions": True,
                        }
                    ]
                },
            )
        raise AssertionError(f"Unexpected call: {path}")

    app_module._api_request = fake_api_request
    benchmark_hint = (
        "Akzeptiere generische Blocker nicht als Endzustand.\n"
        "Liefere den kleinsten sicheren edit_plan innerhalb der sichtbaren Ziel-Dateien."
    )

    with TestClient(app_module.app) as client:
        response = client.get(
            "/worker-guidance",
            params={
                "edit": "coding",
                "benchmark_hint": benchmark_hint,
                "apply_benchmark_hint": "true",
            },
        )

    assert response.status_code == 200
    assert "Benchmark-Hinweis fuer diesen Worker" in response.text
    assert "Akzeptiere generische Blocker nicht als Endzustand." in response.text
    assert "Minimiere den Diff." in response.text
    assert "Liefere den kleinsten sicheren edit_plan innerhalb der sichtbaren Ziel-Dateien." in response.text
