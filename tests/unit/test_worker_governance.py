"""
Purpose: Validate worker guidance persistence, decision-tree annotations, and suggestion approvals.
Input/Output: Exercises the worker governance service with isolated runtime paths and synthetic worker results.
Important invariants: Guidance stays worker-specific, suggestions deduplicate, and decision trees remain visible to the UI.
How to debug: If a test fails, inspect the stored JSON files in the temporary data directory for normalization drift.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from services.shared.agentic_lab.config import get_settings
from services.shared.agentic_lab.schemas import (
    ImprovementSuggestion,
    ImprovementSuggestionDecisionRequest,
    ImprovementSuggestionScope,
    ImprovementSuggestionStatus,
    WorkerRequest,
    WorkerResponse,
)
from services.shared.agentic_lab.worker_governance import WorkerGovernanceService


def _service(tmp_path) -> WorkerGovernanceService:
    return WorkerGovernanceService(
        get_settings(),
        guidance_path=tmp_path / "worker_guidance.json",
        suggestions_path=tmp_path / "improvement_suggestions.json",
    )


def _sample_request(worker_name: str = "research", *, task_id: str = "task-1") -> WorkerRequest:
    return WorkerRequest(
        task_id=task_id,
        goal="Research the latest official FastAPI version and summarize the repository context.",
        repository="Feberdin/example-repo",
        repo_url="https://github.com/Feberdin/example-repo.git",
        local_repo_path="/workspace/example-repo",
        base_branch="main",
        metadata={"current_worker_name": worker_name},
    )


def _architecture_response() -> WorkerResponse:
    return WorkerResponse(
        worker="architecture",
        summary="Architecture review completed.",
        outputs={"approval_gates": ["manual-risk-review", "staging-approval"]},
    )


def _write_manual_suggestion(
    service: WorkerGovernanceService,
    *,
    repository: str,
    worker_name: str,
    title: str,
    action: str,
    status: ImprovementSuggestionStatus,
) -> None:
    now = datetime.now(UTC)
    registry = service.load_suggestion_registry()
    registry.suggestions.append(
        ImprovementSuggestion(
            id=str(uuid4()),
            worker_name=worker_name,
            task_id="historic-task",
            repository=repository,
            title=title,
            summary="Historischer Vorschlag fuer die Registry.",
            rationale="Persistierter Testdatensatz.",
            suggested_action=action,
            impact="medium",
            status=status,
            scope=(
                ImprovementSuggestionScope.REPOSITORY_WIDE
                if status in {
                    ImprovementSuggestionStatus.IMPLEMENTED,
                    ImprovementSuggestionStatus.DISMISSED,
                    ImprovementSuggestionStatus.SUPPRESSED_FOR_REPOSITORY,
                }
                else ImprovementSuggestionScope.TASK_LOCAL
            ),
            created_at=now,
            updated_at=now,
        )
    )
    service._write_suggestion_registry(service._normalize_suggestion_registry(registry))


def test_guidance_registry_loads_seed_workers(tmp_path) -> None:
    service = _service(tmp_path)

    registry = service.load_guidance_registry()

    assert len(registry.workers) == 17
    coding_policy = next(worker for worker in registry.workers if worker.worker_name == "coding")
    qa_policy = next(worker for worker in registry.workers if worker.worker_name == "qa")
    assert coding_policy.role_description.startswith("Setzt minimal-invasive")
    assert coding_policy.auto_submit_suggestions is True
    assert qa_policy.auto_submit_suggestions is False


def test_annotate_worker_response_adds_guidance_and_decision_tree(tmp_path) -> None:
    service = _service(tmp_path)
    request = _sample_request("reviewer")
    response = WorkerResponse(
        worker="reviewer",
        summary="Diff review completed.",
        outputs={"findings": ["Code files changed without test update."]},
        warnings=["Tests should be reviewed."],
        risk_flags=["infrastructure_change"],
        requires_human_approval=True,
        approval_reason="Risky changes detected.",
    )

    annotated = service.annotate_worker_response("reviewer", request, response)

    assert "applied_guidance" in annotated.outputs
    assert "decision_tree" in annotated.outputs
    assert annotated.outputs["decision_tree"]["worker_name"] == "reviewer"
    assert annotated.outputs["applied_guidance"]["display_name"] == "Reviewer Worker"
    assert annotated.outputs["applied_guidance"]["role_description"].startswith("Prüft Diffs")
    assert annotated.outputs["applied_guidance"]["ui_language"] == "de"
    assert "global_minimum_rules" in annotated.outputs["applied_guidance"]


def test_existing_guidance_registry_is_backfilled_with_new_defaults(tmp_path) -> None:
    service = _service(tmp_path)
    legacy_payload = {
        "workers": [
            {
                "worker_name": "coding",
                "display_name": "Coding Worker Custom",
                "enabled": True,
                "role_summary": "Kurze Altbeschreibung fuer den Coding-Worker.",
                "operator_recommendations": ["Alte Empfehlung beibehalten."],
                "decision_preferences": ["Altentscheidung dokumentieren."],
                "competence_boundary": "Alte Scope-Grenze.",
                "escalate_beyond_boundary": False,
                "auto_submit_improvement_suggestions": False,
            }
        ]
    }
    service.guidance_path.write_text(json.dumps(legacy_payload, indent=2), encoding="utf-8")

    registry = service.load_guidance_registry()

    assert len(registry.workers) == 17
    coding_policy = next(worker for worker in registry.workers if worker.worker_name == "coding")
    requirements_policy = next(worker for worker in registry.workers if worker.worker_name == "requirements")
    assert coding_policy.display_name == "Coding Worker Custom"
    assert coding_policy.role_description == "Kurze Altbeschreibung fuer den Coding-Worker."
    assert coding_policy.escalate_out_of_scope is False
    assert coding_policy.auto_submit_suggestions is False
    assert requirements_policy.role_description.startswith("Strukturiert Aufträge")

    persisted = json.loads(service.guidance_path.read_text("utf-8"))
    assert "role_description" in persisted["workers"][0]
    assert "role_summary" not in persisted["workers"][0]


def test_register_worker_suggestions_deduplicates_titles(tmp_path) -> None:
    service = _service(tmp_path)
    request = _sample_request("architecture")
    response = _architecture_response()

    first = service.register_worker_suggestions(worker_name="architecture", request=request, response=response)
    second = service.register_worker_suggestions(worker_name="architecture", request=request, response=response)

    assert len(first) == 1
    assert second == []


def test_suggestion_decision_updates_registry(tmp_path) -> None:
    service = _service(tmp_path)
    request = _sample_request("security")
    response = WorkerResponse(
        worker="security",
        summary="Security review completed.",
        outputs={},
        risk_flags=["security_manual_review"],
    )
    created = service.register_worker_suggestions(worker_name="security", request=request, response=response)

    registry = service.decide_suggestion(
        created[0].id,
        ImprovementSuggestionDecisionRequest(
            decision=ImprovementSuggestionStatus.APPROVED,
            actor="ceo-dashboard",
            note="Approved for next governance cycle.",
        ),
    )

    decided = next(item for item in registry.suggestions if item.id == created[0].id)
    assert decided.status is ImprovementSuggestionStatus.APPROVED
    assert decided.scope is ImprovementSuggestionScope.TASK_LOCAL
    assert decided.actor == "ceo-dashboard"


def test_identical_suggestion_in_new_task_is_not_pending_when_implemented_repo_wide(tmp_path) -> None:
    service = _service(tmp_path)
    first = service.register_worker_suggestions(
        worker_name="architecture",
        request=_sample_request("architecture", task_id="task-1"),
        response=_architecture_response(),
    )

    service.decide_suggestion(
        first[0].id,
        ImprovementSuggestionDecisionRequest(
            decision=ImprovementSuggestionStatus.IMPLEMENTED,
            actor="ceo-dashboard",
            note="Governance wurde bereits im Repo dokumentiert.",
        ),
    )

    second = service.register_worker_suggestions(
        worker_name="architecture",
        request=_sample_request("architecture", task_id="task-2"),
        response=_architecture_response(),
    )

    registry = service.load_suggestion_registry()
    assert second == []
    assert len(registry.suggestions) == 1
    assert all(item.status != ImprovementSuggestionStatus.PENDING for item in registry.suggestions)


def test_approved_suggestion_can_reappear_in_a_later_task(tmp_path) -> None:
    service = _service(tmp_path)
    first = service.register_worker_suggestions(
        worker_name="architecture",
        request=_sample_request("architecture", task_id="task-1"),
        response=_architecture_response(),
    )
    service.decide_suggestion(
        first[0].id,
        ImprovementSuggestionDecisionRequest(
            decision=ImprovementSuggestionStatus.APPROVED,
            actor="ceo-dashboard",
            note="Fuer diesen Task genehmigt, aber noch nicht repo-weit umgesetzt.",
        ),
    )

    second = service.register_worker_suggestions(
        worker_name="architecture",
        request=_sample_request("architecture", task_id="task-2"),
        response=_architecture_response(),
    )

    assert len(second) == 1
    assert second[0].status is ImprovementSuggestionStatus.PENDING


def test_dismissed_suggestion_is_suppressed_repository_wide(tmp_path) -> None:
    service = _service(tmp_path)
    first = service.register_worker_suggestions(
        worker_name="architecture",
        request=_sample_request("architecture", task_id="task-1"),
        response=_architecture_response(),
    )
    service.decide_suggestion(
        first[0].id,
        ImprovementSuggestionDecisionRequest(
            decision=ImprovementSuggestionStatus.DISMISSED,
            actor="ceo-dashboard",
            note="Nicht passend fuer dieses Repository.",
        ),
    )

    second = service.register_worker_suggestions(
        worker_name="architecture",
        request=_sample_request("architecture", task_id="task-2"),
        response=_architecture_response(),
    )

    assert second == []


def test_suppressed_for_repository_is_suppressed_repository_wide(tmp_path) -> None:
    service = _service(tmp_path)
    first = service.register_worker_suggestions(
        worker_name="architecture",
        request=_sample_request("architecture", task_id="task-1"),
        response=_architecture_response(),
    )
    service.decide_suggestion(
        first[0].id,
        ImprovementSuggestionDecisionRequest(
            decision=ImprovementSuggestionStatus.SUPPRESSED_FOR_REPOSITORY,
            actor="ceo-dashboard",
            note="Repo-weite Unterdrueckung bestaetigt.",
        ),
    )

    second = service.register_worker_suggestions(
        worker_name="architecture",
        request=_sample_request("architecture", task_id="task-2"),
        response=_architecture_response(),
    )

    assert second == []


def test_materially_changed_suggestion_can_reappear(tmp_path) -> None:
    service = _service(tmp_path)
    _write_manual_suggestion(
        service,
        repository="Feberdin/example-repo",
        worker_name="architecture",
        title="Repository-spezifische Governance dokumentieren",
        action="Lege ein Repository-Profil oder ADR fuer Approval-Gates an.",
        status=ImprovementSuggestionStatus.IMPLEMENTED,
    )

    second = service.register_worker_suggestions(
        worker_name="architecture",
        request=_sample_request("architecture", task_id="task-2"),
        response=_architecture_response(),
    )

    assert len(second) == 1
    assert second[0].status is ImprovementSuggestionStatus.PENDING


def test_legacy_rejected_status_is_normalized_to_dismissed(tmp_path) -> None:
    service = _service(tmp_path)
    _write_manual_suggestion(
        service,
        repository="Feberdin/example-repo",
        worker_name="architecture",
        title="Legacy suggestion",
        action="Legacy action",
        status=ImprovementSuggestionStatus.REJECTED,
    )

    registry = service.load_suggestion_registry()

    assert registry.suggestions[0].status is ImprovementSuggestionStatus.DISMISSED
    assert registry.suggestions[0].scope is ImprovementSuggestionScope.REPOSITORY_WIDE
