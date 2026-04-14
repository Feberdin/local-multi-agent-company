"""
Purpose: Verify that the GitHub worker can prepare authenticated pushes without leaking token handling into repo config.
Input/Output: Tests feed representative origin URLs and mocked worker dependencies into the GitHub worker helpers.
Important invariants:
  - The worker must reuse GITHUB_TOKEN for both git push and pull-request creation.
  - Token-based push auth must be scoped to one push command instead of rewriting the repository remote permanently.
How to debug: If this fails, inspect services/github_worker/app.py and compare the generated push env with the failing bundle.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from services.shared.agentic_lab import config as config_module
from services.shared.agentic_lab.schemas import WorkerRequest


def _worker_request(repo_path: Path) -> WorkerRequest:
    """Create a minimal GitHub worker request for one isolated temporary repository path."""

    return WorkerRequest(
        task_id="task-github",
        goal="Füge am Anfang der Readme einen Smiley ein.",
        repository="Feberdin/local-multi-agent-company",
        local_repo_path=str(repo_path),
        base_branch="main",
        branch_name="feature/readme-smiley",
        prior_results={},
    )


def _load_github_worker_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> object:
    """Import the GitHub worker only after test-local runtime paths are configured."""

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("RUNTIME_HOME_DIR", str(tmp_path / "runtime-home"))
    config_module.get_settings.cache_clear()
    module_name = "services.github_worker.app"
    if module_name in sys.modules:
        return importlib.reload(sys.modules[module_name])
    return importlib.import_module(module_name)


def test_build_authenticated_push_url_supports_https_and_ssh_origins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github_worker_app = _load_github_worker_module(tmp_path, monkeypatch)
    token = "ghs_test/token"

    https_url = github_worker_app._build_authenticated_push_url(  # pyright: ignore[reportPrivateUsage]
        "https://github.com/Feberdin/local-multi-agent-company.git",
        token,
    )
    ssh_url = github_worker_app._build_authenticated_push_url(  # pyright: ignore[reportPrivateUsage]
        "git@github.com:Feberdin/local-multi-agent-company.git",
        token,
    )
    ssh_scheme_url = github_worker_app._build_authenticated_push_url(  # pyright: ignore[reportPrivateUsage]
        "ssh://git@github.com/Feberdin/local-multi-agent-company.git",
        token,
    )

    assert https_url == (
        "https://x-access-token:ghs_test%2Ftoken@github.com/Feberdin/local-multi-agent-company.git"
    )
    assert ssh_url == https_url
    assert ssh_scheme_url == https_url


def test_build_authenticated_push_env_injects_ephemeral_pushurl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github_worker_app = _load_github_worker_module(tmp_path, monkeypatch)
    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    def fake_git(args: list[str], repo_path: Path, timeout: int = 300, check: bool = True) -> str:  # noqa: ARG001
        if args == ["config", "--get", "remote.origin.url"]:
            return "git@github.com:Feberdin/local-multi-agent-company.git"
        raise AssertionError(f"Unexpected git call: {args}")

    monkeypatch.setattr(github_worker_app, "git", fake_git)
    monkeypatch.setattr(github_worker_app.settings, "github_token", "ghs_test_token")

    push_env = github_worker_app._build_authenticated_push_env(  # pyright: ignore[reportPrivateUsage]
        repo_path,
        {"CUSTOM_ENV": "1"},
    )

    assert push_env["CUSTOM_ENV"] == "1"
    assert push_env["GIT_CONFIG_COUNT"] == "1"
    assert push_env["GIT_CONFIG_KEY_0"] == "remote.origin.pushurl"
    assert push_env["GIT_CONFIG_VALUE_0"] == (
        "https://x-access-token:ghs_test_token@github.com/Feberdin/local-multi-agent-company.git"
    )


@pytest.mark.asyncio
async def test_run_uses_authenticated_push_env_for_git_push(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github_worker_app = _load_github_worker_module(tmp_path, monkeypatch)
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    captured_commands: list[tuple[list[str], dict[str, str] | None]] = []

    def fake_current_diff(repo_path: Path, base_branch: str) -> dict[str, object]:  # noqa: ARG001
        return {"changed_files": ["README.md"], "diff_stat": "README.md | 1 +"}

    def fake_run_command(
        args: list[str],
        cwd: Path | None = None,  # noqa: ARG001
        env: dict[str, str] | None = None,
        timeout: int = 300,  # noqa: ARG001
        check: bool = True,  # noqa: ARG001
    ) -> CompletedProcess[str]:
        captured_commands.append((args, env))
        return CompletedProcess(args, 0, stdout="", stderr="")

    def fake_git(args: list[str], repo_path: Path, timeout: int = 300, check: bool = True) -> str:  # noqa: ARG001
        if args == ["config", "--get", "remote.origin.url"]:
            return "https://github.com/Feberdin/local-multi-agent-company.git"
        if args == ["rev-parse", "HEAD"]:
            return "abc123"
        return ""

    class _FakeGitHubClient:
        async def create_pull_request(self, **kwargs: object) -> dict[str, object]:
            return {"html_url": "https://github.com/Feberdin/local-multi-agent-company/pull/7", "number": 7}

    monkeypatch.setattr(github_worker_app, "current_diff", fake_current_diff)
    monkeypatch.setattr(github_worker_app, "run_command", fake_run_command)
    monkeypatch.setattr(github_worker_app, "git", fake_git)
    monkeypatch.setattr(github_worker_app, "write_report", lambda *args, **kwargs: tmp_path / "github-report.json")
    monkeypatch.setattr(github_worker_app, "github_client", _FakeGitHubClient())
    monkeypatch.setattr(github_worker_app.settings, "github_token", "ghs_push_token")

    response = await github_worker_app.run(_worker_request(repo_path))

    assert response.success is True
    push_command, push_env = next(item for item in captured_commands if item[0][:2] == ["git", "push"])
    assert push_command == ["git", "push", "--set-upstream", "origin", "feature/readme-smiley"]
    assert push_env is not None
    assert push_env["GIT_CONFIG_KEY_0"] == "remote.origin.pushurl"
    assert push_env["GIT_CONFIG_VALUE_0"] == (
        "https://x-access-token:ghs_push_token@github.com/Feberdin/local-multi-agent-company.git"
    )


@pytest.mark.asyncio
async def test_run_returns_clear_preflight_error_when_github_token_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github_worker_app = _load_github_worker_module(tmp_path, monkeypatch)
    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    def fake_current_diff(repo_path: Path, base_branch: str) -> dict[str, object]:  # noqa: ARG001
        return {"changed_files": ["README.md"], "diff_stat": "README.md | 1 +"}

    def fake_git(args: list[str], repo_path: Path, timeout: int = 300, check: bool = True) -> str:  # noqa: ARG001
        if args == ["config", "--get", "remote.origin.url"]:
            return "https://github.com/Feberdin/local-multi-agent-company.git"
        return ""

    def fail_run_command(*args: object, **kwargs: object) -> CompletedProcess[str]:
        raise AssertionError("run_command should not be reached when GITHUB_TOKEN is missing.")

    monkeypatch.setattr(github_worker_app, "current_diff", fake_current_diff)
    monkeypatch.setattr(github_worker_app, "git", fake_git)
    monkeypatch.setattr(github_worker_app, "run_command", fail_run_command)
    monkeypatch.setattr(github_worker_app.settings, "github_token", "")

    response = await github_worker_app.run(_worker_request(repo_path))

    assert response.success is False
    assert response.summary == "GitHub push preflight failed."
    assert "GITHUB_TOKEN fehlt" in response.errors[0]
