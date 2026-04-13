"""
Purpose: Verify that the research worker degrades gracefully instead of crashing with HTTP 500.
Input/Output: Tests simulate dirty checkouts and unexpected runtime failures, then inspect the WorkerResponse.
Important invariants: Research may continue on an existing checkout, and unexpected exceptions must become operator-visible errors.
How to debug: If this fails, inspect services/research_worker/app.py and compare the fallback path with the mocked repo helpers.
"""

from __future__ import annotations

import importlib
from pathlib import Path

from fastapi.testclient import TestClient

from services.shared.agentic_lab.repo_tools import CommandError


class _FakeSourcePlan:
    """Small stub for source routing decisions used by the research worker tests."""

    fallback_reason = None
    general_web_allowed = False
    trusted_matches: list[object] = []

    def model_dump(self, mode: str = "json") -> dict[str, object]:
        del mode
        return {
            "fallback_reason": self.fallback_reason,
            "general_web_allowed": self.general_web_allowed,
            "trusted_matches": [],
        }


def _prepare_research_module(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("STAGING_STACK_ROOT", str(tmp_path / "staging-stacks"))
    from services.shared.agentic_lab.config import get_settings

    get_settings.cache_clear()
    app_module = importlib.import_module("services.research_worker.app")
    return importlib.reload(app_module)


def _worker_payload(repo_path: Path) -> dict[str, object]:
    return {
        "task_id": "task-1",
        "goal": "Analyse the repository and suggest productivity improvements.",
        "repository": "Feberdin/local-multi-agent-company",
        "repo_url": None,
        "local_repo_path": str(repo_path),
        "base_branch": "main",
        "branch_name": None,
        "issue_number": None,
        "enable_web_research": False,
        "auto_deploy_staging": False,
        "test_commands": [],
        "lint_commands": [],
        "typing_commands": [],
        "smoke_checks": [],
        "deployment": None,
        "metadata": {},
        "prior_results": {},
    }


def test_research_worker_uses_existing_checkout_when_refresh_fails(tmp_path, monkeypatch) -> None:
    app_module = _prepare_research_module(tmp_path, monkeypatch)
    repo_path = tmp_path / "workspace" / "local-multi-agent-company"
    (repo_path / ".git").mkdir(parents=True, exist_ok=True)
    (repo_path / "README.md").write_text("# Demo\n", encoding="utf-8")

    def fail_checkout(**kwargs):
        del kwargs
        raise CommandError("git checkout main failed because the worktree is dirty")

    async def fake_summary(*args, **kwargs):
        del args, kwargs
        return "## Architecture\n- Existing checkout analysed successfully."

    monkeypatch.setattr(app_module, "ensure_repository_checkout", fail_checkout)
    monkeypatch.setattr(
        app_module,
        "collect_repo_overview",
        lambda _: {
            "file_count": 1,
            "important_files": ["README.md"],
            "sample_files": ["README.md"],
            "git_status": [" M README.md"],
            "last_commit": "abc123 demo",
        },
    )
    monkeypatch.setattr(app_module, "read_text_file", lambda *_args, **_kwargs: "# Demo\n")
    monkeypatch.setattr(app_module.source_router, "route", lambda *_args, **_kwargs: _FakeSourcePlan())
    monkeypatch.setattr(app_module.worker_governance, "guidance_prompt_block", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(app_module, "_summarize_with_llm", fake_summary)

    with TestClient(app_module.app) as client:
        response = client.post("/run", json=_worker_payload(repo_path))

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert "existing workspace checkout" in payload["warnings"][0]
    assert payload["outputs"]["local_repo_path"] == str(repo_path)


def test_research_worker_returns_structured_failure_instead_of_http_500(tmp_path, monkeypatch) -> None:
    app_module = _prepare_research_module(tmp_path, monkeypatch)
    repo_path = tmp_path / "workspace" / "missing-repo"

    def fail_runtime(**kwargs):
        del kwargs
        raise RuntimeError("boom")

    monkeypatch.setattr(app_module, "ensure_repository_checkout", fail_runtime)

    with TestClient(app_module.app) as client:
        response = client.post("/run", json=_worker_payload(repo_path))

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is False
    assert payload["summary"] == "Repository research failed before the report could be completed."
    assert "RuntimeError: boom" in payload["errors"][0]


def test_research_worker_falls_back_when_llm_returns_generic_helpful_prose(tmp_path, monkeypatch) -> None:
    app_module = _prepare_research_module(tmp_path, monkeypatch)
    repo_path = tmp_path / "workspace" / "local-multi-agent-company"
    (repo_path / ".git").mkdir(parents=True, exist_ok=True)
    (repo_path / "README.md").write_text("# Demo\n", encoding="utf-8")

    async def generic_summary(*args, **kwargs):
        del args, kwargs
        return (
            "Danke fuer das Teilen der umfassenden Dokumentation!\n\n"
            "Wie kann ich dir helfen?\n"
            "1. Fehleranalyse\n"
            "2. Erweiterung\n"
        )

    monkeypatch.setattr(
        app_module,
        "collect_repo_overview",
        lambda _: {
            "file_count": 1,
            "important_files": ["README.md"],
            "sample_files": ["README.md"],
            "git_status": [],
            "last_commit": "abc123 demo",
        },
    )
    monkeypatch.setattr(app_module, "read_text_file", lambda *_args, **_kwargs: "# Demo\n")
    monkeypatch.setattr(app_module.source_router, "route", lambda *_args, **_kwargs: _FakeSourcePlan())
    monkeypatch.setattr(app_module.worker_governance, "guidance_prompt_block", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(app_module, "_summarize_with_llm", generic_summary)

    with TestClient(app_module.app) as client:
        response = client.post("/run", json=_worker_payload(repo_path))

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert any("Using the deterministic fallback summary instead" in warning for warning in payload["warnings"])
    assert payload["outputs"]["research_notes"].startswith("## Architecture")
