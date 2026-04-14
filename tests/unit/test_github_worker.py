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
from services.shared.agentic_lab.repo_tools import CommandError
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
    assert response.outputs["publish_strategy"] == "git_push"


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


@pytest.mark.asyncio
async def test_run_falls_back_to_github_api_when_git_push_is_forbidden(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github_worker_app = _load_github_worker_module(tmp_path, monkeypatch)
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / "README.md").write_text(":) Probe README\n", encoding="utf-8")
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
        if args[:2] == ["git", "push"]:
            raise CommandError(
                "Command git push --set-upstream origin feature/readme-smiley failed with code 128: "
                "remote: Permission to Feberdin/local-multi-agent-company.git denied to Feberdin.\n"
                "fatal: unable to access 'https://github.com/Feberdin/local-multi-agent-company.git/': "
                "The requested URL returned error: 403"
            )
        return CompletedProcess(args, 0, stdout="", stderr="")

    def fake_git(args: list[str], repo_path: Path, timeout: int = 300, check: bool = True) -> str:  # noqa: ARG001
        if args == ["config", "--get", "remote.origin.url"]:
            return "https://github.com/Feberdin/local-multi-agent-company.git"
        if args == ["rev-parse", "HEAD"]:
            return "local-commit-sha"
        if args == ["diff", "--name-status", "--find-renames", "main...HEAD"]:
            return "M\tREADME.md\n"
        if args == ["ls-tree", "HEAD", "--", "README.md"]:
            return "100644 blob abc123\tREADME.md"
        return ""

    class _FallbackGitHubClient:
        async def get_git_ref(self, repository: str, ref_name: str) -> dict[str, object]:  # noqa: ARG002
            assert ref_name == "heads/main"
            return {"object": {"sha": "base-sha"}}

        async def get_git_commit(self, repository: str, commit_sha: str) -> dict[str, object]:  # noqa: ARG002
            assert commit_sha == "base-sha"
            return {"tree": {"sha": "base-tree-sha"}}

        async def create_git_blob(self, repository: str, content_base64: str) -> dict[str, object]:  # noqa: ARG002
            assert content_base64
            return {"sha": "blob-sha"}

        async def create_git_tree(
            self,
            repository: str,  # noqa: ARG002
            *,
            tree: list[dict[str, object]],
            base_tree_sha: str,
        ) -> dict[str, object]:
            assert base_tree_sha == "base-tree-sha"
            assert tree[0]["path"] == "README.md"
            return {"sha": "new-tree-sha"}

        async def create_git_commit(
            self,
            repository: str,  # noqa: ARG002
            *,
            message: str,
            tree_sha: str,
            parent_shas: list[str],
        ) -> dict[str, object]:
            assert "feat(agent-team)" in message
            assert tree_sha == "new-tree-sha"
            assert parent_shas == ["base-sha"]
            return {"sha": "remote-commit-sha"}

        async def create_git_ref(self, repository: str, ref_name: str, commit_sha: str) -> dict[str, object]:  # noqa: ARG002
            assert ref_name == "heads/feature/readme-smiley"
            assert commit_sha == "remote-commit-sha"
            return {"ref": "refs/heads/feature/readme-smiley"}

        async def update_git_ref(  # noqa: ARG002
            self,
            repository: str,
            ref_name: str,
            commit_sha: str,
            *,
            force: bool = False,
        ) -> dict[str, object]:
            raise AssertionError("update_git_ref should not be needed when create_git_ref succeeds.")

        async def create_pull_request(self, **kwargs: object) -> dict[str, object]:
            return {"html_url": "https://github.com/Feberdin/local-multi-agent-company/pull/8", "number": 8}

    monkeypatch.setattr(github_worker_app, "current_diff", fake_current_diff)
    monkeypatch.setattr(github_worker_app, "run_command", fake_run_command)
    monkeypatch.setattr(github_worker_app, "git", fake_git)
    monkeypatch.setattr(github_worker_app, "write_report", lambda *args, **kwargs: tmp_path / "github-report.json")
    monkeypatch.setattr(github_worker_app, "github_client", _FallbackGitHubClient())
    monkeypatch.setattr(github_worker_app.settings, "github_token", "ghs_push_token")

    response = await github_worker_app.run(_worker_request(repo_path))

    assert response.success is True
    assert response.outputs["commit_sha"] == "remote-commit-sha"
    assert response.outputs["publish_strategy"] == "github_api_fallback"
    assert response.outputs["pull_request_number"] == 8


@pytest.mark.asyncio
async def test_run_reuses_local_head_when_retry_has_nothing_new_to_commit(
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
        if args[:2] == ["git", "commit"]:
            raise CommandError(
                "Command git commit failed with code 1: nothing to commit, working tree clean"
            )
        return CompletedProcess(args, 0, stdout="", stderr="")

    def fake_git(args: list[str], repo_path: Path, timeout: int = 300, check: bool = True) -> str:  # noqa: ARG001
        if args == ["config", "--get", "remote.origin.url"]:
            return "https://github.com/Feberdin/local-multi-agent-company.git"
        if args == ["rev-parse", "HEAD"]:
            return "existing-head-sha"
        return ""

    class _FakeGitHubClient:
        async def create_pull_request(self, **kwargs: object) -> dict[str, object]:
            return {"html_url": "https://github.com/Feberdin/local-multi-agent-company/pull/9", "number": 9}

    monkeypatch.setattr(github_worker_app, "current_diff", fake_current_diff)
    monkeypatch.setattr(github_worker_app, "run_command", fake_run_command)
    monkeypatch.setattr(github_worker_app, "git", fake_git)
    monkeypatch.setattr(github_worker_app, "write_report", lambda *args, **kwargs: tmp_path / "github-report.json")
    monkeypatch.setattr(github_worker_app, "github_client", _FakeGitHubClient())
    monkeypatch.setattr(github_worker_app.settings, "github_token", "ghs_push_token")

    response = await github_worker_app.run(_worker_request(repo_path))

    assert response.success is True
    assert response.outputs["commit_sha"] == "existing-head-sha"
    assert response.outputs["local_commit_created"] is False
    push_command, _push_env = next(item for item in captured_commands if item[0][:2] == ["git", "push"])
    assert push_command == ["git", "push", "--set-upstream", "origin", "feature/readme-smiley"]
