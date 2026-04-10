"""
Purpose: GitHub worker for staging changes, committing, pushing branches, and creating draft pull requests.
Input/Output: Receives a reviewed and tested working tree, then returns commit and PR metadata.
Important invariants: Branch-based flow is mandatory, the worker never merges to main, and all push/PR failures stay explicit.
How to debug: If PR creation fails, inspect git remote auth, the staged diff, and the GitHub API error in the report.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI

from services.shared.agentic_lab.config import get_settings
from services.shared.agentic_lab.github_client import GitHubApiError, GitHubClient
from services.shared.agentic_lab.logging_utils import TaskLoggerAdapter, configure_logging
from services.shared.agentic_lab.repo_tools import current_diff, git, run_command, write_report
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

    commit_message = f"{settings.git_commit_message_prefix}: {request.goal[:72]}"
    author_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": settings.git_author_name,
        "GIT_AUTHOR_EMAIL": settings.git_author_email,
        "GIT_COMMITTER_NAME": settings.git_author_name,
        "GIT_COMMITTER_EMAIL": settings.git_author_email,
    }

    try:
        run_command(["git", "add", "-A"], cwd=repo_path, env=author_env)
        run_command(["git", "commit", "-m", commit_message], cwd=repo_path, env=author_env)
        run_command(
            ["git", "push", "--set-upstream", "origin", request.branch_name or ""],
            cwd=repo_path,
            env=author_env,
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
        f"## Validation\n{validation.get('recommendation', 'Validation output unavailable.')}\n"
    )
