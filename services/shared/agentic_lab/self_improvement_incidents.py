"""
Purpose: Persistent incident tracking for failed self-improvement cycles and rollback preparation.
Input/Output: The self-improvement service records incidents here whenever a cycle fails or
              a rollback is scheduled, so the UI can show a durable operator-facing history.
Important invariants:
  - Incidents are append-safe and persisted outside volatile in-memory metadata.
  - Rollback preparation is tracked explicitly even when execution happens in a separate task.
  - The incident registry is an audit layer, not a second source of truth for repository state.
How to debug: Compare the incident list with the linked cycle/task IDs and the rollback task ID.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from services.shared.agentic_lab.db import SelfImprovementIncidentRecord, get_session_factory


class IncidentStatus(StrEnum):
    OPEN = "open"
    ROLLBACK_PREPARED = "rollback_prepared"
    ROLLBACK_RUNNING = "rollback_running"
    ROLLED_BACK = "rolled_back"
    RESOLVED = "resolved"


class SelfImprovementIncidentResponse(BaseModel):
    id: str
    cycle_id: str | None = None
    task_id: str | None = None
    severity: str
    status: str
    summary: str
    failure_stage: str | None = None
    latest_error: str | None = None
    root_cause: str | None = None
    commit_sha: str | None = None
    branch_name: str | None = None
    rollback_task_id: str | None = None
    rollback_status: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, record: SelfImprovementIncidentRecord) -> SelfImprovementIncidentResponse:
        return cls(
            id=record.id,
            cycle_id=record.cycle_id,
            task_id=record.task_id,
            severity=record.severity,
            status=record.status,
            summary=record.summary,
            failure_stage=record.failure_stage,
            latest_error=record.latest_error,
            root_cause=record.root_cause,
            commit_sha=record.commit_sha,
            branch_name=record.branch_name,
            rollback_task_id=record.rollback_task_id,
            rollback_status=record.rollback_status,
            metadata=record.metadata_json or {},
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


class SelfImprovementIncidentService:
    """Small persistence wrapper around the incident audit table."""

    def __init__(self, session_factory=None) -> None:
        self.session_factory = session_factory or get_session_factory()

    def _session(self) -> Session:
        return self.session_factory()

    def list_recent(self, limit: int = 20) -> list[SelfImprovementIncidentResponse]:
        with self._session() as session:
            records = (
                session.query(SelfImprovementIncidentRecord)
                .order_by(SelfImprovementIncidentRecord.created_at.desc())
                .limit(limit)
                .all()
            )
            return [SelfImprovementIncidentResponse.from_record(item) for item in records]

    def open_count(self) -> int:
        with self._session() as session:
            return (
                session.query(SelfImprovementIncidentRecord)
                .filter(
                    SelfImprovementIncidentRecord.status.in_(
                        [
                            IncidentStatus.OPEN.value,
                            IncidentStatus.ROLLBACK_PREPARED.value,
                            IncidentStatus.ROLLBACK_RUNNING.value,
                        ]
                    )
                )
                .count()
            )

    def create_incident(
        self,
        *,
        cycle_id: str | None,
        task_id: str | None,
        severity: str,
        summary: str,
        failure_stage: str | None,
        latest_error: str | None,
        root_cause: str | None,
        commit_sha: str | None,
        branch_name: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> SelfImprovementIncidentResponse:
        record = SelfImprovementIncidentRecord(
            id=str(uuid4()),
            cycle_id=cycle_id,
            task_id=task_id,
            severity=severity,
            status=IncidentStatus.OPEN.value,
            summary=summary,
            failure_stage=failure_stage,
            latest_error=latest_error,
            root_cause=root_cause,
            commit_sha=commit_sha,
            branch_name=branch_name,
            metadata_json=metadata or {},
        )
        with self._session() as session:
            session.add(record)
            session.commit()
            session.refresh(record)
            return SelfImprovementIncidentResponse.from_record(record)

    def attach_rollback_task(
        self,
        incident_id: str,
        *,
        rollback_task_id: str,
        rollback_status: str,
    ) -> SelfImprovementIncidentResponse:
        with self._session() as session:
            record = session.get(SelfImprovementIncidentRecord, incident_id)
            if record is None:
                raise KeyError(f"Incident {incident_id} not found.")
            record.rollback_task_id = rollback_task_id
            record.rollback_status = rollback_status
            if rollback_status == IncidentStatus.ROLLBACK_RUNNING.value:
                record.status = IncidentStatus.ROLLBACK_RUNNING.value
            elif rollback_status == IncidentStatus.ROLLBACK_PREPARED.value:
                record.status = IncidentStatus.ROLLBACK_PREPARED.value
            record.updated_at = datetime.now(UTC)
            session.commit()
            session.refresh(record)
            return SelfImprovementIncidentResponse.from_record(record)

    def update_rollback_status(
        self,
        incident_id: str,
        *,
        rollback_status: str,
        latest_error: str | None = None,
        metadata_updates: dict[str, Any] | None = None,
    ) -> SelfImprovementIncidentResponse:
        """Persist a later rollback outcome for incidents that were already created earlier."""

        with self._session() as session:
            record = session.get(SelfImprovementIncidentRecord, incident_id)
            if record is None:
                raise KeyError(f"Incident {incident_id} not found.")
            record.rollback_status = rollback_status
            if rollback_status == IncidentStatus.ROLLED_BACK.value:
                record.status = IncidentStatus.ROLLED_BACK.value
            elif rollback_status == IncidentStatus.ROLLBACK_RUNNING.value:
                record.status = IncidentStatus.ROLLBACK_RUNNING.value
            elif rollback_status == IncidentStatus.ROLLBACK_PREPARED.value:
                record.status = IncidentStatus.ROLLBACK_PREPARED.value
            if latest_error is not None:
                record.latest_error = latest_error
            if metadata_updates:
                merged = dict(record.metadata_json or {})
                merged.update(metadata_updates)
                record.metadata_json = merged
            record.updated_at = datetime.now(UTC)
            session.commit()
            session.refresh(record)
            return SelfImprovementIncidentResponse.from_record(record)
