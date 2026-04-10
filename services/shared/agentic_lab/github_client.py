"""
Purpose: Minimal GitHub REST client for pull requests and issue comments.
Input/Output: The GitHub worker calls this client after git push succeeds to create and update pull requests.
Important invariants: No GitHub API call is attempted without a configured token, and repository names must be explicit `owner/name` pairs.
How to debug: If PR creation fails, inspect the repository, branch refs, token scopes, and returned status payload.
"""

from __future__ import annotations

from typing import Any

import httpx

from services.shared.agentic_lab.config import Settings


class GitHubApiError(RuntimeError):
    """Raised when GitHub rejects a REST request."""


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
