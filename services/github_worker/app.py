"""
Purpose: GitHub worker for staging changes, committing, pushing branches, and creating draft pull requests.
Input/Output: Receives a reviewed and tested working tree, then returns commit and PR metadata.
Important invariants: Branch-based flow is mandatory, the worker never merges to main, and all push/PR failures stay explicit.
How to debug: If PR creation fails, inspect git remote auth, the staged diff, and the GitHub API error in the report.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import FastAPI

from services.shared.agentic_lab.config import get_settings
from services.shared.agentic_lab.github_client import GitHubApiError, GitHubClient
from services.shared.agentic_lab.logging_utils import TaskLoggerAdapter, configure_logging
from services.shared.agentic_lab.repo_tools import CommandError, current_diff, git, run_command, write_report
from services.shared.agentic_lab.schemas import Artifact, HealthResponse, WorkerRequest, WorkerResponse

settings = get_settings()
logger = configure_logging(settings.service_name, settings.log_level)
github_client = GitHubClient(settings)
app = FastAPI(title="Feberdin GitHub Worker", version="0.1.0")


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(service="github-worker")


@app.post("/run", response_model=WorkerResponse)
async def run(request: WorkerRequest) -> WorkerResponse:
    task_logger = TaskLoggerAdapter(logger.logger, {"service": "github-worker", "task_id": request.task_id})
    repo_path = Path(request.local_repo_path)
    diff = current_diff(repo_path, request.base_branch)
    if not diff["changed_files"]:
        return WorkerResponse(
            worker="github",
            success=False,
            summary="No changes found to commit.",
            errors=["The working tree is empty relative to the base branch."],
        )

    branch_name = (request.branch_name or "").strip()
    if not branch_name:
        return WorkerResponse(
            worker="github",
            success=False,
            summary="GitHub push preflight failed.",
            errors=[
                "Branch-Name fehlt. Der GitHub-Worker braucht einen Feature-Branch, bevor er committen und pushen kann."
            ],
        )

    commit_message = f"{settings.git_commit_message_prefix}: {request.goal[:72]}"
    worker_project_label = request.metadata.get("worker_project_label", "Feberdin local-multi-agent-company worker project")
    commit_body = (
        f"Created by {worker_project_label} after explicit repository modification approval."
    )
    author_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": settings.git_author_name,
        "GIT_AUTHOR_EMAIL": settings.git_author_email,
        "GIT_COMMITTER_NAME": settings.git_author_name,
        "GIT_COMMITTER_EMAIL": settings.git_author_email,
    }
    try:
        push_env = _build_authenticated_push_env(repo_path, author_env)
    except CommandError as exc:
        return WorkerResponse(
            worker="github",
            success=False,
            summary="GitHub push preflight failed.",
            errors=[str(exc)],
        )

    try:
        run_command(["git", "add", "-A"], cwd=repo_path, env=author_env)
        run_command(["git", "commit", "-m", commit_message, "-m", commit_body], cwd=repo_path, env=author_env)
        run_command(
            ["git", "push", "--set-upstream", "origin", branch_name],
            cwd=repo_path,
            env=push_env,
        )
    except Exception as exc:  # noqa: BLE001
        return WorkerResponse(
            worker="github",
            success=False,
            summary="Git push failed.",
            errors=[str(exc)],
        )

    commit_sha = git(["rev-parse", "HEAD"], repo_path=repo_path)
    pr_body = _build_pr_body(request, diff)

    try:
        pr = await github_client.create_pull_request(
            repository=request.repository,
            title=request.goal[:90],
            body=pr_body,
            head_branch=request.branch_name or "",
            base_branch=request.base_branch,
        )
    except GitHubApiError as exc:
        return WorkerResponse(
            worker="github",
            success=False,
            summary="GitHub pull request creation failed.",
            errors=[str(exc)],
        )

    report = {"commit_sha": commit_sha, "pull_request_url": pr["html_url"], "diff_stat": diff["diff_stat"]}
    report_path = write_report(settings.task_report_dir(request.task_id), "github-report.json", report)
    task_logger.info("Created draft pull request %s", pr["html_url"])

    return WorkerResponse(
        worker="github",
        summary="Branch pushed and draft pull request created.",
        outputs={
            "commit_sha": commit_sha,
            "pull_request_url": pr["html_url"],
            "pull_request_number": pr["number"],
        },
        artifacts=[
            Artifact(
                name="github-report",
                path=str(report_path),
                description="Commit SHA and draft pull request metadata.",
            )
        ],
    )


def _build_pr_body(request: WorkerRequest, diff: dict) -> str:
    worker_project_label = request.metadata.get("worker_project_label", "Feberdin local-multi-agent-company worker project")
    requirements = request.prior_results.get("requirements", {}).get("outputs", {})
    architecture = request.prior_results.get("architecture", {}).get("outputs", {})
    review = request.prior_results.get("reviewer", {}).get("outputs", {})
    tests = request.prior_results.get("tester", {}).get("outputs", {})
    validation = request.prior_results.get("validation", {}).get("outputs", {})
    security = request.prior_results.get("security", {}).get("outputs", {})
    return (
        f"## Goal\n{request.goal}\n\n"
        f"## Requirements Summary\n{requirements.get('summary', 'No requirements summary available.')}\n\n"
        f"## Architecture Summary\n{architecture.get('summary', 'No architecture summary available.')}\n\n"
        f"## Changed Files\n{', '.join(diff['changed_files'])}\n\n"
        f"## Review Notes\n{review.get('findings', [])}\n\n"
        f"## Test Results\n{tests.get('errors', []) or 'No test errors reported.'}\n\n"
        f"## Security\n{security.get('risk_flags', []) or 'No extra security flags reported.'}\n\n"
        f"## Validation\n{validation.get('recommendation', 'Validation output unavailable.')}\n\n"
        f"## Provenance\n"
        f"These changes were created by the `{worker_project_label}` and should only exist after "
        f"explicit repository modification approval.\n"
    )


def _has_usable_github_token(raw_token: str) -> bool:
    """Treat empty and example placeholder values as unusable GitHub credentials."""

    token = raw_token.strip()
    return bool(token) and "replace-me" not in token.lower()


def _origin_parts_for_https_push(origin_url: str) -> tuple[str, str, str] | None:
    """Normalize HTTPS and SSH Git remotes into `(scheme, host, path)` for token-based push URLs."""

    normalized = origin_url.strip()
    if not normalized:
        return None

    if normalized.startswith(("https://", "http://")):
        parsed = urlparse(normalized)
        if not parsed.hostname or not parsed.path:
            return None
        host = parsed.hostname
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return parsed.scheme, host, parsed.path

    if normalized.startswith("ssh://"):
        parsed = urlparse(normalized)
        if not parsed.hostname or not parsed.path:
            return None
        host = parsed.hostname
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return "https", host, parsed.path

    if "@" in normalized and ":" in normalized:
        user_host, remote_path = normalized.split(":", 1)
        if "@" not in user_host:
            return None
        _, host = user_host.split("@", 1)
        remote_path = remote_path.strip()
        if not host or not remote_path:
            return None
        return "https", host, f"/{remote_path.lstrip('/')}"

    return None


def _display_origin_url(origin_url: str) -> str:
    """Return a sanitized, operator-friendly remote URL without embedded credentials."""

    normalized = _origin_parts_for_https_push(origin_url)
    if normalized is None:
        return origin_url.strip() or "unbekannte Origin-URL"
    scheme, host, path = normalized
    return f"{scheme}://{host}{path}"


def _build_authenticated_push_url(origin_url: str, github_token: str) -> str | None:
    """Create one token-authenticated HTTPS push URL without changing the fetch remote."""

    origin_parts = _origin_parts_for_https_push(origin_url)
    if origin_parts is None:
        return None
    scheme, host, path = origin_parts
    encoded_token = quote(github_token.strip(), safe="")
    return f"{scheme}://x-access-token:{encoded_token}@{host}{path}"


def _add_ephemeral_git_config(env: dict[str, str], key: str, value: str) -> dict[str, str]:
    """Inject one temporary Git config entry via environment variables for a single command call."""

    merged = dict(env)
    raw_count = merged.get("GIT_CONFIG_COUNT", "0")
    try:
        config_count = int(raw_count)
    except ValueError:
        config_count = 0
    merged["GIT_CONFIG_COUNT"] = str(config_count + 1)
    merged[f"GIT_CONFIG_KEY_{config_count}"] = key
    merged[f"GIT_CONFIG_VALUE_{config_count}"] = value
    return merged


def _build_authenticated_push_env(repo_path: Path, base_env: dict[str, str]) -> dict[str, str]:
    """Prepare one push-only Git environment that authenticates with the configured GitHub token."""

    if not _has_usable_github_token(settings.github_token):
        raise CommandError(
            "GITHUB_TOKEN fehlt oder ist noch auf dem Beispielwert. "
            "Der GitHub-Worker braucht denselben Token fuer `git push` und fuer das Erstellen der Pull Request."
        )

    origin_url = git(["config", "--get", "remote.origin.url"], repo_path=repo_path, check=False)
    if not origin_url:
        raise CommandError(
            "Git-Remote `origin` ist nicht gesetzt. "
            "Pruefe den isolierten Task-Workspace oder die Ausgangs-Repository-Konfiguration."
        )

    authenticated_push_url = _build_authenticated_push_url(origin_url, settings.github_token)
    if not authenticated_push_url:
        raise CommandError(
            "Die Origin-URL "
            f"`{_display_origin_url(origin_url)}` kann nicht fuer einen tokenbasierten GitHub-Push verwendet werden. "
            "Unterstuetzt werden HTTPS- und SSH-Remotes mit einem klaren Host/Repository-Pfad."
        )

    return _add_ephemeral_git_config(base_env, "remote.origin.pushurl", authenticated_push_url)
