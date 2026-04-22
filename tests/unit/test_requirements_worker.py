"""
Purpose: Verify that tiny deterministic task profiles use the fast-path requirements logic.
Input/Output: Tests call the requirements worker with synthetic WorkerRequests and inspect the structured outputs.
Important invariants: Fast paths must stay tight on the real target files and avoid broad worker recommendations.
How to debug: If this fails, inspect services/requirements_worker/app.py and the task-profile detection helper.
"""

from __future__ import annotations

import importlib

import pytest

from services.shared.agentic_lab.config import get_settings
from services.shared.agentic_lab.schemas import WorkerRequest


def _load_requirements_module(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("STAGING_STACK_ROOT", str(tmp_path / "staging-stacks"))
    get_settings.cache_clear()
    module = importlib.import_module("services.requirements_worker.app")
    return importlib.reload(module)


@pytest.mark.asyncio
async def test_requirements_worker_uses_deterministic_readme_smiley_fast_path(tmp_path, monkeypatch) -> None:
    app_module = _load_requirements_module(tmp_path, monkeypatch)

    response = await app_module.run(
        WorkerRequest(
            task_id="task-requirements-fast",
            goal="Fuege am Anfang der Readme einen Smiley ein.",
            repository="Feberdin/local-multi-agent-company",
            local_repo_path=str(tmp_path / "workspace" / "local-multi-agent-company"),
            base_branch="main",
            metadata={"task_profile": {"name": "readme_prefix_smiley_fix"}},
        )
    )

    assert response.success is True
    assert response.outputs["requirements"][0] == "Aendere nur README.md im Repository-Wurzelverzeichnis."
    assert response.outputs["recommended_workers"] == ["cost", "human_resources", "coding", "validation", "github", "memory"]


@pytest.mark.asyncio
async def test_requirements_worker_uses_deterministic_worker_stage_timeout_fast_path(tmp_path, monkeypatch) -> None:
    app_module = _load_requirements_module(tmp_path, monkeypatch)

    response = await app_module.run(
        WorkerRequest(
            task_id="task-requirements-timeout-fast",
            goal="Change WORKER_STAGE_TIMEOUT_SECONDS to 3600 in worker.py",
            repository="Feberdin/local-multi-agent-company",
            local_repo_path=str(tmp_path / "workspace" / "local-multi-agent-company"),
            base_branch="main",
            metadata={
                "task_profile": {
                    "name": "worker_stage_timeout_config_fix",
                    "target_timeout_seconds": 3600.0,
                }
            },
        )
    )

    assert response.success is True
    assert "services/shared/agentic_lab/config.py" in response.outputs["requirements"][0]
    assert "README und Docs konsistent" in response.outputs["requirements"][1]
    assert response.outputs["recommended_workers"] == ["cost", "human_resources", "coding", "validation", "github", "memory"]


@pytest.mark.asyncio
async def test_requirements_worker_uses_deterministic_readme_top_block_fast_path(tmp_path, monkeypatch) -> None:
    app_module = _load_requirements_module(tmp_path, monkeypatch)

    response = await app_module.run(
        WorkerRequest(
            task_id="task-requirements-readme-block-fast",
            goal="Add a self-improvement block at the top of the README.md file",
            repository="Feberdin/local-multi-agent-company",
            local_repo_path=str(tmp_path / "workspace" / "local-multi-agent-company"),
            base_branch="main",
            metadata={"task_profile": {"name": "readme_top_block_fix"}},
        )
    )

    assert response.success is True
    assert response.outputs["requirements"][0] == "Aendere nur README.md im Repository-Wurzelverzeichnis."
    assert "am Dateianfang" in response.outputs["requirements"][1]
    assert response.outputs["recommended_workers"] == ["cost", "human_resources", "coding", "validation", "github", "memory"]
