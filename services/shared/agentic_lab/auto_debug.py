"""
Purpose: Autonomous error analysis and targeted self-fix for failed tasks.
Input/Output: Triggered after a task fails; analyzes the error, creates a focused
              self-improvement task against the system's own codebase, and tracks progress.
Important invariants:
  - Never debugs tasks that are themselves debug/improvement tasks (recursion guard).
  - Stores all state in task metadata so the UI can display progress without extra DB tables.
  - At most AUTO_DEBUG_MAX_ATTEMPTS auto-debug cycles per original task.
How to debug: Check metadata fields auto_debug_status, auto_debug_fix_task_id on the failed task.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from services.shared.agentic_lab.config import Settings
from services.shared.agentic_lab.llm import LLMClient, LLMError
from services.shared.agentic_lab.schemas import TaskCreateRequest, TaskStatus
from services.shared.agentic_lab.task_service import TaskService

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 30.0
_MAX_WAIT_HOURS = 2.0

# Worker names as they appear in worker_results keyed dict
_PIPELINE_ORDER = [
    "requirements", "cost", "human_resources", "research",
    "architecture", "coding", "reviewer", "tester",
    "security", "validation", "documentation", "github", "deploy", "qa",
]


class AutoDebugService:
    """Detect task failures, analyze the root cause via LLM, and launch a targeted fix cycle."""

    def __init__(self, task_service: TaskService, llm: LLMClient, settings: Settings) -> None:
        self.task_service = task_service
        self.llm = llm
        self.settings = settings

    async def maybe_debug(
        self,
        task_id: str,
        run_task_fn: Callable[[str], Awaitable[None]],
    ) -> bool:
        """Entry point: called after a task completes. Returns True if an auto-debug cycle was started."""
        if not self.settings.auto_debug_enabled:
            return False

        try:
            task = self.task_service.get_task(task_id)
        except KeyError:
            return False

        if task.status != TaskStatus.FAILED:
            return False

        meta = task.metadata or {}

        # Recursion guards: don't debug fix tasks or improvement tasks
        if meta.get("auto_debug_parent_task_id"):
            return False
        if meta.get("self_improvement_cycle_id"):
            return False

        attempt = int(meta.get("auto_debug_attempt", 0))
        if attempt >= self.settings.auto_debug_max_attempts:
            logger.info(
                "auto-debug: task %s already attempted %d/%d times, skipping.",
                task_id, attempt, self.settings.auto_debug_max_attempts,
            )
            return False

        failed_worker, error_text = self._extract_failure(task)
        if not failed_worker or not error_text:
            logger.info("auto-debug: task %s has no extractable failure info, skipping.", task_id)
            return False

        logger.info(
            "auto-debug: task %s failed at worker=%s — launching fix cycle (attempt %d/%d).",
            task_id, failed_worker, attempt + 1, self.settings.auto_debug_max_attempts,
        )

        try:
            fix_goal = await self._generate_fix_goal(task.goal, failed_worker, error_text, task.repository)
        except LLMError as exc:
            logger.warning("auto-debug: LLM analysis failed for task %s: %s", task_id, exc)
            # Use a deterministic fallback goal when LLM is unavailable
            fix_goal = f"Fix {failed_worker} worker error: {error_text[:120]}"

        # Mark original task with auto-debug metadata before creating the fix task
        self.task_service.update_runtime_context(
            task_id,
            metadata_updates={
                "auto_debug_status": "fix_in_progress",
                "auto_debug_attempt": attempt + 1,
                "auto_debug_failed_worker": failed_worker,
                "auto_debug_error_summary": error_text[:400],
                "auto_debug_fix_goal": fix_goal,
                "auto_debug_started_at": datetime.now(UTC).isoformat(),
            },
        )

        # Create the fix task targeting the system's own repository
        fix_summary = self.task_service.create_task(
            TaskCreateRequest(
                goal=fix_goal,
                repository=self.settings.self_improvement_target_repo,
                local_repo_path=self.settings.self_improvement_local_repo_path,
                base_branch="main",
                metadata={
                    "auto_debug_parent_task_id": task_id,
                    "auto_debug_target_worker": failed_worker,
                    "auto_debug_attempt": attempt + 1,
                },
            )
        )
        fix_task_id = fix_summary.id
        logger.info("auto-debug: created fix task %s for parent %s (goal: %s)", fix_task_id, task_id, fix_goal)

        # Update parent task with the fix task ID immediately so the UI can link to it
        self.task_service.update_runtime_context(
            task_id,
            metadata_updates={"auto_debug_fix_task_id": fix_task_id},
        )

        # Start the fix task and monitor it in the background
        await run_task_fn(fix_task_id)
        asyncio.create_task(self._monitor_fix(fix_task_id, task_id))
        return True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _extract_failure(self, task) -> tuple[str | None, str | None]:
        """Return (worker_name, error_text) for the first failed worker, or (None, None)."""
        worker_results = task.worker_results or {}

        for worker in _PIPELINE_ORDER:
            result = worker_results.get(worker)
            if not result:
                continue
            if result.get("success", True):
                continue
            errors = result.get("errors") or []
            error_text = "; ".join(str(e) for e in errors) if errors else result.get("summary", "")
            if error_text:
                return worker, error_text

        # Fallback to task-level latest_error
        if task.latest_error:
            return "unknown", task.latest_error

        return None, None

    async def _generate_fix_goal(
        self,
        original_goal: str,
        failed_worker: str,
        error_text: str,
        repository: str,
    ) -> str:
        """Ask the LLM for a single concrete fix goal sentence."""
        user_prompt = (
            f"A task failed in the Feberdin multi-agent system.\n"
            f"Failed worker: {failed_worker}\n"
            f"Original task goal: {original_goal}\n"
            f"Error: {error_text}\n"
            f"Target repository: {repository}\n\n"
            "Write ONE concise fix goal (max 150 characters) for a coding agent to fix the root cause "
            f"in the {failed_worker} worker source code. "
            "Start with an action verb (Fix / Add / Update / Remove). "
            "Do not mention task IDs, timestamps, or the word 'error' alone. "
            "Return only the goal sentence, nothing else."
        )
        raw = await self.llm.complete(
            "You are a debugging expert. Return only the single fix goal sentence.",
            user_prompt,
            worker_name="auto_debug",
            max_tokens=80,
        )
        return raw.strip().strip("\"'").strip()[:200]

    async def _monitor_fix(self, fix_task_id: str, parent_task_id: str) -> None:
        """Poll the fix task until it finishes and update the parent task metadata accordingly."""
        deadline_seconds = _MAX_WAIT_HOURS * 3600
        elapsed = 0.0

        while elapsed < deadline_seconds:
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
            elapsed += _POLL_INTERVAL_SECONDS

            try:
                fix_task = self.task_service.get_task(fix_task_id)
            except KeyError:
                logger.warning("auto-debug: fix task %s disappeared, stopping monitor.", fix_task_id)
                break

            if fix_task.status in {TaskStatus.DONE, TaskStatus.PR_CREATED}:
                logger.info(
                    "auto-debug: fix task %s succeeded (status=%s), updating parent %s.",
                    fix_task_id, fix_task.status, parent_task_id,
                )
                self.task_service.update_runtime_context(
                    parent_task_id,
                    metadata_updates={
                        "auto_debug_status": "fix_ready",
                        "auto_debug_fix_branch": fix_task.branch_name,
                        "auto_debug_fix_pr_url": fix_task.pull_request_url,
                        "auto_debug_fix_completed_at": datetime.now(UTC).isoformat(),
                    },
                )
                return

            if fix_task.status == TaskStatus.FAILED:
                logger.warning(
                    "auto-debug: fix task %s failed, updating parent %s.", fix_task_id, parent_task_id
                )
                self.task_service.update_runtime_context(
                    parent_task_id,
                    metadata_updates={
                        "auto_debug_status": "fix_failed",
                        "auto_debug_fix_error": (fix_task.latest_error or "Fix-Task fehlgeschlagen.")[:400],
                        "auto_debug_fix_completed_at": datetime.now(UTC).isoformat(),
                    },
                )
                return

        # Monitoring deadline exceeded
        logger.warning(
            "auto-debug: monitoring timed out for fix task %s (parent=%s).",
            fix_task_id, parent_task_id,
        )
        self.task_service.update_runtime_context(
            parent_task_id,
            metadata_updates={
                "auto_debug_status": "fix_timeout",
                "auto_debug_fix_completed_at": datetime.now(UTC).isoformat(),
            },
        )
