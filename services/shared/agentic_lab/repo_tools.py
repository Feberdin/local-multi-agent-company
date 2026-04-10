"""
Purpose: Safe repository and subprocess helpers for research, coding, review, and GitHub automation.
Input/Output: Workers use these functions to inspect repos, create branches, read diffs, and write reports.
Important invariants: Commands run without a shell, repository paths stay inside the workspace, and outputs are truncated for logs.
How to debug: If git operations fail, inspect the command, cwd, and stderr captured by these helpers.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from slugify import slugify

from services.shared.agentic_lab.guardrails import ensure_repo_path_is_safe


class CommandError(RuntimeError):
    """Raised when a subprocess fails and the caller requested strict checking."""


def run_command(
    args: list[str],
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 300,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a command defensively without invoking a shell."""

    completed = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if check and completed.returncode != 0:
        raise CommandError(
            f"Command {' '.join(args)} failed with code {completed.returncode}: {completed.stderr.strip()}"
        )
    return completed


def git(args: list[str], repo_path: Path, timeout: int = 300, check: bool = True) -> str:
    """Run a git command inside a repository and return stdout."""

    completed = run_command(["git", *args], cwd=repo_path, timeout=timeout, check=check)
    return completed.stdout.strip()


def guess_repo_url(repository: str) -> str:
    """Build a standard GitHub clone URL from `owner/name` form."""

    return f"https://github.com/{repository}.git"


def ensure_repository_checkout(
    repository: str,
    repo_path: Path,
    workspace_root: Path,
    base_branch: str,
    repo_url: str | None = None,
) -> Path:
    """Clone the repo if missing, otherwise fetch and fast-forward the base branch."""

    ensure_repo_path_is_safe(repo_path, workspace_root)
    repo_path.parent.mkdir(parents=True, exist_ok=True)

    if not (repo_path / ".git").exists():
        run_command(["git", "clone", repo_url or guess_repo_url(repository), str(repo_path)])
    else:
        git(["fetch", "origin"], repo_path=repo_path)

    git(["checkout", base_branch], repo_path=repo_path)
    git(["pull", "--ff-only", "origin", base_branch], repo_path=repo_path)
    return repo_path


def create_branch_name(goal: str, task_id: str) -> str:
    """Return a readable, deterministic branch name for the task."""

    slug = slugify(goal, max_length=48, separator="-")
    return f"feature/{slug}-{task_id[:8]}"


def ensure_branch(repo_path: Path, branch_name: str, from_ref: str) -> None:
    """Create or reset to a clean branch reference from the chosen base ref."""

    existing_branches = git(["branch", "--list", branch_name], repo_path=repo_path, check=False)
    if existing_branches:
        git(["checkout", branch_name], repo_path=repo_path)
    else:
        git(["checkout", "-b", branch_name, from_ref], repo_path=repo_path)


def collect_repo_overview(repo_path: Path) -> dict[str, Any]:
    """Collect a compact repository overview for research and planning prompts."""

    file_list: list[str] = []
    for root, _, files in os.walk(repo_path):
        if ".git" in root.split(os.sep):
            continue
        for filename in files:
            relative_path = str(Path(root, filename).relative_to(repo_path))
            file_list.append(relative_path)

    file_list.sort()
    important_files = [
        path
        for path in file_list
        if path.lower()
        in {
            "readme.md",
            "pyproject.toml",
            "package.json",
            "docker-compose.yml",
            "docker-compose.yaml",
            ".github/workflows/ci.yml",
        }
    ]

    status_output = git(["status", "--short"], repo_path=repo_path, check=False)
    last_commit = git(["log", "-1", "--pretty=%H %s"], repo_path=repo_path, check=False)
    return {
        "file_count": len(file_list),
        "sample_files": file_list[:200],
        "important_files": important_files,
        "git_status": status_output.splitlines() if status_output else [],
        "last_commit": last_commit,
    }


def read_text_file(repo_path: Path, relative_path: str, max_bytes: int = 24_000) -> str:
    """Read a text file defensively and truncate very large content for prompts."""

    content = (repo_path / relative_path).read_text(encoding="utf-8", errors="ignore")
    return content[:max_bytes]


def current_diff(repo_path: Path, base_branch: str) -> dict[str, Any]:
    """Return diff text, changed files, and git status to support review and GitHub steps."""

    changed_files_text = git(["diff", "--name-only", base_branch], repo_path=repo_path, check=False)
    diff_text = git(["diff", base_branch], repo_path=repo_path, check=False)
    stat_text = git(["diff", "--stat", base_branch], repo_path=repo_path, check=False)
    return {
        "changed_files": [line for line in changed_files_text.splitlines() if line.strip()],
        "diff_text": diff_text,
        "diff_stat": stat_text,
    }


def write_report(report_dir: Path, filename: str, content: str | dict[str, Any]) -> Path:
    """Write worker output as a report artifact for later audit or UI display."""

    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / filename
    if isinstance(content, dict):
        report_path.write_text(json.dumps(content, indent=2, ensure_ascii=False), encoding="utf-8")
    else:
        report_path.write_text(content, encoding="utf-8")
    return report_path
