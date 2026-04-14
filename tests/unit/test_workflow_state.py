"""
Purpose: Verify workflow routing decisions for resumable and approval-gated tasks.
Input/Output: Tests call the orchestrator routing helpers with representative state dictionaries.
Important invariants: Approved tasks must resume at the stored target, and blocked tasks must stop cleanly.
How to debug: If routing changes unexpectedly, inspect the mapping logic in `services/orchestrator/workflow.py`.
"""

from __future__ import annotations

from services.orchestrator.workflow import WorkflowOrchestrator
from services.shared.agentic_lab.config import get_settings
from services.shared.agentic_lab.schemas import TaskCreateRequest
from services.shared.agentic_lab.task_service import TaskService


def test_route_entry_uses_resume_target_after_approval(isolated_session_factory) -> None:
    orchestrator = WorkflowOrchestrator(get_settings(), TaskService(session_factory=isolated_session_factory))
    route = orchestrator._route_entry(
        {
            "task_id": "123",
            "goal": "demo",
            "repository": "Feberdin/example-repo",
            "local_repo_path": "/workspace/example-repo",
            "base_branch": "main",
            "current_status": "APPROVAL_REQUIRED",
            "approval_required": False,
            "resume_target": "testing",
            "metadata": {},
        }
    )
    assert route == "testing"


def test_route_entry_stops_for_failed_tasks(isolated_session_factory) -> None:
    orchestrator = WorkflowOrchestrator(get_settings(), TaskService(session_factory=isolated_session_factory))
    route = orchestrator._route_entry(
        {
            "task_id": "123",
            "goal": "demo",
            "repository": "Feberdin/example-repo",
            "local_repo_path": "/workspace/example-repo",
            "base_branch": "main",
            "current_status": "FAILED",
            "approval_required": False,
            "metadata": {},
        }
    )
    assert route == "stop"


class _GovernanceStub:
    def __init__(self, guidance_map):
        self._guidance_map = guidance_map

    def guidance_map(self):
        return self._guidance_map


def test_worker_requests_use_frozen_guidance_and_route_snapshots(isolated_session_factory) -> None:
    task_service = TaskService(session_factory=isolated_session_factory)
    summary = task_service.create_task(
        TaskCreateRequest(
            goal="Implementiere einen kleinen Fix mit reproduzierbaren Worker-Snapshots.",
            repository="Feberdin/local-multi-agent-company",
            local_repo_path="/workspace/local-multi-agent-company",
            allow_repository_modifications=True,
        )
    )
    task = task_service.get_task(summary.id)
    governance = _GovernanceStub({"coding": {"display_name": "Coding", "role_description": "Snapshot A"}})
    orchestrator = WorkflowOrchestrator(get_settings(), task_service, worker_governance_service=governance)
    orchestrator._build_worker_route_snapshot = lambda: {"coding": {"provider": "qwen", "model_name": "snap-a"}}  # type: ignore[method-assign]

    state = orchestrator._ensure_execution_snapshots(orchestrator._task_to_state(task))
    governance._guidance_map = {"coding": {"display_name": "Coding", "role_description": "Snapshot B"}}
    orchestrator._build_worker_route_snapshot = lambda: {"coding": {"provider": "mistral", "model_name": "snap-b"}}  # type: ignore[method-assign]

    request = orchestrator._build_worker_request(state, "coding")

    assert request.metadata["current_worker_guidance"]["role_description"] == "Snapshot A"
    assert request.metadata["current_worker_route"]["provider"] == "qwen"


def test_stage_slow_warning_seconds_tracks_route_timeout_but_caps_operator_noise(isolated_session_factory) -> None:
    orchestrator = WorkflowOrchestrator(get_settings(), TaskService(session_factory=isolated_session_factory))

    fast_route_threshold = orchestrator._stage_slow_warning_seconds({"request_timeout_seconds": 600.0})  # pyright: ignore[reportPrivateUsage]
    slow_route_threshold = orchestrator._stage_slow_warning_seconds({"request_timeout_seconds": 1800.0})  # pyright: ignore[reportPrivateUsage]
    fallback_threshold = orchestrator._stage_slow_warning_seconds({})  # pyright: ignore[reportPrivateUsage]

    assert fast_route_threshold == 300.0
    assert slow_route_threshold == 600.0
    assert fallback_threshold == 600.0
