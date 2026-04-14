"""
Purpose: High-level task persistence and audit operations for the orchestrator.
Input/Output: The orchestrator uses this service to create tasks, update statuses, store worker results, and record approvals.
Important invariants: Every meaningful state change is logged as both an event and a snapshot for later recovery and debugging.
How to debug: If the UI and worker state disagree, compare the latest task row with the newest event and snapshot entries.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from services.shared.agentic_lab.config import Settings, get_settings
from services.shared.agentic_lab.db import (
    ApprovalRecord,
    TaskEventRecord,
    TaskRecord,
    TaskSnapshotRecord,
    get_session_factory,
)
from services.shared.agentic_lab.repo_tools import build_task_workspace_path, create_branch_name
from services.shared.agentic_lab.schemas import (
    WORKFLOW_WORKER_ORDER,
    WORKFLOW_WORKER_TO_RESUME_TARGET,
    WORKFLOW_WORKER_TO_STATUS,
    ApprovalDecision,
    ApprovalRequest,
    ApprovalResponse,
    DeploymentConfig,
    TaskArchiveRequest,
    TaskCreateRequest,
    TaskDetail,
    TaskEventResponse,
    TaskStageRestartRequest,
    TaskStatus,
    TaskSummary,
    WorkerResponse,
    WorkflowWorkerName,
)
from services.shared.agentic_lab.task_profiles import infer_task_profile

WORKER_PROJECT_LABEL = "Feberdin local-multi-agent-company worker project"
WORKFLOW_WORKER_INDEX = {worker.value: index for index, worker in enumerate(WORKFLOW_WORKER_ORDER)}
WORKFLOW_WORKER_LABELS = {
    WorkflowWorkerName.REQUIREMENTS.value: "Anforderungen",
    WorkflowWorkerName.COST.value: "Ressourcenplanung",
    WorkflowWorkerName.HUMAN_RESOURCES.value: "Worker-Auswahl",
    WorkflowWorkerName.RESEARCH.value: "Recherche",
    WorkflowWorkerName.ARCHITECTURE.value: "Architektur",
    WorkflowWorkerName.DATA.value: "Daten",
    WorkflowWorkerName.UX.value: "UX",
    WorkflowWorkerName.CODING.value: "Coding",
    WorkflowWorkerName.ROLLBACK.value: "Rollback",
    WorkflowWorkerName.REVIEWER.value: "Review",
    WorkflowWorkerName.TESTER.value: "Tests",
    WorkflowWorkerName.SECURITY.value: "Security",
    WorkflowWorkerName.VALIDATION.value: "Validierung",
    WorkflowWorkerName.DOCUMENTATION.value: "Dokumentation",
    WorkflowWorkerName.GITHUB.value: "GitHub",
    WorkflowWorkerName.DEPLOY.value: "Staging",
    WorkflowWorkerName.QA.value: "QA",
    WorkflowWorkerName.MEMORY.value: "Wissen",
}
ARCHIVE_METADATA_KEYS = ("archived", "archived_at", "archived_by", "archived_reason")
ARCHIVABLE_TASK_STATUSES = {
    TaskStatus.NEW.value,
    TaskStatus.DONE.value,
    TaskStatus.FAILED.value,
}


class TaskService:
    """Encapsulate all database writes so workflow code stays readable and consistent."""

    def __init__(self, session_factory=None, settings: Settings | None = None) -> None:
        self.session_factory = session_factory or get_session_factory()
        self.settings = settings or get_settings()

    def session(self) -> Session:
        return self.session_factory()

    def create_task(self, request: TaskCreateRequest) -> TaskSummary:
        """Create a new orchestrated task with deterministic branch naming and safe defaults."""

        with self.session() as session:
            task_id = str(uuid4())
            source_local_repo_path = request.local_repo_path or f"/workspace/{request.repository.split('/')[-1]}"
            task_workspace_path = build_task_workspace_path(
                task_id,
                request.repository,
                self.settings.workspace_root,
                self.settings.effective_task_workspace_root,
            )
            branch_name = create_branch_name(request.goal, task_id)
            operator_label = str(request.metadata.get("worker_project_label") or WORKER_PROJECT_LABEL)
            task_profile = infer_task_profile(request.goal, request.metadata)
            metadata_json = {
                **request.metadata,
                "repo_url": request.repo_url,
                "issue_number": request.issue_number,
                "enable_web_research": request.enable_web_research,
                "allow_repository_modifications": request.allow_repository_modifications,
                "current_approval_gate_name": None,
                "source_local_repo_path": source_local_repo_path,
                "task_workspace_path": str(task_workspace_path),
                "workspace_strategy": "task_isolated_checkout",
                "worker_progress": {},
                "worker_project_label": operator_label,
                "auto_deploy_staging": (
                    request.auto_deploy_staging if request.auto_deploy_staging is not None else True
                ),
                "test_commands": request.test_commands,
                "lint_commands": request.lint_commands,
                "typing_commands": request.typing_commands,
            }
            if task_profile and not metadata_json.get("task_profile"):
                metadata_json["task_profile"] = task_profile
            record = TaskRecord(
                id=task_id,
                goal=request.goal,
                repository=request.repository,
                repo_url=request.repo_url,
                local_repo_path=str(task_workspace_path),
                base_branch=request.base_branch,
                branch_name=branch_name,
                status=TaskStatus.NEW.value,
                enable_web_research=request.enable_web_research,
                auto_deploy_staging=(
                    request.auto_deploy_staging if request.auto_deploy_staging is not None else True
                ),
                issue_number=request.issue_number,
                metadata_json=metadata_json,
                smoke_checks_json=[item.model_dump() for item in request.smoke_checks],
                deployment_json=request.deployment.model_dump() if request.deployment else None,
            )
            session.add(record)
            session.commit()
            session.refresh(record)

            self._add_event(
                session,
                task_id=record.id,
                stage=TaskStatus.NEW.value,
                message="Aufgabe wurde angelegt und wartet auf den Start des Workflows.",
                details={
                    "repository": record.repository,
                    "branch_name": record.branch_name,
                    "source_local_repo_path": source_local_repo_path,
                    "task_workspace_path": str(task_workspace_path),
                    "workspace_strategy": "task_isolated_checkout",
                    "task_profile": metadata_json.get("task_profile"),
                },
            )
            self._add_snapshot(session, record.id, TaskStatus.NEW.value, {"created": True})
            session.commit()
            return self._to_summary(record)

    def list_tasks(
        self,
        *,
        include_archived: bool = False,
        only_archived: bool = False,
    ) -> list[TaskSummary]:
        """Return newest tasks first and hide archived tasks by default for a cleaner operator view."""

        with self.session() as session:
            records = session.query(TaskRecord).order_by(TaskRecord.created_at.desc()).all()
            summaries = [self._to_summary(record) for record in records]
            if only_archived:
                return [item for item in summaries if item.archived]
            if include_archived:
                return summaries
            return [item for item in summaries if not item.archived]

    def get_task(self, task_id: str) -> TaskDetail:
        """Return a full task detail view with events and approvals."""

        with self.session() as session:
            record = session.get(TaskRecord, task_id)
            if record is None:
                raise KeyError(f"Task {task_id} was not found.")
            events = [
                TaskEventResponse(
                    id=item.id,
                    task_id=item.task_id,
                    level=item.level,
                    stage=item.stage,
                    message=item.message,
                    details=item.details_json,
                    created_at=item.created_at,
                )
                for item in sorted(record.events, key=lambda entry: entry.created_at)
            ]
            approvals = [
                ApprovalResponse(
                    gate_name=item.gate_name,
                    decision=ApprovalDecision(item.decision),
                    reason=item.reason,
                    actor=item.actor,
                    created_at=item.created_at,
                )
                for item in sorted(record.approvals, key=lambda entry: entry.created_at)
            ]
            return TaskDetail(
                **self._to_summary(record).model_dump(),
                worker_results=record.worker_results_json or {},
                risk_flags=record.risk_flags_json or [],
                events=events,
                approvals=approvals,
                smoke_checks=record.smoke_checks_json or [],
                deployment=(
                    DeploymentConfig.model_validate(record.deployment_json)
                    if record.deployment_json
                    else None
                ),
            )

    def archive_task(self, task_id: str, request: TaskArchiveRequest) -> TaskDetail:
        """Hide one finished task from default listings and benchmarks without deleting any audit data."""

        with self.session() as session:
            record = self._require_task(session, task_id)
            if self._is_archived(record):
                return self.get_task(task_id)
            if record.status not in ARCHIVABLE_TASK_STATUSES or record.approval_required:
                raise ValueError(
                    "Nur abgeschlossene, fehlgeschlagene oder noch nicht gestartete Aufgaben koennen archiviert werden."
                )

            archived_at = datetime.now(UTC)
            metadata = dict(record.metadata_json or {})
            metadata.update(
                {
                    "archived": True,
                    "archived_at": archived_at.isoformat(),
                    "archived_by": request.actor,
                    "archived_reason": request.reason,
                }
            )
            record.metadata_json = metadata
            record.updated_at = archived_at
            self._add_event(
                session,
                task_id,
                "ARCHIVE",
                "Aufgabe wurde archiviert und aus den Standardansichten ausgeblendet.",
                {
                    "event_kind": "task_archived",
                    "actor": request.actor,
                    "reason": request.reason,
                },
            )
            self._add_snapshot(
                session,
                task_id,
                record.status,
                {
                    "archive": {
                        "archived": True,
                        "actor": request.actor,
                        "reason": request.reason,
                        "archived_at": archived_at.isoformat(),
                    }
                },
            )
            session.commit()
            session.refresh(record)
            return self.get_task(task_id)

    def restore_task(self, task_id: str, request: TaskArchiveRequest) -> TaskDetail:
        """Bring one archived task back into default lists without changing its workflow result."""

        with self.session() as session:
            record = self._require_task(session, task_id)
            if not self._is_archived(record):
                return self.get_task(task_id)

            restored_at = datetime.now(UTC)
            metadata = dict(record.metadata_json or {})
            for key in ARCHIVE_METADATA_KEYS:
                metadata.pop(key, None)
            record.metadata_json = metadata
            record.updated_at = restored_at
            self._add_event(
                session,
                task_id,
                "ARCHIVE",
                "Aufgabe wurde aus dem Archiv wiederhergestellt.",
                {
                    "event_kind": "task_restored",
                    "actor": request.actor,
                    "reason": request.reason,
                },
            )
            self._add_snapshot(
                session,
                task_id,
                record.status,
                {
                    "archive": {
                        "archived": False,
                        "actor": request.actor,
                        "reason": request.reason,
                        "restored_at": restored_at.isoformat(),
                    }
                },
            )
            session.commit()
            session.refresh(record)
            return self.get_task(task_id)

    def update_status(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        message: str,
        details: dict | None = None,
        resume_target: str | None = None,
        latest_error: str | None = None,
    ) -> TaskDetail:
        """Update task status, log an event, and store a checkpoint snapshot."""

        with self.session() as session:
            record = self._require_task(session, task_id)
            record.status = status.value
            record.resume_target = resume_target
            record.latest_error = latest_error
            self._add_event(session, task_id, status.value, message, details or {})
            self._add_snapshot(
                session,
                task_id,
                status.value,
                {
                    "resume_target": resume_target,
                    "latest_error": latest_error,
                    "details": details or {},
                },
            )
            session.commit()
            session.refresh(record)
            return self.get_task(task_id)

    def store_worker_result(
        self,
        task_id: str,
        worker_name: str,
        result: WorkerResponse,
    ) -> TaskDetail:
        """Persist worker output, warnings, errors, and newly discovered risk flags."""

        with self.session() as session:
            record = self._require_task(session, task_id)
            worker_results = dict(record.worker_results_json or {})
            worker_results[worker_name] = result.model_dump()
            record.worker_results_json = worker_results

            merged_flags = set(record.risk_flags_json or [])
            merged_flags.update(result.risk_flags)
            record.risk_flags_json = sorted(merged_flags)

            if result.errors:
                record.latest_error = "; ".join(result.errors)

            self._add_event(
                session,
                task_id,
                worker_name.upper(),
                result.summary,
                {
                    "warnings": result.warnings,
                    "errors": result.errors,
                    "risk_flags": result.risk_flags,
                    "artifacts": [artifact.model_dump() for artifact in result.artifacts],
                },
            )
            self._add_snapshot(
                session,
                task_id,
                record.status,
                {"worker_name": worker_name, "result": result.model_dump()},
            )
            session.commit()
            session.refresh(record)
            return self.get_task(task_id)

    def set_approval_required(
        self,
        task_id: str,
        reason: str,
        resume_target: str,
        gate_name: str = "risk-review",
    ) -> TaskDetail:
        """Pause the workflow until a human explicitly approves or rejects the gate."""

        with self.session() as session:
            record = self._require_task(session, task_id)
            record.status = TaskStatus.APPROVAL_REQUIRED.value
            record.approval_required = True
            record.approval_reason = reason
            record.resume_target = resume_target
            metadata = dict(record.metadata_json or {})
            metadata["current_approval_gate_name"] = gate_name
            record.metadata_json = metadata
            self._add_event(
                session,
                task_id,
                TaskStatus.APPROVAL_REQUIRED.value,
                "Human approval is required before the workflow can continue.",
                {"reason": reason, "resume_target": resume_target, "gate_name": gate_name},
            )
            self._add_snapshot(
                session,
                task_id,
                TaskStatus.APPROVAL_REQUIRED.value,
                {"reason": reason, "resume_target": resume_target, "gate_name": gate_name},
            )
            session.commit()
            session.refresh(record)
            return self.get_task(task_id)

    def record_approval(self, task_id: str, request: ApprovalRequest) -> TaskDetail:
        """Record an operator decision and reopen or fail the task accordingly."""

        with self.session() as session:
            record = self._require_task(session, task_id)
            approval = ApprovalRecord(
                task_id=task_id,
                gate_name=request.gate_name,
                decision=request.decision.value,
                actor=request.actor,
                reason=request.reason,
            )
            session.add(approval)

            if request.decision is ApprovalDecision.APPROVE:
                record.approval_required = False
                record.approval_reason = None
                metadata = dict(record.metadata_json or {})
                metadata["current_approval_gate_name"] = None
                if request.gate_name == "repository-modification":
                    metadata["allow_repository_modifications"] = True
                    metadata["repository_modification_approved_by"] = request.actor
                record.metadata_json = metadata
                stage_message = f"Approval `{request.gate_name}` granted by {request.actor}."
            else:
                record.status = TaskStatus.FAILED.value
                record.latest_error = request.reason or "Human operator rejected the approval gate."
                metadata = dict(record.metadata_json or {})
                metadata["current_approval_gate_name"] = None
                record.metadata_json = metadata
                stage_message = f"Approval `{request.gate_name}` rejected by {request.actor}."

            self._add_event(
                session,
                task_id,
                "APPROVAL",
                stage_message,
                {"decision": request.decision.value, "reason": request.reason},
            )
            self._add_snapshot(
                session,
                task_id,
                record.status,
                {"approval": request.model_dump(), "updated_at": datetime.now(UTC).isoformat()},
            )
            session.commit()
            session.refresh(record)
            return self.get_task(task_id)

    def set_pull_request(self, task_id: str, pull_request_url: str) -> TaskDetail:
        """Persist the draft pull request URL after GitHub creation succeeds."""

        with self.session() as session:
            record = self._require_task(session, task_id)
            record.pull_request_url = pull_request_url
            session.commit()
            session.refresh(record)
            return self.get_task(task_id)

    def append_event(
        self,
        task_id: str,
        *,
        stage: str,
        message: str,
        details: dict[str, Any] | None = None,
        level: str = "INFO",
    ) -> TaskDetail:
        """Persist an event without changing the task status so long-running stages stay visible in the UI."""

        with self.session() as session:
            self._require_task(session, task_id)
            self._add_event(session, task_id, stage, message, details or {}, level=level)
            session.commit()
        return self.get_task(task_id)

    def update_runtime_context(
        self,
        task_id: str,
        *,
        local_repo_path: str | None = None,
        metadata_updates: dict[str, Any] | None = None,
    ) -> TaskDetail:
        """Persist runtime-only context such as the isolated workspace path without changing the visible task status."""

        with self.session() as session:
            record = self._require_task(session, task_id)
            if local_repo_path:
                record.local_repo_path = local_repo_path
            if metadata_updates:
                metadata = dict(record.metadata_json or {})
                metadata.update(metadata_updates)
                record.metadata_json = metadata
                record.updated_at = datetime.now(UTC)
            session.commit()
            session.refresh(record)
            return self.get_task(task_id)

    def restart_from_worker(
        self,
        task_id: str,
        request: TaskStageRestartRequest,
    ) -> TaskDetail:
        """Restart one workflow segment in-place without forcing the operator to create a brand-new task."""

        selected_worker = request.worker_name
        selected_worker_name = selected_worker.value
        selected_index = WORKFLOW_WORKER_INDEX[selected_worker_name]
        selected_status = WORKFLOW_WORKER_TO_STATUS[selected_worker]
        resume_target = WORKFLOW_WORKER_TO_RESUME_TARGET[selected_worker]
        selected_label = WORKFLOW_WORKER_LABELS.get(selected_worker_name, selected_worker_name)
        github_index = WORKFLOW_WORKER_INDEX[WorkflowWorkerName.GITHUB.value]

        with self.session() as session:
            record = self._require_task(session, task_id)
            metadata = dict(record.metadata_json or {})
            worker_progress = dict(metadata.get("worker_progress") or {})
            worker_results = dict(record.worker_results_json or {})

            cleared_workers: list[str] = []
            for worker in WORKFLOW_WORKER_ORDER:
                worker_name = worker.value
                if WORKFLOW_WORKER_INDEX[worker_name] < selected_index:
                    continue
                if worker_name in worker_progress:
                    worker_progress.pop(worker_name, None)
                    cleared_workers.append(worker_name)
                if worker_name in worker_results:
                    worker_results.pop(worker_name, None)
                    if worker_name not in cleared_workers:
                        cleared_workers.append(worker_name)

            metadata["worker_progress"] = worker_progress
            metadata["current_approval_gate_name"] = None
            metadata["last_restart_request"] = {
                "worker_name": selected_worker_name,
                "worker_label": selected_label,
                "reason": request.reason,
                "actor": request.actor,
                "requested_at": datetime.now(UTC).isoformat(),
                "cleared_workers": cleared_workers,
            }

            record.metadata_json = metadata
            record.worker_results_json = worker_results
            record.risk_flags_json = self._collect_risk_flags(worker_results)
            record.status = selected_status.value
            record.resume_target = resume_target
            record.approval_required = False
            record.approval_reason = None
            record.latest_error = None
            if selected_index <= github_index:
                record.pull_request_url = None

            self._add_event(
                session,
                task_id,
                selected_status.value,
                f"{selected_label} wird ab diesem Schritt neu gestartet.",
                {
                    "event_kind": "stage_restart_requested",
                    "worker_name": selected_worker_name,
                    "state": "waiting",
                    "current_step": selected_label,
                    "current_action": "Teilbereich wird fuer einen erneuten Lauf vorbereitet.",
                    "current_instruction": (
                        f"{selected_label} wurde fuer einen gezielten Neustart markiert. "
                        "Die betroffene Stage und alle spaeteren Ergebnisse werden frisch neu aufgebaut."
                    ),
                    "current_prompt_summary": (
                        f"{selected_label} wird erneut gestartet, damit der Workflow nicht komplett neu begonnen werden muss."
                    ),
                    "progress_message": f"{selected_label} wurde fuer einen Teil-Neustart eingeplant.",
                    "waiting_for": "Neustart des Worker-Laufs",
                    "last_result_summary": (
                        f"Vorbereitung fuer Neustart abgeschlossen. "
                        f"Zurueckgesetzt: {', '.join(cleared_workers) if cleared_workers else 'keine gespeicherten Ergebnisse'}."
                    ),
                    "started_at": datetime.now(UTC).isoformat(),
                    "updated_at": datetime.now(UTC).isoformat(),
                    "elapsed_seconds": 0.0,
                    "restart_reason": request.reason,
                    "restart_actor": request.actor,
                    "restart_resume_target": resume_target,
                    "cleared_workers": cleared_workers,
                },
            )
            self._add_snapshot(
                session,
                task_id,
                selected_status.value,
                {
                    "restart": request.model_dump(),
                    "resume_target": resume_target,
                    "cleared_workers": cleared_workers,
                    "pull_request_cleared": selected_index <= github_index,
                },
            )
            session.commit()
            session.refresh(record)
            return self.get_task(task_id)

    def _require_task(self, session: Session, task_id: str) -> TaskRecord:
        record = session.get(TaskRecord, task_id)
        if record is None:
            raise KeyError(f"Task {task_id} was not found.")
        return record

    def _collect_risk_flags(self, worker_results: dict[str, Any]) -> list[str]:
        """Rebuild risk flags from the still-valid worker results after a partial restart."""

        merged_flags: set[str] = set()
        for result in worker_results.values():
            if not isinstance(result, dict):
                continue
            for item in result.get("risk_flags", []):
                merged_flags.add(str(item))
        return sorted(merged_flags)

    def _add_event(
        self,
        session: Session,
        task_id: str,
        stage: str,
        message: str,
        details: dict,
        level: str = "INFO",
    ) -> None:
        record = self._require_task(session, task_id)
        record.updated_at = datetime.now(UTC)
        self._merge_worker_progress(record, details)
        session.add(
            TaskEventRecord(
                task_id=task_id,
                level=level,
                stage=stage,
                message=message,
                details_json=details,
            )
        )

    def _add_snapshot(self, session: Session, task_id: str, status: str, state: dict) -> None:
        session.add(TaskSnapshotRecord(task_id=task_id, status=status, state_json=state))

    def _merge_worker_progress(self, record: TaskRecord, details: dict[str, Any]) -> None:
        """Keep the latest structured per-worker progress in task metadata for compact UI rendering."""

        worker_name = str(details.get("worker_name") or "").strip()
        if not worker_name:
            return

        progress_keys = {
            "state",
            "current_action",
            "current_step",
            "current_prompt_summary",
            "current_instruction",
            "waiting_for",
            "blocked_by",
            "next_worker",
            "last_result_summary",
            "progress_message",
            "started_at",
            "updated_at",
            "elapsed_seconds",
            "slow_warning_seconds",
            "event_kind",
            "stage_label",
            "stage_description",
            "service_url",
            "model_route",
            "last_error",
            "last_event_message",
        }
        progress_update = {key: details[key] for key in progress_keys if key in details and details[key] is not None}
        if not progress_update:
            return

        metadata = dict(record.metadata_json or {})
        worker_progress = dict(metadata.get("worker_progress") or {})
        existing = dict(worker_progress.get(worker_name) or {})
        existing.update(progress_update)
        worker_progress[worker_name] = existing
        metadata["worker_progress"] = worker_progress
        record.metadata_json = metadata

    def _is_archived(self, record: TaskRecord) -> bool:
        """Read the durable archive flag from metadata without assuming fresh schema migrations."""

        return bool((record.metadata_json or {}).get("archived"))

    def _to_summary(self, record: TaskRecord) -> TaskSummary:
        metadata = record.metadata_json or {}
        return TaskSummary(
            id=record.id,
            goal=record.goal,
            repository=record.repository,
            repo_url=record.repo_url,
            local_repo_path=record.local_repo_path,
            base_branch=record.base_branch,
            branch_name=record.branch_name,
            status=TaskStatus(record.status),
            resume_target=record.resume_target,
            current_approval_gate_name=(record.metadata_json or {}).get("current_approval_gate_name"),
            approval_required=record.approval_required,
            approval_reason=record.approval_reason,
            allow_repository_modifications=metadata.get("allow_repository_modifications", False),
            pull_request_url=record.pull_request_url,
            latest_error=record.latest_error,
            archived=bool(metadata.get("archived")),
            archived_at=self._parse_metadata_timestamp(metadata.get("archived_at")),
            archived_by=metadata.get("archived_by"),
            archived_reason=metadata.get("archived_reason"),
            metadata=metadata,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    @staticmethod
    def _parse_metadata_timestamp(value: Any) -> datetime | None:
        """Normalize archived timestamps from JSON metadata into timezone-aware UTC datetimes."""

        if isinstance(value, datetime):
            return value.replace(tzinfo=UTC) if value.tzinfo is None else value
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
