"""
Purpose: Verify that automatic self-fix tasks are spawned when a normal task fails in one worker.
Input/Output: The tests create a failed task in an isolated SQLite database and inspect the auto-debug follow-up task.
Important invariants:
  - Auto-debug must create a new focused fix task instead of mutating the failed task in place.
  - The parent task must keep enough metadata for the UI to link the operator to the generated fix run.
How to debug: If this fails, inspect services/shared/agentic_lab/auto_debug.py and task metadata updates together.
"""

from __future__ import annotations

import asyncio

import pytest

from services.shared.agentic_lab import auto_debug as auto_debug_module
from services.shared.agentic_lab.auto_debug import AutoDebugService
from services.shared.agentic_lab.config import Settings
from services.shared.agentic_lab.schemas import TaskCreateRequest, TaskStatus, WorkerResponse
from services.shared.agentic_lab.task_service import TaskService


class _LLMStub:
    """Return one deterministic fix goal so the test can focus on orchestration behavior."""

    async def complete(self, system_prompt: str, user_prompt: str, **kwargs) -> str:  # noqa: ARG002
        return "Fix coding worker no-op plan handling."


@pytest.mark.asyncio
async def test_auto_debug_creates_fix_task_for_failed_coding_task(
    isolated_session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTO_DEBUG_ENABLED", "true")
    monkeypatch.setenv("SELF_IMPROVEMENT_TARGET_REPO", "Feberdin/local-multi-agent-company")
    monkeypatch.setenv("SELF_IMPROVEMENT_LOCAL_REPO_PATH", "/workspace/local-multi-agent-company")
    settings = Settings()
    task_service = TaskService(session_factory=isolated_session_factory, settings=settings)

    summary = task_service.create_task(
        TaskCreateRequest(
            goal="Fix the coding worker when it returns no file operations.",
            repository="Feberdin/example-repo",
            local_repo_path="/workspace/example-repo",
        )
    )
    task_service.store_worker_result(
        summary.id,
        "coding",
        WorkerResponse(
            worker="coding",
            summary="Coding backend returned no file operations.",
            success=False,
            errors=["The local patch backend did not generate any file operations."],
        ),
    )
    task_service.update_status(
        summary.id,
        TaskStatus.FAILED,
        message="Coding ist fehlgeschlagen.",
        latest_error="The local patch backend did not generate any file operations.",
    )

    service = AutoDebugService(task_service, _LLMStub(), settings)
    spawned_tasks: list[asyncio.Task[None]] = []
    scheduled_runs: list[str] = []

    async def run_task_fn(task_id: str) -> None:
        scheduled_runs.append(task_id)

    async def _monitor_fix_noop(fix_task_id: str, parent_task_id: str) -> None:  # noqa: ARG001
        return None

    def _spawn(coro):
        task = asyncio.get_running_loop().create_task(coro)
        spawned_tasks.append(task)
        return task

    monkeypatch.setattr(service, "_monitor_fix", _monitor_fix_noop)
    monkeypatch.setattr(auto_debug_module.asyncio, "create_task", _spawn)

    started = await service.maybe_debug(summary.id, run_task_fn)
    if spawned_tasks:
        await asyncio.gather(*spawned_tasks)

    parent = task_service.get_task(summary.id)
    fix_task_id = str(parent.metadata.get("auto_debug_fix_task_id") or "")
    fix_task = task_service.get_task(fix_task_id)

    assert started is True
    assert fix_task_id
    assert scheduled_runs == [fix_task_id]
    assert parent.metadata["auto_debug_status"] == "fix_in_progress"
    assert parent.metadata["auto_debug_failed_worker"] == "coding"
    assert parent.metadata["auto_debug_fix_goal"] == "Fix coding worker no-op plan handling."
    assert fix_task.repository == "Feberdin/local-multi-agent-company"
    assert fix_task.local_repo_path.endswith(f"/{fix_task.id}/local-multi-agent-company")
    assert fix_task.metadata["auto_debug_parent_task_id"] == summary.id
    assert fix_task.metadata["auto_debug_target_worker"] == "coding"
    assert fix_task.metadata["deployment_target"] == "self"


@pytest.mark.asyncio
async def test_auto_debug_can_chain_follow_up_fix_tasks_until_attempt_limit(
    isolated_session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTO_DEBUG_ENABLED", "true")
    monkeypatch.setenv("AUTO_DEBUG_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("SELF_IMPROVEMENT_TARGET_REPO", "Feberdin/local-multi-agent-company")
    monkeypatch.setenv("SELF_IMPROVEMENT_LOCAL_REPO_PATH", "/workspace/local-multi-agent-company")
    settings = Settings()
    task_service = TaskService(session_factory=isolated_session_factory, settings=settings)

    root = task_service.create_task(
        TaskCreateRequest(
            goal="Repair coding worker failures automatically.",
            repository="Feberdin/example-repo",
            local_repo_path="/workspace/example-repo",
        )
    )
    task_service.store_worker_result(
        root.id,
        "coding",
        WorkerResponse(
            worker="coding",
            summary="Coding backend returned no file operations.",
            success=False,
            errors=["The local patch backend did not generate any file operations."],
        ),
    )
    task_service.update_status(
        root.id,
        TaskStatus.FAILED,
        message="Coding ist fehlgeschlagen.",
        latest_error="The local patch backend did not generate any file operations.",
    )

    service = AutoDebugService(task_service, _LLMStub(), settings)
    spawned_tasks: list[asyncio.Task[None]] = []
    scheduled_runs: list[str] = []

    async def run_task_fn(task_id: str) -> None:
        scheduled_runs.append(task_id)

    async def _monitor_fix_noop(fix_task_id: str, parent_task_id: str) -> None:  # noqa: ARG001
        return None

    def _spawn(coro):
        task = asyncio.get_running_loop().create_task(coro)
        spawned_tasks.append(task)
        return task

    monkeypatch.setattr(service, "_monitor_fix", _monitor_fix_noop)
    monkeypatch.setattr(auto_debug_module.asyncio, "create_task", _spawn)

    first_started = await service.maybe_debug(root.id, run_task_fn)
    if spawned_tasks:
        await asyncio.gather(*spawned_tasks)
    first_fix_id = str(task_service.get_task(root.id).metadata["auto_debug_fix_task_id"])

    task_service.store_worker_result(
        first_fix_id,
        "coding",
        WorkerResponse(
            worker="coding",
            summary="Second fix attempt also failed.",
            success=False,
            errors=["Patch engine could not apply the generated edit operations."],
        ),
    )
    task_service.update_status(
        first_fix_id,
        TaskStatus.FAILED,
        message="Coding ist erneut fehlgeschlagen.",
        latest_error="Patch engine could not apply the generated edit operations.",
    )

    spawned_tasks.clear()
    second_started = await service.maybe_debug(first_fix_id, run_task_fn)
    if spawned_tasks:
        await asyncio.gather(*spawned_tasks)
    second_fix_id = str(task_service.get_task(first_fix_id).metadata["auto_debug_fix_task_id"])
    second_fix = task_service.get_task(second_fix_id)

    task_service.store_worker_result(
        second_fix_id,
        "coding",
        WorkerResponse(
            worker="coding",
            summary="Third fix attempt failed as well.",
            success=False,
            errors=["Still failing."],
        ),
    )
    task_service.update_status(
        second_fix_id,
        TaskStatus.FAILED,
        message="Coding ist weiterhin fehlgeschlagen.",
        latest_error="Still failing.",
    )

    third_started = await service.maybe_debug(second_fix_id, run_task_fn)

    assert first_started is True
    assert second_started is True
    assert third_started is False
    assert scheduled_runs == [first_fix_id, second_fix_id]
    assert second_fix.metadata["auto_debug_parent_task_id"] == first_fix_id
    assert second_fix.metadata["auto_debug_root_task_id"] == root.id
    assert second_fix.metadata["auto_debug_attempt"] == 2
