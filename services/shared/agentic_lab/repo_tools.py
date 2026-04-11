"""
Purpose: Safe repository, Git, and subprocess helpers for repo-based workers in local/self-hosted deployments.
Input/Output: Workers use these helpers to prepare isolated task workspaces, run Git commands, inspect repos, and write reports.
Important invariants: Commands run without a shell, Git always gets a writable HOME plus safe.directory,
and task work never reuses a shared dirty checkout.
How to debug: If repo access fails, inspect the resolved task workspace path, HOME/GIT_CONFIG_GLOBAL,
and the captured stderr from the failing Git command.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from slugify import slugify

from services.shared.agentic_lab.config import get_settings
from services.shared.agentic_lab.guardrails import ensure_repo_path_is_safe


class CommandError(RuntimeError):
    """Raised when a subprocess fails and the caller requested strict checking."""


def _invoke_subprocess(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    """Run one subprocess call without command rewriting so Git bootstrap can call it recursively."""

    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _merge_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """Start from the process environment and apply explicit overrides from the caller."""

    merged = dict(os.environ)
    if env:
        merged.update(env)
    return merged


def _git_runtime_home(env: dict[str, str] | None = None) -> Path:
    """Return the writable HOME directory Git should use inside arbitrary PUID/PGID containers."""

    raw_home = (env or {}).get("RUNTIME_HOME_DIR") or str(get_settings().runtime_home_dir)
    return Path(raw_home).expanduser()


def _ensure_git_runtime_files(env: dict[str, str] | None = None) -> dict[str, str]:
    """Guarantee that Git has a writable HOME and global config file even for arbitrary container users."""

    merged = _merge_env(env)
    home_dir = _git_runtime_home(merged)
    try:
        home_dir.mkdir(parents=True, exist_ok=True)
        (home_dir / ".config").mkdir(parents=True, exist_ok=True)
        gitconfig_path = home_dir / ".gitconfig"
        gitconfig_path.touch(exist_ok=True)
    except OSError as exc:
        raise CommandError(
            f"Git konnte kein beschreibbares HOME unter `{home_dir}` vorbereiten: {exc}. "
            "Setze RUNTIME_HOME_DIR auf einen beschreibbaren Pfad innerhalb des Containers."
        ) from exc

    merged["HOME"] = str(home_dir)
    merged.setdefault("XDG_CONFIG_HOME", str(home_dir / ".config"))
    merged["GIT_CONFIG_GLOBAL"] = str(home_dir / ".gitconfig")
    merged.setdefault("GIT_TERMINAL_PROMPT", "0")
    return merged


def _register_safe_directory(repo_path: Path, env: dict[str, str]) -> None:
    """Idempotently allow a mounted repo path in Git's safe.directory list."""

    if not repo_path.exists():
        return

    resolved_repo = repo_path.resolve()
    existing = _invoke_subprocess(
        ["git", "config", "--global", "--get-all", "safe.directory"],
        env=env,
        timeout=60,
    )
    known_entries = {line.strip() for line in existing.stdout.splitlines() if line.strip()}
    if "*" in known_entries or str(resolved_repo) in known_entries:
        return

    added = _invoke_subprocess(
        ["git", "config", "--global", "--add", "safe.directory", str(resolved_repo)],
        env=env,
        timeout=60,
    )
    if added.returncode != 0:
        stderr = added.stderr.strip() or added.stdout.strip() or "unbekannter Git-Fehler"
        raise CommandError(
            f"Git konnte `safe.directory` fuer `{resolved_repo}` nicht setzen: {stderr}. "
            "Pruefe, ob HOME und GIT_CONFIG_GLOBAL beschreibbar sind."
        )


def _git_related_safe_paths(repo_path: Path) -> list[Path]:
    """Return the repository paths Git may complain about for mounted local clones."""

    candidates = [repo_path]
    git_dir = repo_path / ".git"
    if git_dir.exists():
        candidates.append(git_dir)
    return candidates


def prepare_git_environment(repo_path: Path | None = None, env: dict[str, str] | None = None) -> dict[str, str]:
    """Return a Git-ready environment with writable HOME and safe.directory for mounted workspaces."""

    prepared = _ensure_git_runtime_files(env)
    if repo_path is not None:
        for safe_path in _git_related_safe_paths(repo_path):
            _register_safe_directory(safe_path, prepared)
    return prepared


def _git_clone_source_path(args: list[str]) -> Path | None:
    """Best-effort detection of a local source repo path for `git clone` commands."""

    if len(args) < 4 or args[:2] != ["git", "clone"]:
        return None

    positional_args = [arg for arg in args[2:] if not arg.startswith("-")]
    if len(positional_args) < 2:
        return None

    source_candidate = Path(positional_args[-2])
    if source_candidate.exists():
        return source_candidate
    return None


def _format_git_hint(args: list[str], stderr: str, repo_path: Path | None, env: dict[str, str] | None) -> str | None:
    """Translate common Git runtime failures into operator-friendly hints for the UI and logs."""

    lowered = stderr.lower()
    source_repo_path = _git_clone_source_path(args)
    repo_display = str(repo_path or source_repo_path) if (repo_path or source_repo_path) else "unbekanntes Repository"
    home_display = str(_git_runtime_home(env))

    if "dubious ownership" in lowered or "safe.directory" in lowered:
        return (
            f"Git vertraut dem gemounteten Repository `{repo_display}` noch nicht. "
            "Normalerweise setzt der Worker `safe.directory` automatisch. "
            f"Pruefe sonst HOME=`{home_display}` und die Rechte des gemounteten Repositories."
        )
    if "could not lock config file" in lowered and ".gitconfig" in lowered:
        return (
            f"Git konnte die globale Konfiguration nicht schreiben. "
            f"Das HOME-Verzeichnis `{home_display}` ist vermutlich nicht beschreibbar."
        )
    if "not a git repository" in lowered:
        return (
            f"Unter `{repo_display}` liegt noch kein verwendbarer Git-Checkout. "
            "Pruefe den lokalen Workspace-Pfad oder lasse die Aufgabe einen isolierten Task-Workspace neu anlegen."
        )
    if "would be overwritten" in lowered or "local changes" in lowered:
        return (
            f"Der Task-Workspace `{repo_display}` enthaelt bereits lokale Aenderungen. "
            "Die Worker arbeiten deshalb bewusst nicht auf einem geteilten oder verschmutzten Checkout."
        )
    if args[:2] == ["git", "fetch"]:
        return (
            "Git konnte den Remote-Stand nicht aktualisieren. "
            "Falls das Repo bereits isoliert vorbereitet wurde, kann der Task meist trotzdem mit dem vorhandenen Checkout weiterlaufen."
        )
    return None


def run_command(
    args: list[str],
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 300,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a command defensively without invoking a shell."""

    effective_env: dict[str, str] | None = None
    if args and args[0] == "git":
        effective_env = prepare_git_environment(cwd, env)
    elif cwd is not None and (cwd / ".git").exists():
        effective_env = prepare_git_environment(cwd, env)
    elif env is not None:
        effective_env = _merge_env(env)

    completed = _invoke_subprocess(args, cwd=cwd, env=effective_env, timeout=timeout)
    if check and completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip() or "keine weitere Ausgabe"
        hint = _format_git_hint(args, stderr, cwd, effective_env) if args and args[0] == "git" else None
        message = f"Command {' '.join(args)} failed with code {completed.returncode}: {stderr}"
        if hint:
            message += f"\nHint: {hint}"
        raise CommandError(message)
    return completed


def git(args: list[str], repo_path: Path, timeout: int = 300, check: bool = True) -> str:
    """Run a git command inside a repository and return stdout."""

    completed = run_command(["git", *args], cwd=repo_path, timeout=timeout, check=check)
    return completed.stdout.strip()


def guess_repo_url(repository: str) -> str:
    """Build a standard GitHub clone URL from `owner/name` form."""

    return f"https://github.com/{repository}.git"


def build_task_workspace_path(
    task_id: str,
    repository: str,
    workspace_root: Path,
    task_workspace_root: Path | None = None,
) -> Path:
    """Return the isolated workspace path for one task so tasks never reuse the shared mounted checkout."""

    repo_slug = slugify(repository.split("/")[-1] or repository, separator="-") or "repository"
    workspace_base = task_workspace_root or (workspace_root / ".task-workspaces")
    return workspace_base / task_id / repo_slug


def _discover_origin_url(source_repo_path: Path, fallback_url: str) -> str:
    """Read the source repo's configured origin URL when cloning from a local mounted checkout."""

    origin_url = git(["config", "--get", "remote.origin.url"], repo_path=source_repo_path, check=False)
    return origin_url or fallback_url


def _clone_target_from_best_source(
    *,
    repository: str,
    repo_path: Path,
    source_repo_path: Path | None,
    base_branch: str,
    repo_url: str | None,
) -> None:
    """Create one clean task-local checkout, preferring a local source repo but preserving the real origin URL."""

    fallback_url = repo_url or guess_repo_url(repository)
    clone_source = fallback_url
    canonical_origin_url = fallback_url

    if source_repo_path and (source_repo_path / ".git").exists():
        clone_source = str(source_repo_path)
        canonical_origin_url = _discover_origin_url(source_repo_path, fallback_url)
        clone_env = prepare_git_environment(source_repo_path)
    else:
        clone_env = None

    run_command(
        ["git", "clone", "--branch", base_branch, "--single-branch", clone_source, str(repo_path)],
        env=clone_env,
        timeout=900,
    )
    if canonical_origin_url and canonical_origin_url != clone_source:
        git(["remote", "set-url", "origin", canonical_origin_url], repo_path=repo_path)


def ensure_repository_checkout(
    repository: str,
    repo_path: Path,
    workspace_root: Path,
    base_branch: str,
    repo_url: str | None = None,
    *,
    task_id: str | None = None,
    source_repo_path: Path | None = None,
) -> Path:
    """Prepare or reuse one isolated repo checkout for a task without mutating the shared mounted workspace."""

    settings = get_settings()
    isolated_repo_path = repo_path
    isolated_source_path = source_repo_path

    if task_id:
        desired_repo_path = build_task_workspace_path(
            task_id,
            repository,
            workspace_root,
            settings.effective_task_workspace_root,
        )
        if repo_path.resolve() != desired_repo_path.resolve():
            isolated_source_path = source_repo_path or repo_path
            isolated_repo_path = desired_repo_path

    ensure_repo_path_is_safe(isolated_repo_path, workspace_root)
    if isolated_source_path is not None:
        ensure_repo_path_is_safe(isolated_source_path, workspace_root)

    isolated_repo_path.parent.mkdir(parents=True, exist_ok=True)

    if not (isolated_repo_path / ".git").exists():
        _clone_target_from_best_source(
            repository=repository,
            repo_path=isolated_repo_path,
            source_repo_path=isolated_source_path,
            base_branch=base_branch,
            repo_url=repo_url,
        )

    current_branch = git(["branch", "--show-current"], repo_path=isolated_repo_path, check=False)
    dirty_status = git(["status", "--short"], repo_path=isolated_repo_path, check=False)
    is_dirty = bool(dirty_status.strip())

    if current_branch in {"", base_branch} and not is_dirty:
        git(["fetch", "origin"], repo_path=isolated_repo_path, check=False)
        remote_base = git(["rev-parse", "--verify", f"origin/{base_branch}"], repo_path=isolated_repo_path, check=False)
        if remote_base:
            git(["checkout", base_branch], repo_path=isolated_repo_path, check=False)
            git(["pull", "--ff-only", "origin", base_branch], repo_path=isolated_repo_path, check=False)

    return isolated_repo_path


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
