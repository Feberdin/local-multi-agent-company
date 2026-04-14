"""
Purpose: Verify that tiny README smiley tasks use the deterministic requirements fast path.
Input/Output: Tests call the requirements worker with a synthetic WorkerRequest and inspect the structured outputs.
Important invariants: The fast path must keep scope on README.md and avoid broad worker recommendations.
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
