"""
Purpose: Verify that the memory worker writes both durable raw memory and a compact operator handoff.
Input/Output: The tests call the memory worker with synthetic upstream outputs and inspect the generated artifacts.
Important invariants: The handoff must remain easy to skim for humans and stable enough for later agent reuse.
How to debug: If this fails, inspect services/memory_worker/app.py and the generated handoff artifacts.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from services.shared.agentic_lab.config import get_settings
from services.shared.agentic_lab.schemas import WorkerRequest


def _load_memory_module(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("STAGING_STACK_ROOT", str(tmp_path / "staging-stacks"))
    get_settings.cache_clear()
    module = importlib.import_module("services.memory_worker.app")
    return importlib.reload(module)


@pytest.mark.asyncio
async def test_memory_worker_writes_handoff_json_and_markdown(tmp_path, monkeypatch) -> None:
    app_module = _load_memory_module(tmp_path, monkeypatch)

    response = await app_module.run(
        WorkerRequest(
            task_id="task-memory-handoff",
            goal="Change WORKER_STAGE_TIMEOUT_SECONDS to 3600 in services/shared/agentic_lab/config.py",
            repository="Feberdin/local-multi-agent-company",
            local_repo_path=str(tmp_path / "workspace" / "local-multi-agent-company"),
            base_branch="main",
            branch_name="feature/timeout-fix",
            auto_deploy_staging=False,
            metadata={
                "deployment_target": "staging",
                "allow_deploy_after_success": False,
            },
            prior_results={
                "coding": {
                    "outputs": {
                        "changed_files": [
                            "services/shared/agentic_lab/config.py",
                            "README.md",
                        ],
                        "diff_stat": "2 files changed, 2 insertions(+), 2 deletions(-)",
                    }
                },
                "validation": {
                    "outputs": {
                        "fulfilled": [
                            "`services/shared/agentic_lab/config.py` wurde aktualisiert."
                        ],
                        "residual_risks": [
                            "Der laengere Timeout macht langsame Fehlversuche spaeter sichtbar."
                        ],
                        "release_readiness": "beta",
                    }
                },
                "github": {
                    "outputs": {
                        "commit_sha": "abc123def456",
                        "pull_request_url": "https://github.com/Feberdin/local-multi-agent-company/pull/99",
                        "publish_strategy": "git_push",
                    }
                },
            },
        )
    )

    assert response.success is True
    assert response.outputs["next_steps"]

    handoff_json_path = Path(response.outputs["handoff_json_path"])
    handoff_markdown_path = Path(response.outputs["handoff_markdown_path"])
    assert handoff_json_path.exists()
    assert handoff_markdown_path.exists()

    handoff_payload = json.loads(handoff_json_path.read_text(encoding="utf-8"))
    assert handoff_payload["branch_name"] == "feature/timeout-fix"
    assert handoff_payload["commit_sha"] == "abc123def456"
    assert handoff_payload["pull_request_url"].endswith("/pull/99")
    assert handoff_payload["deployment_performed"] is False
    assert handoff_payload["deploy_allowed"] is False
    assert "services/shared/agentic_lab/config.py" in handoff_payload["changed_files"]

    handoff_markdown = handoff_markdown_path.read_text(encoding="utf-8")
    assert "# Task-Handoff" in handoff_markdown
    assert "feature/timeout-fix" in handoff_markdown
    assert "https://github.com/Feberdin/local-multi-agent-company/pull/99" in handoff_markdown
    assert "Kein Auto-Deploy in diesem Lauf" in handoff_markdown


@pytest.mark.asyncio
async def test_memory_worker_marks_deploy_as_performed_when_outputs_exist(tmp_path, monkeypatch) -> None:
    app_module = _load_memory_module(tmp_path, monkeypatch)

    response = await app_module.run(
        WorkerRequest(
            task_id="task-memory-deployed",
            goal="Deploy a validated staging change",
            repository="Feberdin/local-multi-agent-company",
            local_repo_path=str(tmp_path / "workspace" / "local-multi-agent-company"),
            base_branch="main",
            branch_name="feature/deploy-ready",
            auto_deploy_staging=True,
            metadata={
                "deployment_target": "self",
                "allow_deploy_after_success": True,
            },
            prior_results={
                "coding": {"outputs": {"changed_files": ["README.md"]}},
                "deploy": {"outputs": {"watchdog_status": "healthy"}},
            },
        )
    )

    handoff_payload = json.loads(Path(response.outputs["handoff_json_path"]).read_text(encoding="utf-8"))
    assert handoff_payload["deployment_performed"] is True
    assert handoff_payload["deploy_allowed"] is True
