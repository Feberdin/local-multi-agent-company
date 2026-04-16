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


def test_readme_smiley_profile_skips_research_and_architecture(isolated_session_factory) -> None:
    orchestrator = WorkflowOrchestrator(get_settings(), TaskService(session_factory=isolated_session_factory))
    state = {
        "task_id": "task-fast-path",
        "goal": "Fuege am Anfang der Readme einen Smiley ein.",
        "repository": "Feberdin/local-multi-agent-company",
        "local_repo_path": "/workspace/local-multi-agent-company",
        "base_branch": "main",
        "current_status": "RESOURCE_PLANNING",
        "approval_required": False,
        "metadata": {"task_profile": {"name": "readme_prefix_smiley_fix"}},
    }

    assert orchestrator._route_after_human_resources(state) == "coding"  # pyright: ignore[reportPrivateUsage]
    assert orchestrator._route_after_research(state) == "coding"  # pyright: ignore[reportPrivateUsage]


def test_readme_smiley_profile_skips_review_testing_security_and_deploy(isolated_session_factory) -> None:
    orchestrator = WorkflowOrchestrator(get_settings(), TaskService(session_factory=isolated_session_factory))
    state = {
        "task_id": "task-fast-tail",
        "goal": "Fuege am Anfang der Readme einen Smiley ein.",
        "repository": "Feberdin/local-multi-agent-company",
        "local_repo_path": "/workspace/local-multi-agent-company",
        "base_branch": "main",
        "current_status": "CODING",
        "approval_required": False,
        "auto_deploy_staging": True,
        "metadata": {"task_profile": {"name": "readme_prefix_smiley_fix"}},
    }

    assert orchestrator._route_after_coding(state) == "validation"  # pyright: ignore[reportPrivateUsage]
    assert orchestrator._route_after_validation(state) == "github"  # pyright: ignore[reportPrivateUsage]
    assert orchestrator._route_after_github(state) == "memory"  # pyright: ignore[reportPrivateUsage]


def test_readme_smiley_profile_graph_maps_include_fast_path_destinations(isolated_session_factory) -> None:
    orchestrator = WorkflowOrchestrator(get_settings(), TaskService(session_factory=isolated_session_factory))

    assert orchestrator._human_resources_route_map()["coding"] == "coding"  # pyright: ignore[reportPrivateUsage]
    assert orchestrator._research_route_map()["coding"] == "coding"  # pyright: ignore[reportPrivateUsage]
    assert orchestrator._coding_route_map()["validation"] == "validation"  # pyright: ignore[reportPrivateUsage]
    assert orchestrator._validation_route_map()["github"] == "github"  # pyright: ignore[reportPrivateUsage]
    assert orchestrator._github_route_map()["memory"] == "memory"  # pyright: ignore[reportPrivateUsage]


def test_readme_smiley_profile_uses_fast_path_handoff_labels(isolated_session_factory) -> None:
    orchestrator = WorkflowOrchestrator(get_settings(), TaskService(session_factory=isolated_session_factory))
    state = {
        "task_id": "task-fast-handoff",
        "goal": "Fuege am Anfang der Readme einen Smiley ein.",
        "repository": "Feberdin/local-multi-agent-company",
        "local_repo_path": "/workspace/local-multi-agent-company",
        "base_branch": "main",
        "metadata": {"task_profile": {"name": "readme_prefix_smiley_fix"}},
    }

    assert orchestrator._next_worker_name("human_resources", state) == "coding"  # pyright: ignore[reportPrivateUsage]
    assert orchestrator._previous_worker_name("coding", state) == "human_resources"  # pyright: ignore[reportPrivateUsage]
    assert orchestrator._next_worker_name("github", state) == "memory"  # pyright: ignore[reportPrivateUsage]


def test_worker_stage_timeout_profile_skips_research_and_architecture(isolated_session_factory) -> None:
    orchestrator = WorkflowOrchestrator(get_settings(), TaskService(session_factory=isolated_session_factory))
    state = {
        "task_id": "task-timeout-fast-path",
        "goal": "Change WORKER_STAGE_TIMEOUT_SECONDS to 3600 in worker.py",
        "repository": "Feberdin/local-multi-agent-company",
        "local_repo_path": "/workspace/local-multi-agent-company",
        "base_branch": "main",
        "current_status": "RESOURCE_PLANNING",
        "approval_required": False,
        "metadata": {
            "task_profile": {
                "name": "worker_stage_timeout_config_fix",
                "target_timeout_seconds": 3600.0,
                "skip_research": True,
                "skip_architecture": True,
                "route_after_coding": "validation",
                "route_after_validation": "github",
                "route_after_github": "memory",
            }
        },
    }

    assert orchestrator._route_after_human_resources(state) == "coding"  # pyright: ignore[reportPrivateUsage]
    assert orchestrator._route_after_coding(state) == "validation"  # pyright: ignore[reportPrivateUsage]
    assert orchestrator._route_after_validation(state) == "github"  # pyright: ignore[reportPrivateUsage]
    assert orchestrator._route_after_github(state) == "memory"  # pyright: ignore[reportPrivateUsage]


def test_worker_stage_timeout_profile_uses_fast_path_handoff_labels(isolated_session_factory) -> None:
    orchestrator = WorkflowOrchestrator(get_settings(), TaskService(session_factory=isolated_session_factory))
    state = {
        "task_id": "task-timeout-handoff",
        "goal": "Change WORKER_STAGE_TIMEOUT_SECONDS to 3600 in worker.py",
        "repository": "Feberdin/local-multi-agent-company",
        "local_repo_path": "/workspace/local-multi-agent-company",
        "base_branch": "main",
        "metadata": {
            "task_profile": {
                "name": "worker_stage_timeout_config_fix",
                "target_timeout_seconds": 3600.0,
                "skip_research": True,
                "skip_architecture": True,
                "route_after_coding": "validation",
                "route_after_validation": "github",
                "route_after_github": "memory",
            }
        },
    }

    assert orchestrator._next_worker_name("human_resources", state) == "coding"  # pyright: ignore[reportPrivateUsage]
    assert orchestrator._previous_worker_name("validation", state) == "coding"  # pyright: ignore[reportPrivateUsage]
