"""
Purpose: Verify Git runtime bootstrap and isolated task workspaces for mounted repositories on self-hosted Docker hosts.
Input/Output: These tests create temporary Git repos, then inspect HOME handling, safe.directory registration,
and task-local checkouts.
Important invariants: Git must always get a writable HOME, mounted repos must be marked safe,
and task work must not reuse a dirty shared checkout.
How to debug: If these tests fail, inspect `services/shared/agentic_lab/repo_tools.py`
and compare the created temp repos with the expected task workspace path.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from services.shared.agentic_lab.config import get_settings
from services.shared.agentic_lab.repo_tools import ensure_repository_checkout, prepare_git_environment, run_command


def _git(repo_path: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(repo_path),
        text=True,
        capture_output=True,
        check=True,
    )
    return completed.stdout.strip()


def _init_repo(repo_path: Path, *, origin_url: str) -> None:
    repo_path.mkdir(parents=True, exist_ok=True)
    _git(repo_path, "init", "-b", "main")
    _git(repo_path, "config", "user.name", "Tester")
    _git(repo_path, "config", "user.email", "tester@example.com")
    _git(repo_path, "remote", "add", "origin", origin_url)


def test_prepare_git_environment_creates_writable_home(monkeypatch, tmp_path: Path) -> None:
    runtime_home = tmp_path / "runtime-home"
    monkeypatch.setenv("RUNTIME_HOME_DIR", str(runtime_home))
    get_settings.cache_clear()

    env = prepare_git_environment()
    run_command(["git", "config", "--global", "user.name", "Feberdin Test"], env=env)

    assert env["HOME"] == str(runtime_home)
    assert Path(env["GIT_CONFIG_GLOBAL"]).exists()
    assert "name = Feberdin Test" in Path(env["GIT_CONFIG_GLOBAL"]).read_text(encoding="utf-8")


def test_prepare_git_environment_registers_safe_directory(monkeypatch, tmp_path: Path) -> None:
    runtime_home = tmp_path / "runtime-home"
    repo_path = tmp_path / "workspace" / "demo-repo"
    _init_repo(repo_path, origin_url="https://github.com/Feberdin/demo-repo.git")

    monkeypatch.setenv("RUNTIME_HOME_DIR", str(runtime_home))
    get_settings.cache_clear()

    env = prepare_git_environment(repo_path)
    safe_directories = run_command(
        ["git", "config", "--global", "--get-all", "safe.directory"],
        env=env,
    ).stdout.splitlines()

    assert str(repo_path.resolve()) in safe_directories


def test_ensure_repository_checkout_uses_clean_task_workspace_for_dirty_source_repo(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    runtime_home = tmp_path / "runtime-home"
    source_repo_path = workspace_root / "local-multi-agent-company"
    origin_url = "https://github.com/Feberdin/local-multi-agent-company.git"

    _init_repo(source_repo_path, origin_url=origin_url)
    (source_repo_path / "README.md").write_text("# Clean\n", encoding="utf-8")
    _git(source_repo_path, "add", "README.md")
    _git(source_repo_path, "commit", "-m", "Initial commit")
    (source_repo_path / "README.md").write_text("# Dirty source checkout\n", encoding="utf-8")

    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setenv("RUNTIME_HOME_DIR", str(runtime_home))
    get_settings.cache_clear()

    isolated_repo_path = ensure_repository_checkout(
        repository="Feberdin/local-multi-agent-company",
        repo_path=source_repo_path,
        workspace_root=workspace_root,
        base_branch="main",
        repo_url=origin_url,
        task_id="task-1",
        source_repo_path=source_repo_path,
    )

    assert isolated_repo_path != source_repo_path
    assert ".task-workspaces/task-1" in str(isolated_repo_path)
    assert (isolated_repo_path / ".git").exists()
    assert (isolated_repo_path / "README.md").read_text(encoding="utf-8") == "# Clean\n"
    assert (source_repo_path / "README.md").read_text(encoding="utf-8") == "# Dirty source checkout\n"
    assert _git(isolated_repo_path, "config", "--get", "remote.origin.url") == origin_url
