"""
Purpose: Verify that tiny README smiley tasks use the deterministic validation fast path.
Input/Output: Tests call the validation worker with a synthetic coding result and inspect the structured validation output.
Important invariants: The fast path must stay strict about README-only diffs while avoiding slow LLM calls.
How to debug: If this fails, inspect services/validation_worker/app.py and the readme smiley task profile.
"""

from __future__ import annotations

import importlib

import pytest

from services.shared.agentic_lab.config import get_settings
from services.shared.agentic_lab.schemas import WorkerRequest


def _load_validation_module(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("STAGING_STACK_ROOT", str(tmp_path / "staging-stacks"))
    get_settings.cache_clear()
    module = importlib.import_module("services.validation_worker.app")
    return importlib.reload(module)


@pytest.mark.asyncio
async def test_validation_worker_uses_deterministic_readme_smiley_fast_path(tmp_path, monkeypatch) -> None:
    app_module = _load_validation_module(tmp_path, monkeypatch)

    response = await app_module.run(
        WorkerRequest(
            task_id="task-validation-fast",
            goal="Fuege am Anfang der Readme einen Smiley ein.",
            repository="Feberdin/local-multi-agent-company",
            local_repo_path=str(tmp_path / "workspace" / "local-multi-agent-company"),
            base_branch="main",
            metadata={"task_profile": {"name": "readme_prefix_smiley_fix"}},
            prior_results={"coding": {"outputs": {"changed_files": ["README.md"]}}},
        )
    )

    assert response.success is True
    assert response.outputs["release_readiness"] == "beta"
    assert "Nur README.md ist im resultierenden Diff sichtbar." in response.outputs["fulfilled"]
