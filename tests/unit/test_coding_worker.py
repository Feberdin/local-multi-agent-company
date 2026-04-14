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


def _git_output(repo_path: Path, *args: str) -> str:
    """Return stdout from one git command inside the temporary test repository."""

    completed = subprocess.run(
        ["git", *args],
        cwd=repo_path,
        check=True,
        text=True,
        capture_output=True,
    )
    return completed.stdout.strip()


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


def test_git_revert_backend_prepares_a_deterministic_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _coding_settings(tmp_path, monkeypatch)
    coding_app = _load_coding_module()
    repo_path = _create_repo(tmp_path)
    target_file = repo_path / "worker_target.py"
    target_file.write_text(
        "def target_function():\n"
        "    return 'newer-value'\n",
        encoding="utf-8",
    )
    _run_git(repo_path, "add", "worker_target.py")
    _run_git(repo_path, "commit", "-m", "introduce regression")
    reverted_commit = _git_output(repo_path, "rev-parse", "HEAD")
    _run_git(repo_path, "checkout", "-b", "feature/test-rollback")

    monkeypatch.setattr(coding_app, "settings", settings)

    request = WorkerRequest(
        task_id="task-rollback",
        goal="Revertiere den fehlerhaften Commit.",
        repository="Feberdin/local-multi-agent-company",
        local_repo_path=str(repo_path),
        base_branch="main",
        branch_name="feature/test-rollback",
        metadata={"rollback_commit_sha": reverted_commit},
        prior_results={},
    )

    response = coding_app._run_git_revert_backend(  # pyright: ignore[reportPrivateUsage]
        request,
        repo_path,
        "feature/test-rollback",
        reverted_commit,
    )

    assert response.success is True
    assert response.outputs["backend"] == "git_revert"
    assert response.outputs["rollback_commit_sha"] == reverted_commit
    assert response.outputs["changed_files"] == ["worker_target.py"]
    assert target_file.read_text(encoding="utf-8") == (
        "def target_function():\n"
        "    return 'old-value'\n"
    )
    assert Path(response.artifacts[0].path).exists() is True


def test_candidate_search_considers_config_and_shell_files_when_python_hints_are_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _coding_settings(tmp_path, monkeypatch)
    coding_app = _load_coding_module()
    repo_path = _create_repo(tmp_path)
    (repo_path / "docker-compose.override.yaml").write_text(
        "services:\n  app:\n    healthcheck:\n      test: ['CMD', 'curl', '-f', 'http://localhost/health']\n",
        encoding="utf-8",
    )
    (repo_path / "scripts").mkdir()
    (repo_path / "scripts" / "health-check.sh").write_text("#!/bin/sh\necho healthcheck\n", encoding="utf-8")

    candidates = coding_app._grep_for_candidates(  # pyright: ignore[reportPrivateUsage]
        repo_path,
        "Improve the healthcheck handling in the local deployment scripts",
        max_files=6,
    )

    assert "docker-compose.override.yaml" in candidates or "scripts/health-check.sh" in candidates


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


@pytest.mark.asyncio
async def test_local_patch_backend_recovers_when_primary_model_returns_response_wrapper_json(
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
            content = '{"response":"wrapped answer instead of direct edit_plan"}'
        else:
            call_counter["mistral"] += 1
            content = (
                '{"summary":"Recover from wrapped response JSON.",'
                '"operations":['
                '{"action":"replace_symbol_body",'
                '"file_path":"worker_target.py",'
                '"symbol_name":"target_function",'
                '"reason":"Fallback model must return the real edit plan contract.",'
                '"new_content":"def target_function():\\n    return \\"wrapped-recovered\\"\\n"}'
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
        task_id="task-response-wrapper",
        goal="Add error handling to the git clone command in the local patch backend",
        repository="Feberdin/local-multi-agent-company",
        local_repo_path=str(repo_path),
        base_branch="main",
        branch_name="feature/test-response-wrapper",
        metadata={},
        prior_results={
            "requirements": {"outputs": {"requirements": ["Implement clone error handling."]}},
            "architecture": {"outputs": {"touched_areas": ["worker_target.py"]}},
            "research": {"outputs": {"candidate_files": ["worker_target.py"]}},
        },
    )

    response = await coding_app._run_local_patch_backend(  # pyright: ignore[reportPrivateUsage]
        request,
        repo_path,
        "feature/test-response-wrapper",
    )

    assert response.success is True
    assert call_counter["qwen"] == 2
    assert call_counter["mistral"] == 1
    assert (repo_path / "worker_target.py").read_text(encoding="utf-8").endswith('return "wrapped-recovered"\n')


@pytest.mark.asyncio
async def test_local_patch_backend_uses_deterministic_readme_smiley_fast_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _coding_settings(tmp_path, monkeypatch)
    coding_app = _load_coding_module()
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / "README.md").write_text("Probe README\nSecond line\n", encoding="utf-8")
    _run_git(repo_path, "init", "-b", "main")
    _run_git(repo_path, "config", "user.email", "test@example.com")
    _run_git(repo_path, "config", "user.name", "Test User")
    _run_git(repo_path, "add", "README.md")
    _run_git(repo_path, "commit", "-m", "initial")

    monkeypatch.setattr(coding_app, "settings", settings)

    response = await coding_app._run_local_patch_backend(  # pyright: ignore[reportPrivateUsage]
        WorkerRequest(
            task_id="task-readme-fast",
            goal="Fuege am Anfang der Readme einen Smiley ein.",
            repository="Feberdin/local-multi-agent-company",
            local_repo_path=str(repo_path),
            base_branch="main",
            branch_name="feature/readme-fast",
            metadata={"task_profile": {"name": "readme_prefix_smiley_fix"}},
            prior_results={},
        ),
        repo_path,
        "feature/readme-fast",
    )

    assert response.success is True
    assert response.outputs["changed_files"] == ["README.md"]
    assert response.outputs["deterministic_strategy"] == "prepend_ascii_smiley_to_readme_first_line"
    assert (repo_path / "README.md").read_text(encoding="utf-8").startswith(":) Probe README\n")


@pytest.mark.asyncio
async def test_local_patch_backend_recovers_when_primary_model_returns_incomplete_edit_operation(
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
            content = '{"summary":"Broken plan","operations":[{"action":"create_or_update"}]}'
        else:
            call_counter["mistral"] += 1
            content = (
                '{"summary":"Recover from incomplete edit operation.",'
                '"operations":['
                '{"action":"replace_symbol_body",'
                '"file_path":"worker_target.py",'
                '"symbol_name":"target_function",'
                '"reason":"Fallback model must return a complete operation schema.",'
                '"new_content":"def target_function():\\n    return \\"semantically-recovered\\"\\n"}'
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
        task_id="task-incomplete-operation",
        goal="Add error handling to the git clone command in the local patch backend",
        repository="Feberdin/local-multi-agent-company",
        local_repo_path=str(repo_path),
        base_branch="main",
        branch_name="feature/test-incomplete-operation",
        metadata={},
        prior_results={
            "requirements": {"outputs": {"requirements": ["Implement clone error handling."]}},
            "architecture": {"outputs": {"touched_areas": ["worker_target.py"]}},
            "research": {"outputs": {"candidate_files": ["worker_target.py"]}},
        },
    )

    response = await coding_app._run_local_patch_backend(  # pyright: ignore[reportPrivateUsage]
        request,
        repo_path,
        "feature/test-incomplete-operation",
    )

    assert response.success is True
    assert call_counter["qwen"] == 2
    assert call_counter["mistral"] == 1
    assert (repo_path / "worker_target.py").read_text(encoding="utf-8").endswith('return "semantically-recovered"\n')


@pytest.mark.asyncio
async def test_local_patch_backend_recovers_when_primary_model_returns_generic_empty_plan(
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
            content = (
                '{"summary":"User shared a comprehensive setup for a self-hosted AI agent system on Unraid.",'
                '"operations":[],'
                '"blocking_reason":"No specific code change or operation requested."}'
            )
        else:
            call_counter["mistral"] += 1
            content = (
                '{"summary":"Recover from generic empty coding plan.",'
                '"operations":['
                '{"action":"replace_symbol_body",'
                '"file_path":"worker_target.py",'
                '"symbol_name":"target_function",'
                '"reason":"Fallback provider must replace the generic no-op answer with a real edit.",'
                '"new_content":"def target_function():\\n    return \\"generic-plan-recovered\\"\\n"}'
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
        task_id="task-generic-noop",
        goal="Add error handling to the git clone command in the local patch backend",
        repository="Feberdin/local-multi-agent-company",
        local_repo_path=str(repo_path),
        base_branch="main",
        branch_name="feature/test-generic-noop",
        metadata={},
        prior_results={
            "requirements": {"outputs": {"requirements": ["Implement clone error handling."]}},
            "architecture": {"outputs": {"touched_areas": ["worker_target.py"]}},
            "research": {"outputs": {"candidate_files": ["worker_target.py"]}},
        },
    )

    response = await coding_app._run_local_patch_backend(  # pyright: ignore[reportPrivateUsage]
        request,
        repo_path,
        "feature/test-generic-noop",
    )

    assert response.success is True
    assert call_counter["qwen"] == 2
    assert call_counter["mistral"] == 1
    assert (repo_path / "worker_target.py").read_text(encoding="utf-8").endswith('return "generic-plan-recovered"\n')


@pytest.mark.asyncio
async def test_local_patch_backend_recovers_when_primary_model_returns_german_empty_summary_without_blocker(
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
            content = (
                '{"summary":"Keine Dateiänderungen erforderlich: Es wurden keine spezifischen Änderungen oder Aufgaben '
                'bereitgestellt, die eine Bearbeitung erfordern.",'
                '"operations":[]}'
            )
        else:
            call_counter["mistral"] += 1
            content = (
                '{"summary":"Recover from the German generic empty coding plan.",'
                '"operations":['
                '{"action":"replace_symbol_body",'
                '"file_path":"worker_target.py",'
                '"symbol_name":"target_function",'
                '"reason":"Fallback provider must replace the German generic no-op answer with a real edit.",'
                '"new_content":"def target_function():\\n    return \\"german-plan-recovered\\"\\n"}'
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
        task_id="task-german-noop",
        goal="Add error handling to the git clone command in the local patch backend",
        repository="Feberdin/local-multi-agent-company",
        local_repo_path=str(repo_path),
        base_branch="main",
        branch_name="feature/test-german-noop",
        metadata={},
        prior_results={
            "requirements": {"outputs": {"requirements": ["Implement clone error handling."]}},
            "architecture": {"outputs": {"touched_areas": ["worker_target.py"]}},
            "research": {"outputs": {"candidate_files": ["worker_target.py"]}},
        },
    )

    response = await coding_app._run_local_patch_backend(  # pyright: ignore[reportPrivateUsage]
        request,
        repo_path,
        "feature/test-german-noop",
    )

    assert response.success is True
    assert call_counter["qwen"] == 2
    assert call_counter["mistral"] == 1
    assert (repo_path / "worker_target.py").read_text(encoding="utf-8").endswith('return "german-plan-recovered"\n')


@pytest.mark.asyncio
async def test_local_patch_backend_retries_with_focused_contract_recovery_after_llm_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _coding_settings(tmp_path, monkeypatch)
    coding_app = _load_coding_module()
    repo_path = _create_repo(tmp_path)

    class _StubLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete_json(self, system_prompt: str, user_prompt: str, **_kwargs) -> dict[str, object]:
            self.calls += 1
            if self.calls == 1:
                raise coding_app.LLMError("Model did not return valid JSON for `coding`.")
            assert "Relevant code excerpts:" in user_prompt
            assert "Previous contract failure:" in user_prompt
            return {
                "summary": "Recover after contract failure with one concrete edit.",
                "operations": [
                    {
                        "action": "replace_symbol_body",
                        "file_path": "worker_target.py",
                        "symbol_name": "target_function",
                        "reason": "The focused contract-recovery prompt should produce one valid edit.",
                        "new_content": 'def target_function():\n    return "recovered-after-error"\n',
                    }
                ],
            }

    class _GuidanceStub:
        def guidance_prompt_block(self, request: WorkerRequest, worker_name: str) -> str:  # noqa: ARG002
            return ""

    monkeypatch.setattr(coding_app, "settings", settings)
    monkeypatch.setattr(coding_app, "llm", _StubLLM())
    monkeypatch.setattr(coding_app, "worker_governance", _GuidanceStub())

    request = WorkerRequest(
        task_id="task-contract-recovery",
        goal="Add error handling to the git clone command in the local patch backend",
        repository="Feberdin/local-multi-agent-company",
        local_repo_path=str(repo_path),
        base_branch="main",
        branch_name="feature/test-contract-recovery",
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
        "feature/test-contract-recovery",
    )

    assert response.success is True
    assert (repo_path / "worker_target.py").read_text(encoding="utf-8").endswith('return "recovered-after-error"\n')


def test_extract_relevant_file_excerpt_focuses_git_clone_region(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = _coding_settings(tmp_path, monkeypatch)
    coding_app = _load_coding_module()

    content = "\n".join(
        [
            "def helper():",
            "    return 'ignore'",
            "",
            "def ensure_repository_checkout(repo_url, repo_dir):",
            "    subprocess.run(['git', 'clone', repo_url, repo_dir])",
            "    return repo_dir",
            "",
            "def tail():",
            "    return 'done'",
        ]
    )

    excerpt = coding_app._extract_relevant_file_excerpt(  # pyright: ignore[reportPrivateUsage]
        "services/shared/agentic_lab/repo_tools.py",
        content,
        keywords=["git", "clone", "ensure_repository_checkout"],
        file_index=None,
    )

    assert "git', 'clone'" in excerpt
    assert "ensure_repository_checkout" in excerpt
    assert "# services/shared/agentic_lab/repo_tools.py" in excerpt
    assert settings is not None


def test_coding_user_prompt_renders_candidate_scope_as_readable_targets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _coding_settings(tmp_path, monkeypatch)
    coding_app = _load_coding_module()

    prompt = coding_app._coding_user_prompt(  # pyright: ignore[reportPrivateUsage]
        "Add error handling to the git clone command in the local patch backend",
        {"requirements": ["Implement clone error handling."]},
        {"touched_areas": ["services/shared/agentic_lab/repo_tools.py"]},
        {"candidate_files": ["services/shared/agentic_lab/repo_tools.py"]},
        {"file_count": 2},
            {
                "services/shared/agentic_lab/repo_tools.py": (
                    "# services/shared/agentic_lab/repo_tools.py\n"
                    "[lines 250-275]\n"
                    "0250: def _clone_target_from_best_source(\n"
                    "0251:     *,\n"
                    "0252:     repository: str,\n"
                    "0253:     repo_path: Path,\n"
                    "0254: ) -> None:\n"
                    "0271:     run_command(\n"
                    "0272:         [\"git\", \"clone\", \"--branch\", base_branch, \"--single-branch\", clone_source, str(repo_path)],\n"
                    "0273:         env=clone_env,\n"
                    "0274:         timeout=900,\n"
                    "0275:     )\n"
                    "0280: def ensure_repository_checkout(\n"
                )
            },
        "Symbol index:\n- services/shared/agentic_lab/repo_tools.py:271 _clone_target_from_best_source",
        ["services/shared/agentic_lab/repo_tools.py", "services/coding_worker/app.py"],
    )

    assert "Likely implementation targets:" in prompt
    assert "- services/shared/agentic_lab/repo_tools.py" in prompt
    assert "Relevant symbols: _clone_target_from_best_source, ensure_repository_checkout." in prompt
    assert "Candidate file contents:\n# services/shared/agentic_lab/repo_tools.py" in prompt
    assert 'Candidate files:\n- services/shared/agentic_lab/repo_tools.py\n- services/coding_worker/app.py' in prompt
    assert "Do not claim that no target file was provided when candidate files are listed above." in prompt
    assert "{'services/shared/agentic_lab/repo_tools.py'" not in prompt


def test_select_candidate_files_prioritizes_real_fix_targets_over_generic_repo_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _coding_settings(tmp_path, monkeypatch)
    coding_app = _load_coding_module()
    repo_path = _create_repo(tmp_path)

    for relative_path, content in {
        "README.md": "Git clone setup notes for operators.\n",
        "docker-compose.yml": "services:\n  app:\n    image: feberdin/demo\n",
        "pyproject.toml": "[project]\nname = 'demo'\n",
        "services/coding_worker/app.py": (
            "def run_local_patch_backend():\n"
            "    return 'local patch backend'\n"
        ),
        "services/shared/agentic_lab/repo_tools.py": (
            "def ensure_repository_checkout():\n"
            "    run_command(['git', 'clone', '--branch', 'main', 'demo', '/tmp/repo'])\n"
        ),
    }.items():
        file_path = repo_path / relative_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")

    candidate_files = coding_app._select_candidate_files(  # pyright: ignore[reportPrivateUsage]
        repo_path=repo_path,
        goal="Add error handling to the git clone command in the local patch backend",
        requirements={"requirements": ["Handle git clone failures in ensure_repository_checkout."]},
        architecture={
            "summary": "The clone path is in services/shared/agentic_lab/repo_tools.py.",
            "touched_areas": [
                "README.md",
                "docker-compose.yml",
                "pyproject.toml",
                "services/coding_worker/app.py",
            ],
            "implementation_plan": [
                "Update services/shared/agentic_lab/repo_tools.py first.",
                "Keep services/coding_worker/app.py aligned if needed.",
            ],
        },
        research={
            "candidate_files": [
                "services/shared/agentic_lab/repo_tools.py",
                "services/coding_worker/app.py",
            ],
            "research_notes": (
                "The failing git clone command lives in ensure_repository_checkout "
                "inside services/shared/agentic_lab/repo_tools.py."
            ),
        },
    )

    assert candidate_files[:2] == [
        "services/shared/agentic_lab/repo_tools.py",
        "services/coding_worker/app.py",
    ]
    assert "README.md" not in candidate_files[:2]


@pytest.mark.asyncio
async def test_local_patch_backend_applies_replace_lines_without_rewriting_the_whole_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _coding_settings(tmp_path, monkeypatch)
    coding_app = _load_coding_module()
    repo_path = _create_repo(tmp_path)

    async def handler(request: httpx.Request) -> httpx.Response:
        content = (
            '{"summary":"Tighten one return line only.",'
            '"operations":['
            '{"action":"replace_lines",'
            '"file_path":"worker_target.py",'
            '"start_line":2,'
            '"end_line":2,'
            '"reason":"Only the return line should change.",'
            '"new_content":"    return \\"line-edited\\""}'
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
        task_id="task-lines",
        goal="Change only the return line in worker_target.py.",
        repository="Feberdin/local-multi-agent-company",
        local_repo_path=str(repo_path),
        base_branch="main",
        branch_name="feature/test-replace-lines",
        metadata={},
        prior_results={
            "requirements": {"outputs": {"requirements": ["Change only one return line."]}},
            "architecture": {"outputs": {"touched_areas": ["worker_target.py"]}},
            "research": {"outputs": {"candidate_files": ["worker_target.py"]}},
        },
    )

    response = await coding_app._run_local_patch_backend(  # pyright: ignore[reportPrivateUsage]
        request,
        repo_path,
        "feature/test-replace-lines",
    )

    changed_text = (repo_path / "worker_target.py").read_text(encoding="utf-8")
    assert response.success is True
    assert response.outputs["operation_results"][0]["strategy"] == "replace_lines"
    assert changed_text == 'def target_function():\n    return "line-edited"\n'


@pytest.mark.asyncio
async def test_local_patch_backend_accepts_short_json_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A very short valid JSON response must be enough for the worker to perform one targeted edit."""

    settings = _coding_settings(tmp_path, monkeypatch)
    coding_app = _load_coding_module()
    repo_path = _create_repo(tmp_path)

    async def handler(request: httpx.Request) -> httpx.Response:
        content = (
            '{"summary":"Kurz.",'
            '"operations":['
            '{"action":"replace_lines","file_path":"worker_target.py","start_line":2,"end_line":2,'
            '"reason":"Kurz.","new_content":"    return \\"json-kurz\\""}'
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
        task_id="task-short-json",
        goal="Aendere genau eine Rueckgabezeile.",
        repository="Feberdin/local-multi-agent-company",
        local_repo_path=str(repo_path),
        base_branch="main",
        branch_name="feature/test-short-json",
        metadata={},
        prior_results={
            "requirements": {"outputs": {"requirements": ["Nur eine Rueckgabezeile anpassen."]}},
            "architecture": {"outputs": {"touched_areas": ["worker_target.py"]}},
            "research": {"outputs": {"candidate_files": ["worker_target.py"]}},
        },
    )

    response = await coding_app._run_local_patch_backend(  # pyright: ignore[reportPrivateUsage]
        request,
        repo_path,
        "feature/test-short-json",
    )

    assert response.success is True
    assert response.summary == "Kurz."
    assert response.outputs["changed_files"] == ["worker_target.py"]
    assert response.outputs["operation_results"][0]["strategy"] == "replace_lines"
    assert (repo_path / "worker_target.py").read_text(encoding="utf-8") == (
        'def target_function():\n    return "json-kurz"\n'
    )


@pytest.mark.asyncio
async def test_local_patch_backend_retries_after_empty_operation_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _coding_settings(tmp_path, monkeypatch)
    coding_app = _load_coding_module()
    repo_path = _create_repo(tmp_path)
    call_counter = {"qwen": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        call_counter["qwen"] += 1
        if call_counter["qwen"] == 1:
            content = '{"summary":"No code changes needed after review.","operations":[]}'
        else:
            content = (
                '{"summary":"Add the requested clone error handling.",'
                '"operations":['
                '{"action":"replace_symbol_body",'
                '"file_path":"worker_target.py",'
                '"symbol_name":"target_function",'
                '"reason":"The retry must produce a concrete targeted operation.",'
                '"new_content":"def target_function():\\n    return \\"retried-value\\"\\n"}'
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
        task_id="task-retry",
        goal="Add error handling to the git clone command in the local patch backend",
        repository="Feberdin/local-multi-agent-company",
        local_repo_path=str(repo_path),
        base_branch="main",
        branch_name="feature/test-noop-retry",
        metadata={},
        prior_results={
            "requirements": {"outputs": {"requirements": ["Implement clone error handling."]}},
            "architecture": {"outputs": {"touched_areas": ["worker_target.py"]}},
            "research": {"outputs": {"candidate_files": ["worker_target.py"]}},
        },
    )

    response = await coding_app._run_local_patch_backend(  # pyright: ignore[reportPrivateUsage]
        request,
        repo_path,
        "feature/test-noop-retry",
    )

    assert response.success is True
    assert call_counter["qwen"] == 2
    assert (repo_path / "worker_target.py").read_text(encoding="utf-8").endswith('return "retried-value"\n')


@pytest.mark.asyncio
async def test_local_patch_backend_recovers_from_nested_change_container_shape(
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
            content = '{"summary":"Kein konkreter Code-Änderungsauftrag erkannt.","operations":[]}'
        else:
            call_counter["mistral"] += 1
            content = (
                '{"summary":"Recover from nested change container.",'
                '"operations":['
                '{'
                '"file":"worker_target.py",'
                '"changes":['
                '{'
                '"location":{"type":"function","name":"target_function"},'
                '"new_code":"def target_function():\\n    return \\"nested-shape-recovered\\"\\n",'
                '"description":"Flatten the nested repair shape into one real edit operation."'
                '}'
                ']'
                '}'
                ']}'
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
        task_id="task-nested-change-shape",
        goal="Add error handling to the git clone command in the local patch backend",
        repository="Feberdin/local-multi-agent-company",
        local_repo_path=str(repo_path),
        base_branch="main",
        branch_name="feature/test-nested-change-shape",
        metadata={},
        prior_results={
            "requirements": {"outputs": {"requirements": ["Implement clone error handling."]}},
            "architecture": {"outputs": {"touched_areas": ["worker_target.py"]}},
            "research": {"outputs": {"candidate_files": ["worker_target.py"]}},
        },
    )

    response = await coding_app._run_local_patch_backend(  # pyright: ignore[reportPrivateUsage]
        request,
        repo_path,
        "feature/test-nested-change-shape",
    )

    assert response.success is True
    assert call_counter["qwen"] == 2
    assert call_counter["mistral"] == 1
    assert (repo_path / "worker_target.py").read_text(encoding="utf-8").endswith(
        'return "nested-shape-recovered"\n'
    )


@pytest.mark.asyncio
async def test_local_patch_backend_persists_failure_diagnostics_when_operations_stay_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _coding_settings(tmp_path, monkeypatch)
    coding_app = _load_coding_module()
    repo_path = _create_repo(tmp_path)

    async def handler(request: httpx.Request) -> httpx.Response:
        content = '{"summary":"No code changes needed after review.","operations":[],"blocking_reason":"Model found no safe patch."}'
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
        task_id="task-noops",
        goal="Add error handling to the git clone command in the local patch backend",
        repository="Feberdin/local-multi-agent-company",
        local_repo_path=str(repo_path),
        base_branch="main",
        branch_name="feature/test-noop-failure",
        metadata={},
        prior_results={
            "requirements": {"outputs": {"requirements": ["Implement clone error handling."]}},
            "architecture": {"outputs": {"touched_areas": ["worker_target.py"]}},
            "research": {"outputs": {"candidate_files": ["worker_target.py"]}},
        },
    )

    response = await coding_app._run_local_patch_backend(  # pyright: ignore[reportPrivateUsage]
        request,
        repo_path,
        "feature/test-noop-failure",
    )

    assert response.success is False
    assert response.summary == "Coding backend returned no file operations."
    assert response.outputs["candidate_files"] == ["worker_target.py"]
    assert response.outputs["patch_plan_summary"] == "No code changes needed after review."
    assert response.outputs["blocking_reason"] == "Model found no safe patch."
    assert len(response.outputs["plan_attempts"]) == 2
    assert response.outputs["plan_attempts"][0]["operation_count"] == 0
    assert response.warnings == [
        "Das Modell hat zwar ein JSON-Objekt geliefert, aber keine konkreten Datei-Operationen vorgeschlagen."
    ]
    assert response.artifacts[0].name == "coding-failure"
    failure_report = Path(response.artifacts[0].path)
    assert failure_report.exists() is True
    assert "worker_target.py" in failure_report.read_text(encoding="utf-8")
