"""
Purpose: GitHub worker for staging changes, committing, pushing branches, and creating pull requests.
Input/Output: Receives a reviewed and tested working tree, then returns commit and PR metadata.
Important invariants:
  - Branch-based flow is mandatory, and the worker never merges to main directly.
  - Pull requests default to draft unless metadata explicitly marks them ready for autonomous promotion later.
  - All push/PR failures stay explicit and operator-readable.
How to debug: If PR creation fails, inspect git remote auth, the staged diff, and the GitHub API error in the report.
"""

from __future__ import annotations

import base64
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

    commit_created = False
    try:
        run_command(["git", "add", "-A"], cwd=repo_path, env=author_env)
        commit_created = _create_or_reuse_local_commit(
            repo_path,
            author_env,
            commit_message,
            commit_body,
        )
        run_command(
            ["git", "push", "--set-upstream", "origin", branch_name],
            cwd=repo_path,
            env=push_env,
        )
        commit_sha = git(["rev-parse", "HEAD"], repo_path=repo_path)
        publish_strategy = "git_push"
    except Exception as exc:  # noqa: BLE001
        if not _should_attempt_api_publish(exc):
            return WorkerResponse(
                worker="github",
                success=False,
                summary="Git push failed.",
                errors=[str(exc)],
            )
        try:
            commit_sha = await _publish_branch_via_github_api(
                request=request,
                repo_path=repo_path,
                branch_name=branch_name,
                commit_message=commit_message,
                commit_body=commit_body,
            )
            publish_strategy = "github_api_fallback"
            task_logger.warning(
                "Git push failed; published branch via GitHub API fallback instead."
            )
        except (GitHubApiError, CommandError) as fallback_exc:
            return WorkerResponse(
                worker="github",
                success=False,
                summary="Git push failed.",
                errors=[str(exc), f"GitHub API fallback failed: {fallback_exc}"],
            )

    pr_body = _build_pr_body(request, diff)

    pull_request_draft = bool(request.metadata.get("pull_request_draft", True))

    try:
        pr = await github_client.create_pull_request(
            repository=request.repository,
            title=request.goal[:90],
            body=pr_body,
            head_branch=request.branch_name or "",
            base_branch=request.base_branch,
            draft=pull_request_draft,
        )
    except GitHubApiError as exc:
        return WorkerResponse(
            worker="github",
            success=False,
            summary="GitHub pull request creation failed.",
            errors=[str(exc)],
        )

    report = {
        "commit_sha": commit_sha,
        "pull_request_url": pr["html_url"],
        "pull_request_draft": pull_request_draft,
        "diff_stat": diff["diff_stat"],
        "publish_strategy": publish_strategy,
        "local_commit_created": commit_created,
    }
    report_path = write_report(settings.task_report_dir(request.task_id), "github-report.json", report)
    task_logger.info("Created draft pull request %s", pr["html_url"])

    return WorkerResponse(
        worker="github",
        summary="Branch pushed and draft pull request created.",
        outputs={
            "commit_sha": commit_sha,
            "pull_request_url": pr["html_url"],
            "pull_request_number": pr["number"],
            "pull_request_draft": pull_request_draft,
            "publish_strategy": publish_strategy,
            "local_commit_created": commit_created,
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


def _create_or_reuse_local_commit(
    repo_path: Path,
    author_env: dict[str, str],
    commit_message: str,
    commit_body: str,
) -> bool:
    """Create one local commit or reuse the existing task commit on retry when nothing new changed."""

    try:
        run_command(
            ["git", "commit", "-m", commit_message, "-m", commit_body],
            cwd=repo_path,
            env=author_env,
        )
        return True
    except CommandError as exc:
        if not _is_nothing_to_commit_error(exc):
            raise
        return False


def _is_nothing_to_commit_error(error: CommandError) -> bool:
    """Detect Git's retry-safe 'nothing to commit' variants so the worker can reuse HEAD."""

    lowered = str(error).lower()
    return "nothing to commit" in lowered or "no changes added to commit" in lowered


def _should_attempt_api_publish(error: Exception) -> bool:
    """Return true when the git push failed in a way the GitHub API fallback can realistically recover."""

    lowered = str(error).lower()
    return any(
        signal in lowered
        for signal in (
            "terminal prompts disabled",
            "permission to ",
            "requested url returned error: 403",
            "authentication failed",
        )
    )


def _parse_diff_name_status(diff_output: str) -> list[tuple[str, str]]:
    """Normalize git name-status output into simple `(status, path)` entries, including rename deletes/adds."""

    entries: list[tuple[str, str]] = []
    for raw_line in diff_output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = [part for part in line.split("\t") if part]
        if len(parts) < 2:
            continue
        status = parts[0].strip()
        if status.startswith("R") and len(parts) >= 3:
            entries.append(("D", parts[1].strip()))
            entries.append(("A", parts[2].strip()))
            continue
        entries.append((status[:1], parts[-1].strip()))
    return entries


def _local_tree_element_for_path(repo_path: Path, relative_path: str) -> dict[str, str]:
    """Read one file from the task checkout and convert it into a GitHub tree element."""

    ls_tree_line = git(["ls-tree", "HEAD", "--", relative_path], repo_path=repo_path, check=False).strip()
    mode = "100644"
    blob_type = "blob"
    if ls_tree_line:
        metadata, _, _path = ls_tree_line.partition("\t")
        metadata_parts = metadata.split()
        if len(metadata_parts) >= 2:
            mode = metadata_parts[0]
            blob_type = metadata_parts[1]
    file_bytes = (repo_path / relative_path).read_bytes()
    encoded_content = base64.b64encode(file_bytes).decode("ascii")
    return {
        "path": relative_path,
        "mode": mode,
        "type": blob_type,
        "content_base64": encoded_content,
    }


async def _publish_branch_via_github_api(
    *,
    request: WorkerRequest,
    repo_path: Path,
    branch_name: str,
    commit_message: str,
    commit_body: str,
) -> str:
    """Publish the current local diff through the GitHub Git Data API when plain `git push` is forbidden."""

    base_ref = await github_client.get_git_ref(request.repository, f"heads/{request.base_branch}")
    base_sha = str((base_ref.get("object") or {}).get("sha") or "").strip()
    if not base_sha:
        raise GitHubApiError(
            f"GitHub lieferte keinen gueltigen SHA fuer `{request.base_branch}` zurueck."
        )
    base_commit = await github_client.get_git_commit(request.repository, base_sha)
    base_tree_sha = str((base_commit.get("tree") or {}).get("sha") or "").strip()
    if not base_tree_sha:
        raise GitHubApiError(
            f"GitHub lieferte keinen gueltigen Tree-SHA fuer `{request.base_branch}` zurueck."
        )

    diff_name_status = git(
        ["diff", "--name-status", "--find-renames", f"{request.base_branch}...HEAD"],
        repo_path=repo_path,
        check=False,
    )
    parsed_entries = _parse_diff_name_status(diff_name_status)
    if not parsed_entries:
        raise CommandError(
            "GitHub API fallback konnte keine geaenderten Dateien aus dem lokalen Branch ableiten."
        )

    tree: list[dict[str, str | None]] = []
    for status, relative_path in parsed_entries:
        if status == "D":
            tree.append(
                {
                    "path": relative_path,
                    "mode": "100644",
                    "type": "blob",
                    "sha": None,
                }
            )
            continue
        local_entry = _local_tree_element_for_path(repo_path, relative_path)
        blob = await github_client.create_git_blob(
            request.repository,
            local_entry["content_base64"],
        )
        tree.append(
            {
                "path": local_entry["path"],
                "mode": local_entry["mode"],
                "type": local_entry["type"],
                "sha": str(blob.get("sha") or "").strip(),
            }
        )

    created_tree = await github_client.create_git_tree(
        request.repository,
        tree=tree,
        base_tree_sha=base_tree_sha,
    )
    created_tree_sha = str(created_tree.get("sha") or "").strip()
    if not created_tree_sha:
        raise GitHubApiError("GitHub lieferte keinen gueltigen Tree-SHA fuer den Fallback-Commit.")

    combined_message = f"{commit_message}\n\n{commit_body}"
    created_commit = await github_client.create_git_commit(
        request.repository,
        message=combined_message,
        tree_sha=created_tree_sha,
        parent_shas=[base_sha],
    )
    commit_sha = str(created_commit.get("sha") or "").strip()
    if not commit_sha:
        raise GitHubApiError("GitHub lieferte keinen gueltigen Commit-SHA fuer den Fallback-Branch.")

    ref_name = f"heads/{branch_name}"
    try:
        await github_client.create_git_ref(request.repository, ref_name, commit_sha)
    except GitHubApiError:
        await github_client.update_git_ref(
            request.repository,
            ref_name,
            commit_sha,
            force=True,
        )
    return commit_sha


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
