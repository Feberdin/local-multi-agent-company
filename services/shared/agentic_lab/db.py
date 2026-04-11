"""
Purpose: SQLAlchemy persistence for tasks, events, approvals, and workflow checkpoints.
Input/Output: The orchestrator stores and retrieves task state from SQLite through these models and helpers.
Important invariants: Task state changes are append-logged through events and snapshots so workflow progress remains auditable.
How to debug: If state seems inconsistent, inspect the `task_snapshots` and `task_events` tables first.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from services.shared.agentic_lab.config import get_settings


class Base(DeclarativeBase):
    """Shared SQLAlchemy declarative base."""


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp for audit fields."""

    return datetime.now(UTC)


class TaskRecord(Base):
    """Main persistent task row for orchestrated work."""

    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    repository: Mapped[str] = mapped_column(String(255), nullable=False)
    repo_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    local_repo_path: Mapped[str] = mapped_column(String(500), nullable=False)
    base_branch: Mapped[str] = mapped_column(String(120), nullable=False, default="main")
    branch_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="NEW")
    resume_target: Mapped[str | None] = mapped_column(String(50), nullable=True)
    approval_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    approval_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    pull_request_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    latest_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    enable_web_research: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    auto_deploy_staging: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    issue_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    worker_results_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    risk_flags_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    smoke_checks_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    deployment_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    events: Mapped[list[TaskEventRecord]] = relationship(back_populates="task")
    approvals: Mapped[list[ApprovalRecord]] = relationship(back_populates="task")
    snapshots: Mapped[list[TaskSnapshotRecord]] = relationship(back_populates="task")


class TaskEventRecord(Base):
    """Append-only event log for task progress and operator visibility."""

    __tablename__ = "task_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), nullable=False, index=True)
    level: Mapped[str] = mapped_column(String(20), nullable=False, default="INFO")
    stage: Mapped[str] = mapped_column(String(50), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    task: Mapped[TaskRecord] = relationship(back_populates="events")


class ApprovalRecord(Base):
    """Decision log for human approval gates."""

    __tablename__ = "approvals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), nullable=False, index=True)
    gate_name: Mapped[str] = mapped_column(String(100), nullable=False)
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    actor: Mapped[str] = mapped_column(String(120), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    task: Mapped[TaskRecord] = relationship(back_populates="approvals")


class TaskSnapshotRecord(Base):
    """Checkpoint snapshots for step-by-step workflow recovery."""

    __tablename__ = "task_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    state_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    task: Mapped[TaskRecord] = relationship(back_populates="snapshots")


class SelfImprovementCycleRecord(Base):
    """Tracks each controlled self-improvement cycle from analysis through deployment."""

    __tablename__ = "self_improvement_cycles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    cycle_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="idle")
    trigger: Mapped[str] = mapped_column(String(50), nullable=False, default="manual")
    problem_hypothesis: Mapped[str | None] = mapped_column(Text, nullable=True)
    problem_class: Mapped[str | None] = mapped_column(String(80), nullable=True)
    risk_level: Mapped[str] = mapped_column(String(20), nullable=False, default="low")
    is_risky: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    risk_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    goal: Mapped[str | None] = mapped_column(Text, nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    branch_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    commit_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    changed_files_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    test_results_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    deploy_result_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    healthcheck_result_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    latest_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


_engine = None
_session_factory = None


def get_engine():
    """Create the SQLAlchemy engine lazily so tests can inject alternate settings first."""

    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})
    return _engine


def get_session_factory():
    """Return a lazy session factory bound to the active engine."""

    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _session_factory


def configure_database(database_url: str):
    """Override the engine and session factory, primarily for tests."""

    global _engine, _session_factory
    _engine = create_engine(database_url, connect_args={"check_same_thread": False})
    _session_factory = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def init_db() -> None:
    """Create database tables if they are missing."""

    Base.metadata.create_all(bind=get_engine())
