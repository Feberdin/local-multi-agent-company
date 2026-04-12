"""
Purpose: Validate the coding worker's local patch backend under realistic self-hosted failure patterns.
Input/Output: The tests run the local patch backend against a temporary git repository and mocked LLM providers.
Important invariants:
  - A prose-only primary model response must not permanently block coding.
  - A fallback provider may still recover the stage with valid JSON edit operations.
How to debug: If this fails, inspect services/coding_worker/app.py and services/shared/agentic_lab/llm.py together.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from services.shared.agentic_lab import config as config_module
from services.shared.agentic_lab.config import Settings
from services.shared.agentic_lab.llm import LLMClient
from services.shared.agentic_lab.schemas import WorkerRequest


def _run_git(repo_path: Path, *args: str) -> None:
    """Run one git command inside the temporary test repository."""

    subprocess.run(
        ["git", *args],
        cwd=repo_path,
        check=True,
        text=True,
        capture_output=True,
    )


def _coding_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Build a local test Settings object with deterministic model routing and writable runtime paths."""

    routing_file = tmp_path / "model-routing.yaml"
    routing_file.write_text(
        (
            "workers:\n"
            "  coding:\n"
            "    primary_provider: qwen\n"
            "    fallback_provider: mistral\n"
            "    request_timeout_seconds: 0.5\n"
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MODEL_ROUTING_CONFIG", str(routing_file))
    monkeypatch.setenv("MISTRAL_BASE_URL", "http://mistral.test/v1")
    monkeypatch.setenv("MISTRAL_MODEL_NAME", "mistral-small3.2:latest")
    monkeypatch.setenv("QWEN_BASE_URL", "http://qwen.test/v1")
    monkeypatch.setenv("QWEN_MODEL_NAME", "qwen3.5:35b-a3b")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("RUNTIME_HOME_DIR", str(tmp_path / "runtime-home"))
    monkeypatch.setenv("LLM_CONNECT_TIMEOUT_SECONDS", "0.5")
    monkeypatch.setenv("LLM_READ_TIMEOUT_SECONDS", "0.5")
    monkeypatch.setenv("LLM_WRITE_TIMEOUT_SECONDS", "0.5")
    monkeypatch.setenv("LLM_POOL_TIMEOUT_SECONDS", "0.5")
    monkeypatch.setenv("LLM_REQUEST_DEADLINE_SECONDS", "0.5")
    monkeypatch.setenv("CODING_PROVIDER", "local_patch")
    return Settings()


def _create_repo(tmp_path: Path) -> Path:
    """Create one tiny git repository that the local patch backend can edit."""

    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / "worker_target.py").write_text(
        "def target_function():\n"
        "    return 'old-value'\n",
        encoding="utf-8",
    )
    _run_git(repo_path, "init", "-b", "main")
    _run_git(repo_path, "config", "user.email", "test@example.com")
    _run_git(repo_path, "config", "user.name", "Test User")
    _run_git(repo_path, "add", "worker_target.py")
    _run_git(repo_path, "commit", "-m", "initial")
    return repo_path


def _load_coding_module() -> object:
    """Import the coding worker only after test env vars are in place."""

    config_module.get_settings.cache_clear()
    module_name = "services.coding_worker.app"
    if module_name in sys.modules:
        return importlib.reload(sys.modules[module_name])
    return importlib.import_module(module_name)


@pytest.mark.asyncio
async def test_local_patch_backend_recovers_when_primary_model_returns_prose(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _coding_settings(tmp_path, monkeypatch)
    coding_app = _load_coding_module()
    repo_path = _create_repo(tmp_path)
    call_counter = {"qwen": 0, "mistral": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if "qwen.test" in str(request.url):
            call_counter["qwen"] += 1
            content = "# Feberdin Multi-Agent Architecture Review\nThis is prose, not a JSON edit plan."
        else:
            call_counter["mistral"] += 1
            content = (
                '{"summary":"Add guarded error handling for clone preparation.",'
                '"operations":['
                '{"action":"replace_symbol_body",'
                '"file_path":"worker_target.py",'
                '"symbol_name":"target_function",'
                '"reason":"Return the new value so the patch engine proves that JSON fallback worked.",'
                '"new_content":"def target_function():\\n    return \\"new-value\\"\\n"}'
                "]}"
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
            request=request,
        )

    class _GuidanceStub:
        def guidance_prompt_block(self, request: WorkerRequest, worker_name: str) -> str:  # noqa: ARG002
            return ""

    monkeypatch.setattr(coding_app, "settings", settings)
    monkeypatch.setattr(coding_app, "llm", LLMClient(settings, transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(coding_app, "worker_governance", _GuidanceStub())

    request = WorkerRequest(
        task_id="task-123",
        goal="Add error handling to the git clone command in the local patch backend",
        repository="Feberdin/local-multi-agent-company",
        local_repo_path=str(repo_path),
        base_branch="main",
        branch_name="feature/test-json-fallback",
        metadata={},
        prior_results={
            "requirements": {"outputs": {"requirements": ["Handle git clone failures clearly."]}},
            "architecture": {"outputs": {"touched_areas": ["worker_target.py"]}},
            "research": {"outputs": {"candidate_files": ["worker_target.py"]}},
        },
    )

    response = await coding_app._run_local_patch_backend(  # pyright: ignore[reportPrivateUsage]
        request,
        repo_path,
        "feature/test-json-fallback",
    )

    assert response.success is True
    assert "worker_target.py" in response.outputs["changed_files"]
    assert "structured edit operations" not in response.summary.lower()
    assert (repo_path / "worker_target.py").read_text(encoding="utf-8").endswith('return "new-value"\n')
    assert call_counter["qwen"] == 2
    assert call_counter["mistral"] == 1
