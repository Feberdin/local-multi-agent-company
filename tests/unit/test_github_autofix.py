"""
Purpose: Verify that failed GitHub PR checks automatically create one focused follow-up fix task.
Input/Output: Tests create tasks with draft PR metadata, feed synthetic GitHub CI summaries, and inspect the
              generated follow-up task plus operator-visible task metadata.
Important invariants:
  - The same failed check signature must not spawn duplicate fix tasks.
  - Successful checks must be recorded honestly without creating unnecessary work.
How to debug: If this fails, inspect services/shared/agentic_lab/github_autofix.py together with the task metadata.
"""

from __future__ import annotations

import asyncio

import pytest

from services.shared.agentic_lab import github_autofix as github_autofix_module
from services.shared.agentic_lab.config import Settings
from services.shared.agentic_lab.github_autofix import GitHubAutofixService
from services.shared.agentic_lab.schemas import TaskCreateRequest, TaskStatus
from services.shared.agentic_lab.task_service import TaskService


class _LLMStub:
    """Return one deterministic follow-up goal so the test can focus on orchestration behavior."""

    async def complete(self, system_prompt: str, user_prompt: str, **kwargs) -> str:  # noqa: ARG002
        return "Fix failing mypy workflow timeout handling."


class _GitHubClientStub:
    """Serve one deterministic PR head and CI summary without real network calls."""

    def __init__(self, summary: dict[str, object]) -> None:
        self.summary = summary

    async def get_pull_request(self, repository: str, pull_number: int) -> dict[str, object]:  # noqa: ARG002
        return {"number": pull_number, "head": {"sha": "abc123def456"}}

    async def get_commit_check_overview(self, repository: str, ref: str) -> dict[str, object]:  # noqa: ARG002
        return dict(self.summary)


@pytest.mark.asyncio
async def test_github_autofix_creates_follow_up_fix_task_for_failed_pr_checks(
    isolated_session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghs-test-token")
    monkeypatch.setenv("GITHUB_AUTO_FIX_ENABLED", "true")
    monkeypatch.setenv("GITHUB_AUTO_FIX_MAX_ATTEMPTS", "2")
    settings = Settings()
    task_service = TaskService(session_factory=isolated_session_factory, settings=settings)
    summary = task_service.create_task(
        TaskCreateRequest(
            goal="Fix the CI failure when workflow typing checks break.",
            repository="Feberdin/example-repo",
            local_repo_path="/workspace/example-repo",
        )
    )
    task_service.set_pull_request(summary.id, "https://github.com/Feberdin/example-repo/pull/17")
    task_service.update_status(
        summary.id,
        TaskStatus.PR_CREATED,
        message="Draft PR created.",
        details={"pull_request_url": "https://github.com/Feberdin/example-repo/pull/17"},
    )

    service = GitHubAutofixService(
        task_service=task_service,
        github_client=_GitHubClientStub(
            {
                "overall_state": "failure",
                "completed": True,
                "failed_checks": [
                    {
                        "name": "validate",
                        "status": "completed",
                        "conclusion": "failure",
                        "summary": "mypy failed in services/orchestrator/workflow.py",
                        "details_url": "https://github.com/Feberdin/example-repo/actions/runs/1",
                    }
                ],
                "pending_checks": [],
                "successful_checks": [],
            }
        ),
        llm=_LLMStub(),
        settings=settings,
    )
    scheduled_runs: list[str] = []
    spawned_tasks: list[asyncio.Task[None]] = []

    async def run_task_fn(task_id: str) -> None:
        scheduled_runs.append(task_id)

    async def _monitor_fix_noop(fix_task_id: str, parent_task_id: str) -> None:  # noqa: ARG001
        return None

    def _spawn(coro):
        task = asyncio.get_running_loop().create_task(coro)
        spawned_tasks.append(task)
        return task

    monkeypatch.setattr(service, "_monitor_fix", _monitor_fix_noop)
    monkeypatch.setattr(github_autofix_module.asyncio, "create_task", _spawn)

    started = await service.inspect_task(summary.id, run_task_fn)
    if spawned_tasks:
        await asyncio.gather(*spawned_tasks)

    parent = task_service.get_task(summary.id)
    fix_task_id = str(parent.metadata.get("github_autofix_fix_task_id") or "")
    fix_task = task_service.get_task(fix_task_id)

    assert started is True
    assert scheduled_runs == [fix_task_id]
    assert parent.metadata["github_autofix_status"] == "fix_in_progress"
    assert "validate" in str(parent.metadata["github_autofix_error_summary"])
    assert parent.metadata["github_autofix_pull_request_number"] == 17
    assert fix_task.repository == "Feberdin/example-repo"
    assert fix_task.metadata["github_autofix_parent_task_id"] == summary.id
    assert fix_task.metadata["github_autofix_root_task_id"] == summary.id
    assert fix_task.metadata["worker_project_label"] == "GitHub-CI Auto-Fix"


@pytest.mark.asyncio
async def test_github_autofix_deduplicates_the_same_failed_check_signature(
    isolated_session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghs-test-token")
    monkeypatch.setenv("GITHUB_AUTO_FIX_ENABLED", "true")
    settings = Settings()
    task_service = TaskService(session_factory=isolated_session_factory, settings=settings)
    summary = task_service.create_task(
        TaskCreateRequest(
            goal="Fix duplicate GitHub CI follow-ups.",
            repository="Feberdin/example-repo",
            local_repo_path="/workspace/example-repo",
        )
    )
    task_service.set_pull_request(summary.id, "https://github.com/Feberdin/example-repo/pull/22")
    task_service.update_status(summary.id, TaskStatus.PR_CREATED, message="Draft PR created.")

    service = GitHubAutofixService(
        task_service=task_service,
        github_client=_GitHubClientStub(
            {
                "overall_state": "failure",
                "completed": True,
                "failed_checks": [
                    {
                        "name": "validate",
                        "status": "completed",
                        "conclusion": "failure",
                        "summary": "same failure",
                        "details_url": "",
                    }
                ],
                "pending_checks": [],
                "successful_checks": [],
            }
        ),
        llm=_LLMStub(),
        settings=settings,
    )

    async def run_task_fn(task_id: str) -> None:  # noqa: ARG001
        return None

    async def _monitor_fix_noop(fix_task_id: str, parent_task_id: str) -> None:  # noqa: ARG001
        return None

    monkeypatch.setattr(service, "_monitor_fix", _monitor_fix_noop)
    spawned_tasks: list[asyncio.Task[None]] = []

    def _spawn(coro):
        task = asyncio.get_running_loop().create_task(coro)
        spawned_tasks.append(task)
        return task

    monkeypatch.setattr(github_autofix_module.asyncio, "create_task", _spawn)

    first_started = await service.inspect_task(summary.id, run_task_fn)
    if spawned_tasks:
        await asyncio.gather(*spawned_tasks)
    spawned_tasks.clear()
    second_started = await service.inspect_task(summary.id, run_task_fn)
    if spawned_tasks:
        await asyncio.gather(*spawned_tasks)

    parent = task_service.get_task(summary.id)

    assert first_started is True
    assert second_started is False
    assert parent.metadata["github_autofix_status"] == "fix_in_progress"


@pytest.mark.asyncio
async def test_github_autofix_marks_green_checks_without_creating_follow_up_work(
    isolated_session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghs-test-token")
    monkeypatch.setenv("GITHUB_AUTO_FIX_ENABLED", "true")
    settings = Settings()
    task_service = TaskService(session_factory=isolated_session_factory, settings=settings)
    summary = task_service.create_task(
        TaskCreateRequest(
            goal="Observe healthy GitHub checks without extra work.",
            repository="Feberdin/example-repo",
            local_repo_path="/workspace/example-repo",
        )
    )
    task_service.set_pull_request(summary.id, "https://github.com/Feberdin/example-repo/pull/31")
    task_service.update_status(summary.id, TaskStatus.PR_CREATED, message="Draft PR created.")

    service = GitHubAutofixService(
        task_service=task_service,
        github_client=_GitHubClientStub(
            {
                "overall_state": "success",
                "completed": True,
                "failed_checks": [],
                "pending_checks": [],
                "successful_checks": [{"name": "validate", "status": "completed", "conclusion": "success"}],
            }
        ),
        llm=_LLMStub(),
        settings=settings,
    )

    async def run_task_fn(task_id: str) -> None:  # noqa: ARG001
        raise AssertionError("No follow-up task should be created for green checks.")

    started = await service.inspect_task(summary.id, run_task_fn)
    parent = task_service.get_task(summary.id)

    assert started is False
    assert parent.metadata["github_autofix_status"] == "checks_passed"
    assert "github_autofix_fix_task_id" not in parent.metadata
