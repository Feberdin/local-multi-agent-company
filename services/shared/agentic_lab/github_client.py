"""
Purpose: Minimal GitHub REST client for pull requests, issue comments, and PR-check inspection.
Input/Output: Workers and orchestrator helpers use this client to create pull requests, post comments,
              and inspect GitHub CI state for one pull request head commit.
Important invariants:
  - No GitHub API call is attempted without a configured token.
  - Repository names must stay explicit `owner/name` pairs.
  - Check summaries are normalized into stable, operator-readable buckets before automation reacts.
How to debug: If PR creation or CI inspection fails, inspect the repository, branch refs, token scopes,
              returned status payload, and the normalized check summary from this module first.
"""

from __future__ import annotations

from typing import Any

import httpx

from services.shared.agentic_lab.config import Settings


class GitHubApiError(RuntimeError):
    """Raised when GitHub rejects a REST request."""


FAILED_CHECK_CONCLUSIONS = frozenset(
    {"failure", "timed_out", "cancelled", "action_required", "startup_failure", "stale"}
)
PENDING_CHECK_STATUSES = frozenset({"queued", "in_progress", "pending", "requested", "waiting"})
SUCCESSFUL_CHECK_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})


def summarize_commit_checks(
    *,
    head_sha: str,
    check_runs_payload: dict[str, Any],
    status_payload: dict[str, Any],
) -> dict[str, Any]:
    """
    Normalize GitHub's mixed Checks + Commit Status APIs into one automation-friendly summary.

    Why this exists:
    GitHub exposes newer check-runs and older commit-status contexts separately. The auto-fix loop
    needs one consistent view so it can decide whether a pull request is still pending, has clearly
    failed, or is already healthy.
    """

    failed_checks: list[dict[str, str]] = []
    pending_checks: list[dict[str, str]] = []
    successful_checks: list[dict[str, str]] = []
    seen_failure_keys: set[tuple[str, str]] = set()

    def _record(target: list[dict[str, str]], item: dict[str, str]) -> None:
        target.append(item)

    for raw_check in check_runs_payload.get("check_runs", []) or []:
        name = str(raw_check.get("name") or "GitHub Check").strip()
        status = str(raw_check.get("status") or "unknown").strip().lower()
        conclusion = str(raw_check.get("conclusion") or "").strip().lower()
        output = raw_check.get("output") or {}
        summary = str(output.get("title") or output.get("summary") or output.get("text") or "").strip()
        details_url = str(raw_check.get("details_url") or raw_check.get("html_url") or "").strip()
        item = {
            "name": name,
            "status": status,
            "conclusion": conclusion,
            "summary": summary,
            "details_url": details_url,
        }
        if status in PENDING_CHECK_STATUSES or (status and status != "completed"):
            _record(pending_checks, item)
            continue
        if conclusion in FAILED_CHECK_CONCLUSIONS:
            _record(failed_checks, item)
            seen_failure_keys.add((name, conclusion))
            continue
        if conclusion in SUCCESSFUL_CHECK_CONCLUSIONS:
            _record(successful_checks, item)

    for raw_status in status_payload.get("statuses", []) or []:
        name = str(raw_status.get("context") or "commit-status").strip()
        state = str(raw_status.get("state") or "unknown").strip().lower()
        summary = str(raw_status.get("description") or "").strip()
        details_url = str(raw_status.get("target_url") or "").strip()
        conclusion = {"error": "failure", "failure": "failure", "pending": "pending", "success": "success"}.get(
            state,
            state,
        )
        item = {
            "name": name,
            "status": "completed" if state != "pending" else "pending",
            "conclusion": conclusion,
            "summary": summary,
            "details_url": details_url,
        }
        if state == "pending":
            _record(pending_checks, item)
            continue
        if conclusion in FAILED_CHECK_CONCLUSIONS and (name, conclusion) not in seen_failure_keys:
            _record(failed_checks, item)
            seen_failure_keys.add((name, conclusion))
            continue
        if conclusion == "success":
            _record(successful_checks, item)

    overall_state = str(status_payload.get("state") or check_runs_payload.get("status") or "unknown").strip().lower()
    completed = not pending_checks and bool(failed_checks or successful_checks or overall_state in {"success", "failure", "error"})

    return {
        "head_sha": head_sha,
        "overall_state": overall_state,
        "completed": completed,
        "failed_checks": failed_checks,
        "pending_checks": pending_checks,
        "successful_checks": successful_checks,
    }


class GitHubClient:
    """Tiny GitHub REST client focused on the MVP workflows."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _headers(self) -> dict[str, str]:
        if not self.settings.github_token or "replace-me" in self.settings.github_token:
            raise GitHubApiError("GITHUB_TOKEN is missing or still set to the example value.")
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.settings.github_token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def create_pull_request(
        self,
        repository: str,
        title: str,
        body: str,
        head_branch: str,
        base_branch: str,
    ) -> dict[str, Any]:
        """Create a pull request and return the GitHub response payload."""

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{self.settings.github_api_url.rstrip('/')}/repos/{repository}/pulls",
                headers=self._headers(),
                json={
                    "title": title,
                    "body": body,
                    "head": head_branch,
                    "base": base_branch,
                    "draft": True,
                },
            )
            if response.status_code >= 400:
                raise GitHubApiError(f"GitHub PR creation failed: {response.status_code} {response.text}")
            return response.json()

    async def create_issue_comment(self, repository: str, issue_number: int, body: str) -> dict[str, Any]:
        """Create a status or summary comment on an issue or pull request."""

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{self.settings.github_api_url.rstrip('/')}/repos/{repository}/issues/{issue_number}/comments",
                headers=self._headers(),
                json={"body": body},
            )
            if response.status_code >= 400:
                raise GitHubApiError(
                    f"GitHub comment creation failed: {response.status_code} {response.text}"
                )
            return response.json()

    async def get_pull_request(self, repository: str, pull_number: int) -> dict[str, Any]:
        """Load one pull request so automation can inspect the current head SHA safely."""

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{self.settings.github_api_url.rstrip('/')}/repos/{repository}/pulls/{pull_number}",
                headers=self._headers(),
            )
            if response.status_code >= 400:
                raise GitHubApiError(
                    f"GitHub pull request fetch failed: {response.status_code} {response.text}"
                )
            return response.json()

    async def get_commit_check_overview(self, repository: str, ref: str) -> dict[str, Any]:
        """Return one normalized CI summary for the given commit ref or SHA."""

        async with httpx.AsyncClient(timeout=30) as client:
            check_runs_response = await client.get(
                f"{self.settings.github_api_url.rstrip('/')}/repos/{repository}/commits/{ref}/check-runs",
                headers=self._headers(),
            )
            if check_runs_response.status_code >= 400:
                raise GitHubApiError(
                    f"GitHub check-runs fetch failed: {check_runs_response.status_code} {check_runs_response.text}"
                )

            status_response = await client.get(
                f"{self.settings.github_api_url.rstrip('/')}/repos/{repository}/commits/{ref}/status",
                headers=self._headers(),
            )
            if status_response.status_code >= 400:
                raise GitHubApiError(
                    f"GitHub commit-status fetch failed: {status_response.status_code} {status_response.text}"
                )

        return summarize_commit_checks(
            head_sha=ref,
            check_runs_payload=check_runs_response.json(),
            status_payload=status_response.json(),
        )
