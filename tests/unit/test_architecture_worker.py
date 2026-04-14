"""
Purpose: Verify that architecture outputs keep touched_areas grounded in real repository files.
Input/Output: Tests call the normalization helper with a temporary repo and inspect the resulting file list.
Important invariants: Non-existent touched_areas must not propagate to downstream workers when better real files exist.
How to debug: If this fails, inspect services/architecture_worker/app.py and the research candidate_files used for recovery.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from services.shared.agentic_lab.config import get_settings
from services.shared.agentic_lab.schemas import WorkerRequest


def _load_architecture_module(tmp_path: Path, monkeypatch) -> object:
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("STAGING_STACK_ROOT", str(tmp_path / "staging-stacks"))
    get_settings.cache_clear()
    module = importlib.import_module("services.architecture_worker.app")
    return importlib.reload(module)


def test_normalize_architecture_outputs_filters_missing_paths_and_uses_research_candidates(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app_module = _load_architecture_module(tmp_path, monkeypatch)
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    existing_file = repo_path / "services" / "coding_worker" / "app.py"
    existing_file.parent.mkdir(parents=True, exist_ok=True)
    existing_file.write_text("print('ok')\n", encoding="utf-8")
    fallback_file = repo_path / "services" / "shared" / "agentic_lab" / "repo_tools.py"
    fallback_file.parent.mkdir(parents=True, exist_ok=True)
    fallback_file.write_text("print('fallback')\n", encoding="utf-8")

    outputs = {
        "summary": "Generic architecture plan.",
        "touched_areas": [
            "services/coding_worker/app.py",
            "services/coding_worker/task_dispatcher.py",
            "services/coding_worker/result_handler.py",
        ],
    }
    research = {
        "candidate_files": [
            "services/shared/agentic_lab/repo_tools.py",
            "README.md",
        ]
    }

    normalized = app_module._normalize_architecture_outputs(outputs, repo_path, research)  # pyright: ignore[reportPrivateUsage]

    assert normalized["touched_areas"] == [
        "services/coding_worker/app.py",
        "services/shared/agentic_lab/repo_tools.py",
    ]


def test_normalize_architecture_outputs_prioritizes_semantically_relevant_source_files(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app_module = _load_architecture_module(tmp_path, monkeypatch)
    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    for relative_path, content in {
        "README.md": "# docs\n",
        "docker-compose.yml": "services:\n  app:\n    image: demo\n",
        "pyproject.toml": "[project]\nname = 'demo'\n",
        "services/coding_worker/app.py": "def coding_worker():\n    return 'ok'\n",
        "services/shared/agentic_lab/repo_tools.py": (
            "def ensure_repository_checkout():\n"
            "    run_command(['git', 'clone', '--branch', 'main', 'demo', '/tmp/repo'])\n"
        ),
    }.items():
        file_path = repo_path / relative_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")

    outputs = {
        "summary": (
            "Wrap git clone failures inside ensure_repository_checkout in "
            "services/shared/agentic_lab/repo_tools.py."
        ),
        "components": [
            {
                "name": "Checkout helper",
                "file_path": "services/shared/agentic_lab/repo_tools.py",
                "description": "Owns the clone path for local repo preparation.",
            }
        ],
        "implementation_plan": [
            "Update services/shared/agentic_lab/repo_tools.py to add guarded clone error handling.",
            "Keep services/coding_worker/app.py aligned with the helper contract.",
        ],
        "touched_areas": [
            "README.md",
            "docker-compose.yml",
            "pyproject.toml",
            "services/coding_worker/app.py",
        ],
    }
    research = {
        "candidate_files": [
            "services/shared/agentic_lab/repo_tools.py",
            "services/coding_worker/app.py",
        ],
        "research_notes": (
            "The git clone path lives in services/shared/agentic_lab/repo_tools.py "
            "inside ensure_repository_checkout."
        ),
    }

    normalized = app_module._normalize_architecture_outputs(outputs, repo_path, research)  # pyright: ignore[reportPrivateUsage]

    assert normalized["touched_areas"][:2] == [
        "services/shared/agentic_lab/repo_tools.py",
        "services/coding_worker/app.py",
    ]
    assert "README.md" not in normalized["touched_areas"][:2]


def test_empty_architecture_fields_detect_semantically_blank_required_values(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app_module = _load_architecture_module(tmp_path, monkeypatch)

    empty_fields = app_module._empty_architecture_fields(  # pyright: ignore[reportPrivateUsage]
        {
            "summary": "",
            "components": [],
            "responsibilities": {"orchestrator": ""},
            "data_flows": [],
            "module_boundaries": "",
            "deployment_strategy": [],
            "logging_strategy": "",
            "implementation_plan": "",
            "test_strategy": [],
            "risks": "",
            "approval_gates": [],
            "touched_areas": ["services/shared/agentic_lab/repo_tools.py"],
        }
    )

    assert empty_fields == [
        "summary",
        "components",
        "responsibilities",
        "data_flows",
        "module_boundaries",
        "deployment_strategy",
        "logging_strategy",
        "implementation_plan",
        "test_strategy",
        "risks",
        "approval_gates",
    ]


@pytest.mark.asyncio
async def test_run_retries_when_required_architecture_fields_are_semantically_empty(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app_module = _load_architecture_module(tmp_path, monkeypatch)
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    target_file = repo_path / "services" / "shared" / "agentic_lab" / "repo_tools.py"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text("def ensure_repository_checkout():\n    return 'ok'\n", encoding="utf-8")

    prompts: list[str] = []

    class _FakeLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete_json(self, *, user_prompt: str, **_kwargs) -> dict[str, object]:
            prompts.append(user_prompt)
            self.calls += 1
            if self.calls == 1:
                return {
                    "summary": "",
                    "components": [],
                    "responsibilities": {},
                    "data_flows": [],
                    "module_boundaries": "",
                    "deployment_strategy": [],
                    "logging_strategy": "",
                    "implementation_plan": "",
                    "test_strategy": [],
                    "risks": "",
                    "approval_gates": [],
                    "touched_areas": ["services/shared/agentic_lab/repo_tools.py"],
                }
            return {
                "summary": "Guard git clone failures in repo_tools.",
                "components": [
                    {
                        "name": "Checkout helper",
                        "description": "Owns local repository preparation.",
                    }
                ],
                "responsibilities": {"checkout helper": "Wrap git clone failures and raise clearer errors."},
                "data_flows": ["Goal -> architecture -> coding against repo_tools.py"],
                "module_boundaries": ["Keep clone preparation inside repo_tools.py."],
                "deployment_strategy": ["No deployment changes required."],
                "logging_strategy": ["Log clone failures with command context."],
                "implementation_plan": ["Update ensure_repository_checkout to catch git clone failures."],
                "test_strategy": ["Add unit coverage for the guarded clone path."],
                "risks": ["Guard against masking the original clone stderr."],
                "approval_gates": ["No additional approval gate required."],
                "touched_areas": ["services/shared/agentic_lab/repo_tools.py"],
            }

    class _GuidanceStub:
        def guidance_prompt_block(self, request: WorkerRequest, worker_name: str) -> str:  # noqa: ARG002
            return ""

    monkeypatch.setattr(app_module, "llm", _FakeLLM())
    monkeypatch.setattr(app_module, "worker_governance", _GuidanceStub())

    response = await app_module.run(
        WorkerRequest(
            task_id="task-architecture-retry",
            goal="Add error handling to the git clone command in the local patch backend",
            repository="Feberdin/local-multi-agent-company",
            local_repo_path=str(repo_path),
            base_branch="main",
            branch_name="feature/architecture-retry",
            prior_results={
                "research": {
                    "outputs": {
                        "candidate_files": ["services/shared/agentic_lab/repo_tools.py"],
                    }
                }
            },
        )
    )

    assert response.success is True
    assert response.outputs["summary"] == "Guard git clone failures in repo_tools."
    assert response.outputs["touched_areas"][0] == "services/shared/agentic_lab/repo_tools.py"
    assert len(prompts) == 2
    assert "required fields were empty or semantically blank" in prompts[1]
    assert "summary, components" in prompts[1]


@pytest.mark.asyncio
async def test_architecture_worker_uses_readme_smiley_fast_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app_module = _load_architecture_module(tmp_path, monkeypatch)
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / "README.md").write_text("Probe README\n", encoding="utf-8")

    response = await app_module.run(
        WorkerRequest(
            task_id="task-architecture-fast",
            goal="Fuege am Anfang der Readme einen Smiley ein.",
            repository="Feberdin/local-multi-agent-company",
            local_repo_path=str(repo_path),
            base_branch="main",
            metadata={"task_profile": {"name": "readme_prefix_smiley_fix"}},
            prior_results={"research": {"outputs": {"candidate_files": ["README.md"]}}},
        )
    )

    assert response.success is True
    assert response.outputs["touched_areas"] == ["README.md"]
    assert response.outputs["implementation_plan"][0] == "Open README.md in the task-local workspace."
