"""
Purpose: Verify that tiny deterministic task profiles use the validation fast path.
Input/Output: Tests call the validation worker with synthetic coding results and inspect the structured output.
Important invariants: Fast paths must stay strict about their narrow diffs while avoiding slow LLM calls.
How to debug: If this fails, inspect services/validation_worker/app.py and the task-profile helper.
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


@pytest.mark.asyncio
async def test_validation_worker_uses_deterministic_worker_stage_timeout_fast_path(tmp_path, monkeypatch) -> None:
    app_module = _load_validation_module(tmp_path, monkeypatch)

    response = await app_module.run(
        WorkerRequest(
            task_id="task-validation-timeout-fast",
            goal="Change WORKER_STAGE_TIMEOUT_SECONDS to 3600 in worker.py",
            repository="Feberdin/local-multi-agent-company",
            local_repo_path=str(tmp_path / "workspace" / "local-multi-agent-company"),
            base_branch="main",
            metadata={
                "task_profile": {
                    "name": "worker_stage_timeout_config_fix",
                    "target_timeout_seconds": 3600.0,
                    "target_files": [
                        "services/shared/agentic_lab/config.py",
                        "README.md",
                        "docs/configuration.md",
                        "docs/troubleshooting.md",
                    ],
                }
            },
            prior_results={
                "coding": {
                    "outputs": {
                        "changed_files": [
                            "services/shared/agentic_lab/config.py",
                            "README.md",
                        ]
                    }
                }
            },
        )
    )

    assert response.success is True
    assert response.outputs["release_readiness"] == "beta"
    assert "services/shared/agentic_lab/config.py" in response.outputs["fulfilled"][0]


@pytest.mark.asyncio
async def test_validation_worker_uses_deterministic_readme_top_block_fast_path(tmp_path, monkeypatch) -> None:
    app_module = _load_validation_module(tmp_path, monkeypatch)

    response = await app_module.run(
        WorkerRequest(
            task_id="task-validation-readme-block-fast",
            goal="Add a self-improvement block at the top of the README.md file",
            repository="Feberdin/local-multi-agent-company",
            local_repo_path=str(tmp_path / "workspace" / "local-multi-agent-company"),
            base_branch="main",
            metadata={"task_profile": {"name": "readme_top_block_fix"}},
            prior_results={
                "coding": {
                    "outputs": {
                        "changed_files": ["README.md"],
                        "inserted_section_title": "Self-Improvement",
                    }
                }
            },
        )
    )

    assert response.success is True
    assert response.outputs["release_readiness"] == "beta"
    assert any("Self-Improvement" in item for item in response.outputs["fulfilled"])
