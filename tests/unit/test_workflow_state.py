"""
Purpose: Verify workflow routing decisions for resumable and approval-gated tasks.
Input/Output: Tests call the orchestrator routing helpers with representative state dictionaries.
Important invariants: Approved tasks must resume at the stored target, and blocked tasks must stop cleanly.
How to debug: If routing changes unexpectedly, inspect the mapping logic in `services/orchestrator/workflow.py`.
"""

from __future__ import annotations

from services.orchestrator.workflow import WorkflowOrchestrator
from services.shared.agentic_lab.config import get_settings
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
