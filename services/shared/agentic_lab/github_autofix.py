"""
Purpose: Watch GitHub pull request checks and automatically launch one focused follow-up fix task on failure.
Input/Output: Scans recent tasks with draft PRs, inspects GitHub CI state, and creates a new repository task when
              failed checks provide a concrete next bug to fix.
Important invariants:
  - Only one automatic follow-up task is launched per failed-check signature.
  - The same failing signature is not re-triggered endlessly.
  - All operator-visible state lives in normal task metadata so the UI can explain what happened.
How to debug: Inspect task.metadata keys beginning with `github_autofix_`, then compare them with the GitHub check
              summary returned by `GitHubClient.get_commit_check_overview()`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from services.shared.agentic_lab.config import Settings
from services.shared.agentic_lab.github_client import GitHubApiError, GitHubClient
from services.shared.agentic_lab.llm import LLMClient, LLMError
from services.shared.agentic_lab.schemas import TaskCreateRequest, TaskDetail, TaskStatus
from services.shared.agentic_lab.task_service import TaskService

logger = logging.getLogger(__name__)

_MONITORABLE_TASK_STATUSES = {
    TaskStatus.PR_CREATED,
    TaskStatus.STAGING_DEPLOYED,
    TaskStatus.DONE,
}
_FIX_MONITOR_POLL_SECONDS = 30.0
_FIX_MONITOR_TIMEOUT_SECONDS = 2 * 3600.0
TaskRunner = Callable[[str], Awaitable[None]]


class GitHubAutofixService:
    """Glue GitHub CI failures to the existing task workflow instead of leaving operators with red PR checks."""

    def __init__(
        self,
        task_service: TaskService,
        github_client: GitHubClient,
        llm: LLMClient,
        settings: Settings,
    ) -> None:
        self.task_service = task_service
        self.github_client = github_client
        self.llm = llm
        self.settings = settings

    async def scan_once(self, run_task_fn: TaskRunner) -> int:
        """
        Inspect the newest PR-bearing tasks once and trigger follow-up fixes where needed.

        Why this exists:
        GitHub check failures often arrive minutes after the original task has already finished. A lightweight
        periodic scan is simpler and more restart-safe than keeping many long-lived per-task monitor coroutines.
        """

        if not self.settings.github_auto_fix_enabled or not self.settings.github_token:
            return 0

        started = 0
        tasks = self.task_service.list_tasks(include_archived=False)
        for task in tasks[:25]:
            if await self.inspect_task(task.id, run_task_fn):
                started += 1
        return started

    async def inspect_task(self, task_id: str, run_task_fn: TaskRunner) -> bool:
        """Inspect one task's PR checks once and start a focused follow-up task when GitHub reports a real failure."""

        if not self.settings.github_auto_fix_enabled or not self.settings.github_token:
            return False

        try:
            task = self.task_service.get_task(task_id)
        except KeyError:
            return False

        if not self._is_monitorable_task(task):
            return False

        pull_number = self._pull_request_number(task)
        if pull_number is None:
            self.task_service.update_runtime_context(
                task.id,
                metadata_updates={
                    "github_autofix_status": "monitor_error",
                    "github_autofix_last_error": "Pull-Request-Nummer konnte aus der gespeicherten URL nicht gelesen werden.",
                    "github_autofix_last_checked_at": datetime.now(UTC).isoformat(),
                },
            )
            return False

        try:
            pull_request = await self.github_client.get_pull_request(task.repository, pull_number)
            head_sha = str((pull_request.get("head") or {}).get("sha") or "").strip()
            if not head_sha:
                raise GitHubApiError("GitHub pull request payload enthielt keinen head SHA.")
            check_summary = await self.github_client.get_commit_check_overview(task.repository, head_sha)
        except GitHubApiError as exc:
            logger.warning("github-autofix: task %s could not inspect PR checks: %s", task.id, exc)
            self.task_service.update_runtime_context(
                task.id,
                metadata_updates={
                    "github_autofix_status": "monitor_error",
                    "github_autofix_last_error": str(exc)[:500],
                    "github_autofix_last_checked_at": datetime.now(UTC).isoformat(),
                },
            )
            return False

        return await self._apply_check_summary(task, pull_number, head_sha, check_summary, run_task_fn)

    def _is_monitorable_task(self, task: TaskDetail) -> bool:
        """Reject tasks that should never trigger automatic GitHub-based follow-up work."""

        if task.archived or task.approval_required or not task.pull_request_url:
            return False
        if task.status not in _MONITORABLE_TASK_STATUSES:
            return False
        return True

    def _pull_request_number(self, task: TaskDetail) -> int | None:
        """Read the PR number from metadata first, then fall back to parsing the stored GitHub URL."""

        metadata = dict(task.metadata or {})
        raw_number = metadata.get("pull_request_number")
        if isinstance(raw_number, int):
            return raw_number
        if isinstance(raw_number, str) and raw_number.strip().isdigit():
            return int(raw_number.strip())

        if not task.pull_request_url:
            return None
        parsed = urlparse(task.pull_request_url)
        segments = [segment for segment in parsed.path.split("/") if segment]
        if len(segments) >= 4 and segments[-2] == "pull" and segments[-1].isdigit():
            return int(segments[-1])
        return None

    async def _apply_check_summary(
        self,
        task: TaskDetail,
        pull_number: int,
        head_sha: str,
        check_summary: dict[str, Any],
        run_task_fn: TaskRunner,
    ) -> bool:
        """Update operator-visible metadata and optionally create one targeted follow-up fix task."""

        failed_checks = [item for item in check_summary.get("failed_checks", []) if isinstance(item, dict)]
        pending_checks = [item for item in check_summary.get("pending_checks", []) if isinstance(item, dict)]
        metadata = dict(task.metadata or {})
        signature = self._failure_signature(head_sha, failed_checks)
        common_updates = {
            "github_autofix_last_checked_at": datetime.now(UTC).isoformat(),
            "github_autofix_pull_request_number": pull_number,
            "github_autofix_head_sha": head_sha,
            "github_autofix_last_overall_state": str(check_summary.get("overall_state") or "unknown"),
            "github_autofix_failed_checks": failed_checks,
            "github_autofix_pending_checks": pending_checks,
            "github_autofix_max_attempts": self.settings.github_auto_fix_max_attempts,
        }

        if failed_checks:
            if metadata.get("github_autofix_status") == "fix_in_progress" and metadata.get("github_autofix_fix_task_id"):
                return False
            if signature and signature == str(metadata.get("github_autofix_last_signature") or ""):
                return False

            attempt = int(metadata.get("github_autofix_attempt", 0))
            if attempt >= self.settings.github_auto_fix_max_attempts:
                self.task_service.update_runtime_context(
                    task.id,
                    metadata_updates={
                        **common_updates,
                        "github_autofix_status": "max_attempts_reached",
                        "github_autofix_last_signature": signature,
                        "github_autofix_error_summary": self._format_failed_checks(failed_checks),
                    },
                )
                return False

            failure_summary = self._format_failed_checks(failed_checks)
            try:
                fix_goal = await self._generate_fix_goal(task, failure_summary)
            except LLMError as exc:
                logger.warning("github-autofix: LLM analysis failed for task %s: %s", task.id, exc)
                fix_goal = self._fallback_fix_goal(task.goal, failure_summary)

            root_task_id = str(metadata.get("github_autofix_root_task_id") or task.id)
            self.task_service.update_runtime_context(
                task.id,
                metadata_updates={
                    **common_updates,
                    "github_autofix_status": "fix_in_progress",
                    "github_autofix_attempt": attempt + 1,
                    "github_autofix_root_task_id": root_task_id,
                    "github_autofix_last_signature": signature,
                    "github_autofix_error_summary": failure_summary,
                    "github_autofix_fix_goal": fix_goal,
                    "github_autofix_started_at": datetime.now(UTC).isoformat(),
                },
            )

            fix_summary = self.task_service.create_task(
                TaskCreateRequest(
                    goal=fix_goal,
                    repository=task.repository,
                    repo_url=task.repo_url,
                    local_repo_path=str(metadata.get("source_local_repo_path") or task.local_repo_path),
                    base_branch=task.base_branch,
                    issue_number=task.metadata.get("issue_number"),
                    enable_web_research=bool(task.metadata.get("enable_web_research", False)),
                    allow_repository_modifications=True,
                    auto_deploy_staging=bool(task.metadata.get("auto_deploy_staging", True)),
                    test_commands=list(task.metadata.get("test_commands", [])),
                    lint_commands=list(task.metadata.get("lint_commands", [])),
                    typing_commands=list(task.metadata.get("typing_commands", [])),
                    smoke_checks=list(task.smoke_checks),
                    deployment=task.deployment,
                    metadata={
                        "github_autofix_parent_task_id": task.id,
                        "github_autofix_root_task_id": root_task_id,
                        "github_autofix_attempt": attempt + 1,
                        "github_autofix_failed_checks": failed_checks,
                        "github_autofix_error_summary": failure_summary,
                        "github_autofix_pull_request_url": task.pull_request_url,
                        "github_autofix_pull_request_number": pull_number,
                        "github_autofix_head_sha": head_sha,
                        "github_autofix_source_branch_name": task.branch_name,
                        "deployment_target": "github_ci_autofix",
                        "worker_project_label": "GitHub-CI Auto-Fix",
                        "allow_repository_modifications": True,
                    },
                )
            )
            self.task_service.update_runtime_context(
                task.id,
                metadata_updates={"github_autofix_fix_task_id": fix_summary.id},
            )
            logger.info(
                "github-autofix: created fix task %s for parent %s after failed checks on PR #%s.",
                fix_summary.id,
                task.id,
                pull_number,
            )
            await run_task_fn(fix_summary.id)
            asyncio.create_task(self._monitor_fix(fix_summary.id, task.id))
            return True

        if pending_checks or not bool(check_summary.get("completed")):
            self.task_service.update_runtime_context(
                task.id,
                metadata_updates={
                    **common_updates,
                    "github_autofix_status": "monitoring",
                },
            )
            return False

        if metadata.get("github_autofix_status") != "checks_passed":
            self.task_service.update_runtime_context(
                task.id,
                metadata_updates={
                    **common_updates,
                    "github_autofix_status": "checks_passed",
                },
            )
        return False

    async def _generate_fix_goal(self, task: TaskDetail, failure_summary: str) -> str:
        """Ask the LLM for one short follow-up task goal that targets the failing GitHub checks directly."""

        user_prompt = (
            f"A draft pull request created by the Feberdin workflow now has failing GitHub checks.\n"
            f"Repository: {task.repository}\n"
            f"Original goal: {task.goal}\n"
            f"Pull request: {task.pull_request_url}\n"
            f"Failed checks: {failure_summary}\n\n"
            "Write ONE concise follow-up coding goal (max 180 characters) that fixes the likely root cause in the same repository. "
            "Start with an action verb like Fix/Add/Update/Remove. Return only the goal sentence."
        )
        raw = await self.llm.complete(
            "You are a CI failure triage expert. Return only one concrete follow-up fix goal sentence.",
            user_prompt,
            worker_name="github_autofix",
            max_tokens=96,
        )
        goal = raw.strip().strip("\"'").strip()
        return goal[:180] if goal else self._fallback_fix_goal(task.goal, failure_summary)

    def _fallback_fix_goal(self, original_goal: str, failure_summary: str) -> str:
        """Return a deterministic minimal goal when the LLM is unavailable."""

        failure_snippet = failure_summary[:120].strip()
        return f"Fix failing GitHub checks for: {original_goal[:80]} ({failure_snippet})"[:180]

    def _format_failed_checks(self, failed_checks: list[dict[str, Any]]) -> str:
        """Compress several failed checks into one operator-readable summary string."""

        parts: list[str] = []
        for check in failed_checks[:4]:
            name = str(check.get("name") or "GitHub Check").strip()
            conclusion = str(check.get("conclusion") or check.get("status") or "failed").strip()
            summary = str(check.get("summary") or "").strip()
            part = f"{name} ({conclusion})"
            if summary:
                part = f"{part}: {summary}"
            parts.append(part)
        return " | ".join(parts)[:500]

    def _failure_signature(self, head_sha: str, failed_checks: list[dict[str, Any]]) -> str:
        """Build a stable fingerprint so the same failing check set does not spawn duplicate follow-up tasks."""

        normalized_checks: list[str] = []
        for item in failed_checks:
            name = str(item.get("name") or "").strip().lower()
            conclusion = str(item.get("conclusion") or item.get("status") or "").strip().lower()
            summary = str(item.get("summary") or "").strip().lower()
            normalized_checks.append(f"{name}:{conclusion}:{summary[:120]}")
        joined = "|".join(sorted(normalized_checks))
        return f"{head_sha}:{joined}"[:1200]

    async def _monitor_fix(self, fix_task_id: str, parent_task_id: str) -> None:
        """Mirror the follow-up fix task back onto the original PR task so operators can see whether auto-fix helped."""

        elapsed = 0.0
        while elapsed < _FIX_MONITOR_TIMEOUT_SECONDS:
            await asyncio.sleep(_FIX_MONITOR_POLL_SECONDS)
            elapsed += _FIX_MONITOR_POLL_SECONDS

            try:
                fix_task = self.task_service.get_task(fix_task_id)
            except KeyError:
                return

            if fix_task.status in {TaskStatus.DONE, TaskStatus.PR_CREATED}:
                self.task_service.update_runtime_context(
                    parent_task_id,
                    metadata_updates={
                        "github_autofix_status": "fix_ready",
                        "github_autofix_fix_branch": fix_task.branch_name,
                        "github_autofix_fix_pr_url": fix_task.pull_request_url,
                        "github_autofix_fix_completed_at": datetime.now(UTC).isoformat(),
                    },
                )
                return

            if fix_task.status == TaskStatus.FAILED:
                self.task_service.update_runtime_context(
                    parent_task_id,
                    metadata_updates={
                        "github_autofix_status": "fix_failed",
                        "github_autofix_fix_error": (fix_task.latest_error or "GitHub-Auto-Fix-Task fehlgeschlagen.")[:500],
                        "github_autofix_fix_completed_at": datetime.now(UTC).isoformat(),
                    },
                )
                return

        self.task_service.update_runtime_context(
            parent_task_id,
            metadata_updates={
                "github_autofix_status": "fix_monitor_timeout",
                "github_autofix_fix_completed_at": datetime.now(UTC).isoformat(),
            },
        )
