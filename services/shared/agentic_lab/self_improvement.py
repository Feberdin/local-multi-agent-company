"""
Purpose: Controlled self-improvement service that uses the existing task workflow to analyze,
         fix, validate, and optionally deploy improvements to the system's own repository.
Input/Output: Operators start cycles via API; the service creates tasks against the own repo
              and monitors them through to completion. All state is persisted in the DB.
Important invariants:
  - Never commits directly to main; always works via the task workflow which uses branches.
  - At most one active cycle at a time.
  - Daily cycle count is enforced to prevent runaway automation.
  - Risky changes (auth, secrets, deploy paths, self-modification) require manual approval.
  - On orchestrator restart, in-flight cycles are marked failed (fail-safe, not resume).
How to debug: Check `self_improvement_cycles` table and the linked task in `tasks`.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Coroutine
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel
from sqlalchemy.orm import Session

from services.shared.agentic_lab.config import Settings, get_settings
from services.shared.agentic_lab.db import (
    SelfImprovementCycleRecord,
    SelfImprovementSessionRecord,
    get_session_factory,
)
from services.shared.agentic_lab.llm import LLMClient, LLMError
from services.shared.agentic_lab.schemas import (
    ApprovalDecision,
    ApprovalRequest,
    TaskCreateRequest,
    TaskStatus,
)
from services.shared.agentic_lab.self_improvement_email import SelfImprovementEmailService
from services.shared.agentic_lab.self_improvement_governance import (
    ApprovalEmailIntent,
    GovernanceDecision,
    GovernanceStatus,
    SelfImprovementGovernancePolicy,
    SelfImprovementGovernanceService,
    normalize_self_improvement_mode,
)
from services.shared.agentic_lab.self_improvement_incidents import (
    IncidentStatus,
    SelfImprovementIncidentResponse,
    SelfImprovementIncidentService,
)
from services.shared.agentic_lab.self_update_watchdog import (
    SelfUpdateWatchdogState,
    SelfUpdateWatchdogStatus,
    read_watchdog_state,
)
from services.shared.agentic_lab.task_service import TaskService

TaskRunner = Callable[[str], Coroutine[Any, Any, None]]

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class CycleStatus(StrEnum):
    IDLE = "idle"
    ANALYZING = "analyzing"
    PLANNING = "planning"
    IMPLEMENTING = "implementing"
    VALIDATING = "validating"
    DEPLOYING = "deploying"
    POST_DEPLOY_TESTING = "post_deploy_testing"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"
    AWAITING_MANUAL_REVIEW = "awaiting_manual_review"


class SessionStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"
    AWAITING_MANUAL_REVIEW = "awaiting_manual_review"


class ProblemClass(StrEnum):
    TIMEOUT = "timeout"
    UNREACHABLE_ENDPOINT = "unreachable_endpoint"
    GIT_ERROR = "git_error"
    MISSING_TOOL = "missing_tool"
    INVALID_RESPONSE_SCHEMA = "invalid_response_schema"
    RETRYABLE_WORKER_ERROR = "retryable_worker_error"
    UI_RENDERING_PROBLEM = "ui_rendering_problem"
    DEPLOYMENT_FAILURE = "deployment_failure"
    CODE_QUALITY = "code_quality"
    TEST_FAILURE = "test_failure"
    PERFORMANCE = "performance"
    UNKNOWN = "unknown"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


# ---------------------------------------------------------------------------
# Risk classification
# ---------------------------------------------------------------------------

# Ordered: first match wins. Entries with higher risk come first.
_RISKY_PATTERNS: list[tuple[re.Pattern[str], str, RiskLevel]] = [
    (
        re.compile(r"\bself.?improv\w*\b", re.I),
        "Aenderungen an der Self-Improvement-Logik selbst erfordern manuelle Freigabe.",
        RiskLevel.CRITICAL,
    ),
    (
        re.compile(r"\b(auth\w*|oauth|jwt|session)\b", re.I),
        "Beruehrt Authentifizierung oder Autorisierung.",
        RiskLevel.HIGH,
    ),
    (
        re.compile(r"\b(secret|token|password|credential|api.?key|\.env)\b", re.I),
        "Beruehrt Secrets oder Zugangsdaten.",
        RiskLevel.HIGH,
    ),
    (
        re.compile(r"\b(deploy\w*|rollout|pipeline|ci.?cd|github.?action|workflow\.py)\b", re.I),
        "Aenderung am Deployment-Pfad oder CI/CD.",
        RiskLevel.HIGH,
    ),
    (
        re.compile(r"\b(database|migration|schema|db\.py|alembic)\b", re.I),
        "Datenbankschema-Aenderung.",
        RiskLevel.HIGH,
    ),
    (
        re.compile(r"\b(docker|compose|dockerfile|container)\b", re.I),
        "Aenderung an Containerinfrastruktur.",
        RiskLevel.MEDIUM,
    ),
    (
        re.compile(r"\b(security|permission|chmod|sudo|root)\b", re.I),
        "Sicherheitsrelevante Aenderung.",
        RiskLevel.HIGH,
    ),
]


def classify_risk(goal: str) -> tuple[RiskLevel, str | None]:
    """Return the risk level and human-readable reason for a proposed goal string."""
    for pattern, reason, level in _RISKY_PATTERNS:
        if pattern.search(goal):
            return level, reason
    return RiskLevel.LOW, None


# ---------------------------------------------------------------------------
# Error / problem classification
# ---------------------------------------------------------------------------

_ERROR_KEYWORDS: list[tuple[str, ProblemClass]] = [
    ("timeout", ProblemClass.TIMEOUT),
    ("timed out", ProblemClass.TIMEOUT),
    ("deadline", ProblemClass.TIMEOUT),
    ("exceeded", ProblemClass.TIMEOUT),
    ("connection refused", ProblemClass.UNREACHABLE_ENDPOINT),
    ("unreachable", ProblemClass.UNREACHABLE_ENDPOINT),
    ("connect error", ProblemClass.UNREACHABLE_ENDPOINT),
    ("no route to host", ProblemClass.UNREACHABLE_ENDPOINT),
    ("safe.directory", ProblemClass.GIT_ERROR),
    ("not a git repository", ProblemClass.GIT_ERROR),
    ("git", ProblemClass.GIT_ERROR),
    ("command not found", ProblemClass.MISSING_TOOL),
    ("no such file or directory", ProblemClass.MISSING_TOOL),
    ("valid json", ProblemClass.INVALID_RESPONSE_SCHEMA),
    ("validationerror", ProblemClass.INVALID_RESPONSE_SCHEMA),
    ("string_too_long", ProblemClass.INVALID_RESPONSE_SCHEMA),
    ("response shape", ProblemClass.INVALID_RESPONSE_SCHEMA),
    ("render", ProblemClass.UI_RENDERING_PROBLEM),
    ("template", ProblemClass.UI_RENDERING_PROBLEM),
    ("jinja", ProblemClass.UI_RENDERING_PROBLEM),
    ("deploy", ProblemClass.DEPLOYMENT_FAILURE),
    ("healthcheck", ProblemClass.DEPLOYMENT_FAILURE),
    ("compose", ProblemClass.DEPLOYMENT_FAILURE),
    ("test failed", ProblemClass.TEST_FAILURE),
    ("assertion", ProblemClass.TEST_FAILURE),
    ("slow", ProblemClass.PERFORMANCE),
    ("latency", ProblemClass.PERFORMANCE),
]


def classify_error_text(error_text: str) -> ProblemClass:
    """Map a free-form error string to the most likely ProblemClass."""
    lower = error_text.lower()
    for keyword, cls in _ERROR_KEYWORDS:
        if keyword in lower:
            return cls
    return ProblemClass.UNKNOWN


# ---------------------------------------------------------------------------
# Problem analysis (LLM-assisted)
# ---------------------------------------------------------------------------


async def analyze_problems(
    task_service: TaskService,
    llm_client: LLMClient,
    *,
    problem_hint: str | None = None,
    max_failed_tasks: int = 10,
) -> tuple[str, ProblemClass, str]:
    """
    Scan recent failed tasks, classify the dominant problem, and generate
    exactly one concrete, actionable coding goal using the LLM.

    Returns (goal, problem_class, problem_hypothesis).
    Falls back gracefully if the LLM is unavailable.
    """
    tasks = task_service.list_tasks()
    failed = [t for t in tasks if t.status == TaskStatus.FAILED][:max_failed_tasks]

    snippets: list[str] = []
    dominant_class = ProblemClass.UNKNOWN
    for t in failed:
        if t.latest_error:
            cls = classify_error_text(t.latest_error)
            if dominant_class == ProblemClass.UNKNOWN:
                dominant_class = cls
            snippets.append(f"- Ziel: {t.goal[:100]!r}\n  Fehler: {t.latest_error[:200]}")

    evidence = "\n".join(snippets[:8]) if snippets else "Keine aktuellen Fehlermeldungen vorhanden."
    if problem_hint:
        evidence = f"Operator-Hinweis: {problem_hint}\n\n{evidence}"

    system_prompt = (
        "Du bist ein Analyse-Agent fuer ein lokales Multi-Agenten-System (Feberdin/local-multi-agent-company). "
        "Deine Aufgabe: Analysiere die Fehler-Snippets und generiere genau EINE konkrete, "
        "minimal-invasive Coding-Aufgabe.\n"
        "Regeln:\n"
        "- Die Aufgabe muss eine echte Datei-Aenderung beschreiben (kein Brainstorming).\n"
        "- Kleinstmoeglich: ein Bug, eine Robustheitsverbesserung, ein fehlender Fehler-Handler.\n"
        "- Max 400 Zeichen.\n"
        "- Beschreibe was geaendert werden soll, nicht warum.\n"
    )
    user_prompt = (
        f"Beobachtete Fehler (letzte fehlgeschlagene Tasks):\n{evidence}\n\n"
        "Antworte mit JSON:\n"
        '{"goal": "...", "problem_class": "timeout|unreachable_endpoint|git_error|'
        "missing_tool|invalid_response_schema|retryable_worker_error|ui_rendering_problem|"
        'deployment_failure|code_quality|test_failure|performance|unknown", '
        '"hypothesis": "..."}'
    )

    try:
        result = await llm_client.complete_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            worker_name="self_improvement",
            required_keys=["goal", "problem_class", "hypothesis"],
        )
        goal = str(result.get("goal", "")).strip()[:3990]
        raw_class = str(result.get("problem_class", "unknown")).lower().strip()
        hypothesis = str(result.get("hypothesis", "")).strip()[:300]
        try:
            problem_class = ProblemClass(raw_class)
        except ValueError:
            problem_class = dominant_class
    except (LLMError, Exception):
        # Fallback: construct a generic goal from the most common error class
        goal = _fallback_goal(problem_hint, dominant_class)
        problem_class = dominant_class
        hypothesis = "LLM-Analyse nicht verfuegbar. Generisches Ziel basierend auf Fehlermuster."

    if len(goal) < 10:
        goal = _fallback_goal(problem_hint, dominant_class)
        hypothesis = hypothesis or "Generisches Ziel verwendet."

    goal, hypothesis = _normalize_improvement_goal(goal, problem_class, hypothesis)
    return goal, problem_class, hypothesis


def _fallback_goal(problem_hint: str | None, cls: ProblemClass) -> str:
    if problem_hint:
        return f"Behebe folgendes Problem im Repository: {problem_hint[:300]}"
    fallbacks = {
        ProblemClass.TIMEOUT: (
            "Change WORKER_STAGE_TIMEOUT_SECONDS to 3600 in services/shared/agentic_lab/config.py "
            "and align the visible timeout examples in README.md and docs."
        ),
        ProblemClass.INVALID_RESPONSE_SCHEMA: (
            "Verbessere die JSON-Extraktion in complete_json() mit einem robusteren "
            "Fallback wenn das Modell Prose statt JSON zurueckgibt."
        ),
        ProblemClass.GIT_ERROR: (
            "Stelle sicher dass git-Workspace-Verzeichnisse korrekt als safe.directory "
            "konfiguriert sind und gib eine klare Fehlermeldung bei Git-Fehlern."
        ),
        ProblemClass.UNREACHABLE_ENDPOINT: (
            "Verbessere die Fehlerbehandlung bei nicht erreichbaren Worker-Endpunkten "
            "mit aussagekraeftiger Fehlermeldung und Retry-Hinweis."
        ),
    }
    return fallbacks.get(
        cls,
        "Analysiere die letzten Worker-Fehler im Repository und implementiere "
        "eine minimale Verbesserung der Fehlerbehandlung oder Robustheit.",
    )


def _normalize_improvement_goal(goal: str, problem_class: ProblemClass, hypothesis: str) -> tuple[str, str]:
    """Rewrite one known timeout hallucination into the real repo paths before it reaches the workflow."""

    normalized_goal = goal.strip()
    normalized_hypothesis = hypothesis.strip()
    if problem_class == ProblemClass.TIMEOUT and "WORKER_STAGE_TIMEOUT_SECONDS" in normalized_goal:
        if "worker.py" in normalized_goal.lower() or "worker.py" in normalized_hypothesis.lower():
            normalized_goal = (
                "Change WORKER_STAGE_TIMEOUT_SECONDS to 3600 in services/shared/agentic_lab/config.py "
                "and align the visible timeout examples in README.md and docs."
            )
            normalized_hypothesis = (
                "Der echte Worker-Stage-Timeout lebt in services/shared/agentic_lab/config.py; "
                "README und Docs enthalten zusaetzliche Beispielwerte."
            )
    return normalized_goal, normalized_hypothesis


# ---------------------------------------------------------------------------
# Pydantic response models
# ---------------------------------------------------------------------------


class SelfImprovementCycleResponse(BaseModel):
    id: str
    cycle_number: int
    status: str
    trigger: str
    problem_hypothesis: str | None
    problem_class: str | None
    risk_level: str
    is_risky: bool
    risk_reason: str | None
    goal: str | None
    task_id: str | None
    branch_name: str | None
    commit_sha: str | None
    changed_files: list[str]
    test_results: dict[str, Any]
    deploy_result: dict[str, Any]
    healthcheck_result: dict[str, Any]
    retry_count: int
    max_retries: int
    latest_error: str | None
    notes: str | None
    started_at: datetime
    completed_at: datetime | None
    metadata: dict[str, Any]
    governance_status: str = GovernanceStatus.PENDING.value
    governance_action: str | None = None
    approval_email_status: str | None = None
    approval_email_detail: str | None = None
    approval_email_outbox_path: str | None = None
    current_gate_name: str | None = None
    current_gate_reason: str | None = None
    incident_id: str | None = None
    rollback_task_id: str | None = None
    rollback_status: str | None = None

    @classmethod
    def from_record(cls, r: SelfImprovementCycleRecord) -> SelfImprovementCycleResponse:
        try:
            status = CycleStatus(r.status).value
        except ValueError:
            status = r.status
        metadata = r.metadata_json or {}
        return cls(
            id=r.id,
            cycle_number=r.cycle_number,
            status=status,
            trigger=r.trigger,
            problem_hypothesis=r.problem_hypothesis,
            problem_class=r.problem_class,
            risk_level=r.risk_level or RiskLevel.LOW.value,
            is_risky=r.is_risky,
            risk_reason=r.risk_reason,
            goal=r.goal,
            task_id=r.task_id,
            branch_name=r.branch_name,
            commit_sha=r.commit_sha,
            changed_files=r.changed_files_json or [],
            test_results=r.test_results_json or {},
            deploy_result=r.deploy_result_json or {},
            healthcheck_result=r.healthcheck_result_json or {},
            retry_count=r.retry_count,
            max_retries=r.max_retries,
            latest_error=r.latest_error,
            notes=r.notes,
            started_at=r.started_at,
            completed_at=r.completed_at,
            metadata=metadata,
            governance_status=str(metadata.get("governance_status") or GovernanceStatus.PENDING.value),
            governance_action=metadata.get("governance_action"),
            approval_email_status=metadata.get("approval_email_status"),
            approval_email_detail=metadata.get("approval_email_detail"),
            approval_email_outbox_path=metadata.get("approval_email_outbox_path"),
            current_gate_name=metadata.get("current_gate_name"),
            current_gate_reason=metadata.get("current_gate_reason"),
            incident_id=metadata.get("incident_id"),
            rollback_task_id=metadata.get("rollback_task_id"),
            rollback_status=metadata.get("rollback_status"),
        )


class SelfImprovementSessionResponse(BaseModel):
    id: str
    status: str
    trigger: str
    problem_hint: str | None
    current_cycle_id: str | None
    last_cycle_id: str | None
    cycles_started: int
    completed_cycles: int
    success_cycles: int
    failed_cycles: int
    max_cycles: int
    last_error: str | None
    stop_reason: str | None
    started_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    metadata: dict[str, Any]

    @classmethod
    def from_record(cls, r: SelfImprovementSessionRecord) -> SelfImprovementSessionResponse:
        try:
            status = SessionStatus(r.status).value
        except ValueError:
            status = r.status
        return cls(
            id=r.id,
            status=status,
            trigger=r.trigger,
            problem_hint=r.problem_hint,
            current_cycle_id=r.current_cycle_id,
            last_cycle_id=r.last_cycle_id,
            cycles_started=r.cycles_started,
            completed_cycles=r.completed_cycles,
            success_cycles=r.success_cycles,
            failed_cycles=r.failed_cycles,
            max_cycles=r.max_cycles,
            last_error=r.last_error,
            stop_reason=r.stop_reason,
            started_at=r.started_at,
            updated_at=r.updated_at,
            completed_at=r.completed_at,
            metadata=r.metadata_json or {},
        )


class SelfImprovementStatusResponse(BaseModel):
    mode: str
    enabled: bool
    active_cycle: SelfImprovementCycleResponse | None
    active_session: SelfImprovementSessionResponse | None
    pending_review_cycles: list[SelfImprovementCycleResponse]
    daily_cycle_count: int
    max_cycles_per_day: int
    daily_limit_reached: bool
    can_start: bool
    last_cycle: SelfImprovementCycleResponse | None
    open_incident_count: int


class StartCycleRequest(BaseModel):
    trigger: str = "manual"
    problem_hint: str | None = None
    force: bool = False


class StartSessionRequest(BaseModel):
    trigger: str = "overnight"
    problem_hint: str | None = None
    force: bool = False
    max_cycles: int = 3


class ApproveCycleRequest(BaseModel):
    actor: str = "human-operator"
    reason: str | None = None


class SelfImprovementConfigResponse(BaseModel):
    enabled: bool
    mode: str
    normalized_mode: str
    max_auto_fix_attempts: int
    max_cycles_per_day: int
    max_session_cycles: int
    deploy_after_success: bool
    require_approval_for_risky: bool
    preflight_required: bool
    auto_rollback: bool
    target_repo: str
    local_repo_path: str
    policy_path: str
    approval_email_enabled: bool
    approval_email_to: str | None = None
    github_auto_fix_enabled: bool
    github_auto_fix_poll_seconds: float
    github_auto_fix_max_attempts: int


class SelfImprovementPolicyResponse(BaseModel):
    repository: str
    local_repo_path: str
    docs_root: str
    ai_change_index: str
    approval_gate_name: str
    mode_rules: dict[str, dict[str, dict[str, Any]]]

    @classmethod
    def from_policy(cls, policy: SelfImprovementGovernancePolicy) -> SelfImprovementPolicyResponse:
        return cls(
            repository=policy.repository_scope.repository,
            local_repo_path=policy.repository_scope.local_repo_path,
            docs_root=policy.repository_scope.docs_root,
            ai_change_index=policy.repository_scope.ai_change_index,
            approval_gate_name=policy.repository_scope.approval_gate_name,
            mode_rules={
                mode: {
                    risk: rule.model_dump(mode="json")
                    for risk, rule in sorted(rules.items())
                }
                for mode, rules in sorted(policy.mode_rules.items())
            },
        )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class SelfImprovementError(RuntimeError):
    """Raised for controlled self-improvement constraint violations."""


class SelfImprovementService:
    """
    Orchestrates controlled self-improvement cycles.

    One cycle:
      1. Analyzes recent failed tasks to identify a problem.
      2. Creates a task against the system's own repository.
      3. Monitors task progress, enforces retry limits.
      4. Updates the cycle record at each step.

    Safety gates:
      - Only one active cycle at a time.
      - Only one unattended repair session at a time.
      - Daily cycle limit.
      - Risky goals require manual approval before task creation.
      - On restart, in-flight cycles are marked FAILED (no partial resume).
    """

    _TERMINAL = {
        CycleStatus.COMPLETED,
        CycleStatus.FAILED,
        CycleStatus.PAUSED,
    }
    _ACTIVE = {
        CycleStatus.ANALYZING,
        CycleStatus.PLANNING,
        CycleStatus.IMPLEMENTING,
        CycleStatus.VALIDATING,
        CycleStatus.DEPLOYING,
        CycleStatus.POST_DEPLOY_TESTING,
    }
    _SESSION_ACTIVE = {
        SessionStatus.RUNNING,
        SessionStatus.AWAITING_MANUAL_REVIEW,
    }

    def __init__(
        self,
        task_service: TaskService,
        llm_client: LLMClient,
        *,
        settings: Settings | None = None,
        session_factory=None,
    ) -> None:
        self.task_service = task_service
        self.llm_client = llm_client
        self.settings = settings or get_settings()
        self._session_factory = session_factory or get_session_factory()
        self.governance_service = SelfImprovementGovernanceService(self.settings)
        self.email_service = SelfImprovementEmailService(self.settings)
        self.incident_service = SelfImprovementIncidentService(self._session_factory)
        # Lazy asyncio.Lock — created on first async call (event loop already running then).
        self.__lock: asyncio.Lock | None = None
        # Tracks which cycle_ids have been operator-approved for risky execution.
        self._approved_cycle_ids: set[str] = set()

    @property
    def _lock(self) -> asyncio.Lock:
        if self.__lock is None:
            self.__lock = asyncio.Lock()
        return self.__lock

    @contextmanager
    def _session(self):
        session: Session = self._session_factory()
        try:
            yield session
        finally:
            session.close()

    # ── query helpers ─────────────────────────────────────────────────────────

    def daily_cycle_count(self) -> int:
        """Count cycles started today (UTC)."""
        today = datetime.now(UTC)
        day_start = today.replace(hour=0, minute=0, second=0, microsecond=0)
        with self._session() as session:
            return (
                session.query(SelfImprovementCycleRecord)
                .filter(SelfImprovementCycleRecord.started_at >= day_start)
                .count()
            )

    def is_daily_limit_reached(self) -> bool:
        limit = self.settings.self_improvement_max_cycles_per_day
        return limit > 0 and self.daily_cycle_count() >= limit

    def get_active_cycle(self) -> SelfImprovementCycleRecord | None:
        active_values = [s.value for s in self._ACTIVE]
        with self._session() as session:
            return (
                session.query(SelfImprovementCycleRecord)
                .filter(SelfImprovementCycleRecord.status.in_(active_values))
                .order_by(SelfImprovementCycleRecord.started_at.desc())
                .first()
            )

    def get_active_session(self) -> SelfImprovementSessionRecord | None:
        active_values = [s.value for s in self._SESSION_ACTIVE]
        with self._session() as session:
            return (
                session.query(SelfImprovementSessionRecord)
                .filter(SelfImprovementSessionRecord.status.in_(active_values))
                .order_by(SelfImprovementSessionRecord.started_at.desc())
                .first()
            )

    def get_cycle(self, cycle_id: str) -> SelfImprovementCycleRecord | None:
        with self._session() as session:
            return session.get(SelfImprovementCycleRecord, cycle_id)

    def get_session_record(self, session_id: str) -> SelfImprovementSessionRecord | None:
        with self._session() as session:
            return session.get(SelfImprovementSessionRecord, session_id)

    def list_cycles(self, limit: int = 20) -> list[SelfImprovementCycleRecord]:
        with self._session() as session:
            return (
                session.query(SelfImprovementCycleRecord)
                .order_by(SelfImprovementCycleRecord.started_at.desc())
                .limit(limit)
                .all()
            )

    def list_sessions(self, limit: int = 20) -> list[SelfImprovementSessionRecord]:
        with self._session() as session:
            return (
                session.query(SelfImprovementSessionRecord)
                .order_by(SelfImprovementSessionRecord.started_at.desc())
                .limit(limit)
                .all()
            )

    def get_status(self) -> SelfImprovementStatusResponse:
        active = self.get_active_cycle()
        active_session = self.get_active_session()
        all_cycles = self.list_cycles(limit=1)
        last_cycle = all_cycles[0] if all_cycles else None
        pending_review_cycles = [
            SelfImprovementCycleResponse.from_record(item)
            for item in self.list_cycles(limit=50)
            if item.status == CycleStatus.AWAITING_MANUAL_REVIEW.value
        ]
        daily = self.daily_cycle_count()
        limit_reached = self.is_daily_limit_reached()
        return SelfImprovementStatusResponse(
            mode=normalize_self_improvement_mode(self.settings.self_improvement_mode).value,
            enabled=self.settings.self_improvement_enabled,
            active_cycle=SelfImprovementCycleResponse.from_record(active) if active else None,
            active_session=SelfImprovementSessionResponse.from_record(active_session) if active_session else None,
            pending_review_cycles=pending_review_cycles,
            daily_cycle_count=daily,
            max_cycles_per_day=self.settings.self_improvement_max_cycles_per_day,
            daily_limit_reached=limit_reached,
            can_start=(
                not limit_reached
                and active is None
                and active_session is None
                and self.settings.self_improvement_enabled
            ),
            last_cycle=SelfImprovementCycleResponse.from_record(last_cycle) if last_cycle else None,
            open_incident_count=self.incident_service.open_count(),
        )

    def get_policy(self) -> SelfImprovementPolicyResponse:
        """Expose the resolved governance policy to API and UI callers."""

        return SelfImprovementPolicyResponse.from_policy(self.governance_service.load_policy())

    def list_incidents(self, limit: int = 20) -> list[SelfImprovementIncidentResponse]:
        """Return recent incidents for the operator dashboard."""

        return self.incident_service.list_recent(limit=limit)

    # ── cycle state mutations ─────────────────────────────────────────────────

    def _next_cycle_number(self) -> int:
        with self._session() as session:
            return session.query(SelfImprovementCycleRecord).count() + 1

    def _create_record(self, *, trigger: str, problem_hint: str | None) -> SelfImprovementCycleRecord:
        record = SelfImprovementCycleRecord(
            id=str(uuid4()),
            cycle_number=self._next_cycle_number(),
            status=CycleStatus.ANALYZING.value,
            trigger=trigger,
            max_retries=self.settings.self_improvement_max_auto_fix_attempts,
            metadata_json={"trigger": trigger, "problem_hint": problem_hint},
        )
        with self._session() as session:
            session.add(record)
            session.commit()
            session.refresh(record)
            return record

    def _create_session_record(
        self,
        *,
        trigger: str,
        problem_hint: str | None,
        max_cycles: int,
    ) -> SelfImprovementSessionRecord:
        record = SelfImprovementSessionRecord(
            id=str(uuid4()),
            status=SessionStatus.RUNNING.value,
            trigger=trigger,
            problem_hint=problem_hint,
            max_cycles=max_cycles,
            metadata_json={
                "original_problem_hint": problem_hint,
                "next_problem_hint": problem_hint,
                "goal_history": [],
                "error_history": [],
            },
        )
        with self._session() as session:
            session.add(record)
            session.commit()
            session.refresh(record)
            return record

    def _update(self, cycle_id: str, **kwargs: Any) -> SelfImprovementCycleRecord:
        with self._session() as session:
            record = session.get(SelfImprovementCycleRecord, cycle_id)
            if record is None:
                raise KeyError(f"Cycle {cycle_id} not found.")
            for key, val in kwargs.items():
                setattr(record, key, val)
            session.commit()
            session.refresh(record)
            return record

    def _update_session(self, session_id: str, **kwargs: Any) -> SelfImprovementSessionRecord:
        with self._session() as session:
            record = session.get(SelfImprovementSessionRecord, session_id)
            if record is None:
                raise KeyError(f"Session {session_id} not found.")
            for key, val in kwargs.items():
                setattr(record, key, val)
            session.commit()
            session.refresh(record)
            return record

    # ── public control API ────────────────────────────────────────────────────

    async def start_cycle(
        self,
        *,
        trigger: str = "manual",
        problem_hint: str | None = None,
        force: bool = False,
        run_task_fn: TaskRunner | None = None,
        running_tasks_set: set[str] | None = None,
    ) -> SelfImprovementCycleResponse:
        """
        Start a new improvement cycle.

        Returns immediately; the pipeline runs in the background.
        Raises SelfImprovementError if constraints are violated.
        """
        if not self.settings.self_improvement_enabled and not force:
            raise SelfImprovementError(
                "Self-Improvement ist deaktiviert (SELF_IMPROVEMENT_ENABLED=false). "
                "Aktiviere es in der Konfiguration oder verwende force=true."
            )
        if not force and self.is_daily_limit_reached():
            raise SelfImprovementError(
                f"Tageslimit von {self.settings.self_improvement_max_cycles_per_day} Zyklen erreicht."
            )
        if self.get_active_cycle() is not None:
            raise SelfImprovementError(
                "Es laeuft bereits ein aktiver Zyklus. Stoppe ihn zuerst oder warte auf Abschluss."
            )

        record = self._create_record(trigger=trigger, problem_hint=problem_hint)
        asyncio.create_task(
            self._run_pipeline(
                cycle_id=record.id,
                problem_hint=problem_hint,
                run_task_fn=run_task_fn,
            )
        )
        return SelfImprovementCycleResponse.from_record(record)

    async def start_session(
        self,
        *,
        trigger: str = "overnight",
        problem_hint: str | None = None,
        force: bool = False,
        max_cycles: int | None = None,
        run_task_fn: TaskRunner | None = None,
    ) -> SelfImprovementSessionResponse:
        """Start one unattended repair session that may run several full cycles in sequence."""

        requested_cycles = max(1, min(max_cycles or self.settings.self_improvement_max_session_cycles, 10))
        if not self.settings.self_improvement_enabled and not force:
            raise SelfImprovementError(
                "Self-Improvement ist deaktiviert (SELF_IMPROVEMENT_ENABLED=false). "
                "Aktiviere es in der Konfiguration oder verwende force=true."
            )
        if not force and self.is_daily_limit_reached():
            raise SelfImprovementError(
                f"Tageslimit von {self.settings.self_improvement_max_cycles_per_day} Zyklen erreicht."
            )
        if self.get_active_session() is not None:
            raise SelfImprovementError(
                "Es laeuft bereits eine aktive Selbstreparatur-Session. Stoppe sie zuerst oder warte auf Abschluss."
            )
        if self.get_active_cycle() is not None:
            raise SelfImprovementError(
                "Es laeuft bereits ein aktiver Self-Improvement-Zyklus. Stoppe ihn zuerst oder warte auf Abschluss."
            )

        record = self._create_session_record(
            trigger=trigger,
            problem_hint=problem_hint,
            max_cycles=requested_cycles,
        )
        asyncio.create_task(
            self._run_session(
                session_id=record.id,
                force=force,
                run_task_fn=run_task_fn,
            )
        )
        return SelfImprovementSessionResponse.from_record(record)

    def stop_cycle(self, cycle_id: str, *, actor: str = "human-operator") -> SelfImprovementCycleResponse:
        """Mark an active cycle as paused/stopped by the operator."""
        record = self._update(
            cycle_id,
            status=CycleStatus.PAUSED.value,
            completed_at=datetime.now(UTC),
            notes=f"Manuell gestoppt von: {actor}",
        )
        return SelfImprovementCycleResponse.from_record(record)

    def stop_session(
        self,
        session_id: str,
        *,
        actor: str = "human-operator",
    ) -> SelfImprovementSessionResponse:
        """Stop one unattended repair session and pause its current cycle if needed."""

        session_record = self.get_session_record(session_id)
        if session_record is None:
            raise KeyError(f"Session {session_id} nicht gefunden.")
        active_cycle = self.get_cycle(session_record.current_cycle_id) if session_record.current_cycle_id else None
        if active_cycle is not None and active_cycle.status in {state.value for state in self._ACTIVE}:
            self.stop_cycle(active_cycle.id, actor=actor)
        record = self._update_session(
            session_id,
            status=SessionStatus.STOPPED.value,
            completed_at=datetime.now(UTC),
            stop_reason=f"Manuell gestoppt von: {actor}",
        )
        return SelfImprovementSessionResponse.from_record(record)

    async def approve_risky_cycle(
        self,
        cycle_id: str,
        *,
        actor: str,
        reason: str | None,
        run_task_fn: TaskRunner | None = None,
        running_tasks_set: set[str] | None = None,
    ) -> SelfImprovementCycleResponse:
        """Approve either a pre-task governance gate or a paused workflow task gate."""
        cycle = self.get_cycle(cycle_id)
        if cycle is None:
            raise KeyError(f"Zyklus {cycle_id} nicht gefunden.")
        if cycle.status != CycleStatus.AWAITING_MANUAL_REVIEW.value:
            raise SelfImprovementError(
                f"Zyklus ist nicht im Status 'awaiting_manual_review' (aktuell: {cycle.status})."
            )
        meta = dict(cycle.metadata_json or {})
        meta.update(
            {
                "approved_by": actor,
                "approved_reason": reason,
                "approved_at": datetime.now(UTC).isoformat(),
                "governance_status": GovernanceStatus.APPROVED.value,
            }
        )
        self._approved_cycle_ids.add(cycle_id)

        if cycle.task_id:
            task = self.task_service.get_task(cycle.task_id)
            gate_name = task.current_approval_gate_name or str(meta.get("current_gate_name") or "risk-review")
            self.task_service.record_approval(
                cycle.task_id,
                ApprovalRequest(
                    gate_name=gate_name,
                    decision=ApprovalDecision.APPROVE,
                    actor=actor,
                    reason=reason,
                ),
            )
            self._update(
                cycle_id,
                status=CycleStatus.IMPLEMENTING.value,
                metadata_json=meta,
            )
            if run_task_fn is not None:
                asyncio.create_task(run_task_fn(cycle.task_id))
            refreshed = self.get_cycle(cycle_id)
            return SelfImprovementCycleResponse.from_record(refreshed) if refreshed else SelfImprovementCycleResponse.from_record(cycle)

        self._update(
            cycle_id,
            status=CycleStatus.IMPLEMENTING.value,
            metadata_json=meta,
        )
        asyncio.create_task(
            self._create_and_start_task(
                cycle_id=cycle_id,
                goal=cycle.goal or "",
                run_task_fn=run_task_fn,
                approved_override=True,
            )
        )
        refreshed = self.get_cycle(cycle_id)
        return SelfImprovementCycleResponse.from_record(refreshed) if refreshed else SelfImprovementCycleResponse.from_record(cycle)

    def resume_orphaned_cycles(
        self,
        run_task_fn: TaskRunner | None = None,
        running_tasks_set: set[str] | None = None,
    ) -> None:
        """
        Called on orchestrator startup.

        In-flight cycles are marked FAILED (fail-safe). The operator can start a
        new cycle manually. This avoids partially re-running tasks that may already
        have progressed through several stages.
        """
        active = self.get_active_cycle()
        if active is not None:
            if self._resume_self_update_watchdog_cycle(active.id):
                return
            self._update(
                active.id,
                status=CycleStatus.FAILED.value,
                completed_at=datetime.now(UTC),
                latest_error=(
                    "Zyklus war beim Neustart des Orchestrators aktiv. "
                    "Starte einen neuen Zyklus, um fortzufahren."
                ),
            )

    def resume_orphaned_sessions(
        self,
        run_task_fn: TaskRunner | None = None,
    ) -> None:
        """Resume unattended repair sessions after an orchestrator restart."""

        for record in self.list_sessions(limit=50):
            if record.status != SessionStatus.RUNNING.value:
                continue
            metadata = dict(record.metadata_json or {})
            asyncio.create_task(
                self._run_session(
                    session_id=record.id,
                    force=bool(metadata.get("force")),
                    run_task_fn=run_task_fn,
                )
            )

    @staticmethod
    def _normalize_history_value(value: str | None) -> str:
        """Reduce goal and error history to loop-detection fingerprints."""

        return " ".join((value or "").lower().split())[:240]

    def _session_is_stagnating(self, metadata: dict[str, Any]) -> bool:
        """Stop unattended sessions before they repeat the same failing idea forever."""

        goals = [self._normalize_history_value(item) for item in metadata.get("goal_history", []) if item]
        errors = [self._normalize_history_value(item) for item in metadata.get("error_history", []) if item]
        if len(goals) >= 2 and goals[-1] and goals[-1] == goals[-2]:
            return True
        if len(errors) >= 2 and errors[-1] and errors[-1] == errors[-2]:
            return True
        return False

    def _build_followup_problem_hint(
        self,
        session_record: SelfImprovementSessionRecord,
        cycle: SelfImprovementCycleRecord,
    ) -> str:
        """Carry the latest concrete failure context into the next unattended cycle."""

        original_hint = session_record.problem_hint or (session_record.metadata_json or {}).get("original_problem_hint")
        parts: list[str] = []
        if original_hint:
            parts.append(f"Urspruenglicher Hinweis: {str(original_hint)[:300]}")
        if cycle.goal:
            parts.append(f"Voriger Reparaturauftrag: {cycle.goal[:260]}")
        if cycle.problem_hypothesis:
            parts.append(f"Analyse des vorigen Zyklus: {cycle.problem_hypothesis[:260]}")
        if cycle.latest_error:
            parts.append(f"Letzter Fehler nach dem Reparaturversuch: {cycle.latest_error[:500]}")
        if not parts:
            parts.append("Voriger Reparaturversuch war nicht erfolgreich. Analysiere den naechsten kleinsten Fix.")
        return "\n".join(parts)[:1200]

    async def _run_session(
        self,
        *,
        session_id: str,
        force: bool,
        run_task_fn: TaskRunner | None,
        poll_interval: float = 20.0,
    ) -> None:
        """Drive several self-improvement cycles in sequence for unattended overnight repair."""
        try:
            while True:
                session_record = self.get_session_record(session_id)
                if session_record is None:
                    return
                if session_record.status not in {SessionStatus.RUNNING.value, SessionStatus.AWAITING_MANUAL_REVIEW.value}:
                    return
                if session_record.status == SessionStatus.AWAITING_MANUAL_REVIEW.value:
                    return

                current_cycle = (
                    self.get_cycle(session_record.current_cycle_id)
                    if session_record.current_cycle_id
                    else None
                )
                if current_cycle is None:
                    active_cycle = self.get_active_cycle()
                    if active_cycle is not None:
                        self._update_session(
                            session_id,
                            status=SessionStatus.FAILED.value,
                            completed_at=datetime.now(UTC),
                            last_error=active_cycle.latest_error,
                            stop_reason=(
                                "Ein anderer Self-Improvement-Zyklus laeuft parallel. "
                                "Nachtmodus stoppt, um Kollisionen zu vermeiden."
                            ),
                        )
                        return
                    if not force and self.is_daily_limit_reached():
                        self._update_session(
                            session_id,
                            status=SessionStatus.FAILED.value,
                            completed_at=datetime.now(UTC),
                            stop_reason=(
                                f"Tageslimit von {self.settings.self_improvement_max_cycles_per_day} Zyklen erreicht."
                            ),
                        )
                        return
                    if session_record.cycles_started >= session_record.max_cycles:
                        self._update_session(
                            session_id,
                            status=(
                                SessionStatus.COMPLETED.value
                                if session_record.success_cycles > 0
                                else SessionStatus.FAILED.value
                            ),
                            completed_at=datetime.now(UTC),
                            stop_reason=(
                                f"Session-Budget von {session_record.max_cycles} Zyklus/Zyklen erreicht."
                            ),
                        )
                        return

                    metadata = dict(session_record.metadata_json or {})
                    next_hint = str(metadata.get("next_problem_hint") or session_record.problem_hint or "").strip() or None
                    cycle = await self.start_cycle(
                        trigger=f"{session_record.trigger}_session",
                        problem_hint=next_hint,
                        force=force,
                        run_task_fn=run_task_fn,
                    )
                    metadata["force"] = force
                    self._update_session(
                        session_id,
                        status=SessionStatus.RUNNING.value,
                        current_cycle_id=cycle.id,
                        last_cycle_id=cycle.id,
                        cycles_started=session_record.cycles_started + 1,
                        stop_reason=None,
                        last_error=None,
                        metadata_json=metadata,
                    )
                    await asyncio.sleep(1.0)
                    continue

                if current_cycle.status in {state.value for state in self._ACTIVE}:
                    await asyncio.sleep(poll_interval)
                    continue

                metadata = dict(session_record.metadata_json or {})
                goal_history = list(metadata.get("goal_history") or [])
                error_history = list(metadata.get("error_history") or [])
                if current_cycle.goal:
                    goal_history.append(current_cycle.goal)
                if current_cycle.latest_error:
                    error_history.append(current_cycle.latest_error)
                metadata["goal_history"] = goal_history[-4:]
                metadata["error_history"] = error_history[-4:]
                metadata["last_cycle_status"] = current_cycle.status
                metadata["last_cycle_id"] = current_cycle.id

                if current_cycle.status == CycleStatus.AWAITING_MANUAL_REVIEW.value:
                    self._update_session(
                        session_id,
                        status=SessionStatus.AWAITING_MANUAL_REVIEW.value,
                        current_cycle_id=current_cycle.id,
                        last_cycle_id=current_cycle.id,
                        last_error=current_cycle.latest_error,
                        stop_reason=(
                            "Ein Reparaturzyklus benoetigt manuelle Freigabe. "
                            "Nachtmodus pausiert hier bewusst."
                        ),
                        metadata_json=metadata,
                    )
                    return

                completed_cycles = session_record.completed_cycles + 1
                base_update: dict[str, Any] = {
                    "current_cycle_id": None,
                    "last_cycle_id": current_cycle.id,
                    "completed_cycles": completed_cycles,
                    "metadata_json": metadata,
                }

                if current_cycle.status == CycleStatus.COMPLETED.value:
                    self._update_session(
                        session_id,
                        status=SessionStatus.COMPLETED.value,
                        completed_at=datetime.now(UTC),
                        success_cycles=session_record.success_cycles + 1,
                        last_error=None,
                        stop_reason=(
                            "Mindestens ein vollstaendiger Selbstreparaturzyklus wurde erfolgreich abgeschlossen."
                        ),
                        **base_update,
                    )
                    return

                metadata["next_problem_hint"] = self._build_followup_problem_hint(session_record, current_cycle)
                stagnating = self._session_is_stagnating(metadata)
                failed_cycles = session_record.failed_cycles + 1

                if stagnating:
                    self._update_session(
                        session_id,
                        status=SessionStatus.FAILED.value,
                        completed_at=datetime.now(UTC),
                        failed_cycles=failed_cycles,
                        last_error=current_cycle.latest_error,
                        stop_reason=(
                            "Zwei aufeinanderfolgende Reparaturzyklen wiederholen denselben Auftrag oder Fehler. "
                            "Nachtmodus stoppt, um Endlosschleifen zu vermeiden."
                        ),
                        **base_update,
                    )
                    return

                if completed_cycles >= session_record.max_cycles:
                    self._update_session(
                        session_id,
                        status=SessionStatus.FAILED.value,
                        completed_at=datetime.now(UTC),
                        failed_cycles=failed_cycles,
                        last_error=current_cycle.latest_error,
                        stop_reason=(
                            f"Maximale Session-Laenge von {session_record.max_cycles} Zyklus/Zyklen erreicht."
                        ),
                        **base_update,
                    )
                    return

                self._update_session(
                    session_id,
                    status=SessionStatus.RUNNING.value,
                    failed_cycles=failed_cycles,
                    last_error=current_cycle.latest_error,
                    stop_reason="Naechster autonomer Reparaturzyklus wird vorbereitet.",
                    **base_update,
                )
                await asyncio.sleep(1.0)
        except Exception as exc:
            self._update_session(
                session_id,
                status=SessionStatus.FAILED.value,
                completed_at=datetime.now(UTC),
                last_error=f"{type(exc).__name__}: {exc}",
                stop_reason="Der Nachtmodus ist unerwartet abgestuerzt und wurde sicher beendet.",
            )

    def _resume_self_update_watchdog_cycle(self, cycle_id: str) -> bool:
        """Resume external self-update monitoring instead of failing immediately after an orchestrator restart."""

        cycle = self.get_cycle(cycle_id)
        if cycle is None or not cycle.task_id:
            return False
        metadata = dict(cycle.metadata_json or {})
        if not metadata.get("allow_deploy_after_success"):
            return False

        state = read_watchdog_state(cycle.task_id, self.settings)
        if state is None:
            return False
        if state.status in {
            SelfUpdateWatchdogStatus.ARMED,
            SelfUpdateWatchdogStatus.MONITORING,
            SelfUpdateWatchdogStatus.ROLLBACK_RUNNING,
        }:
            asyncio.create_task(self._monitor_external_watchdog(cycle.id, cycle.task_id))
            return True

        self._finalize_cycle_from_watchdog(cycle.id, cycle.task_id, state)
        return True

    async def _monitor_external_watchdog(self, cycle_id: str, task_id: str) -> None:
        """Keep observing a persisted self-update watchdog after the orchestrator has restarted."""

        while True:
            await asyncio.sleep(max(3.0, self.settings.self_update_watchdog_poll_seconds))
            state = read_watchdog_state(task_id, self.settings)
            if state is None:
                return
            if state.status in {
                SelfUpdateWatchdogStatus.ARMED,
                SelfUpdateWatchdogStatus.MONITORING,
                SelfUpdateWatchdogStatus.ROLLBACK_RUNNING,
            }:
                cycle = self.get_cycle(cycle_id)
                if cycle is None:
                    return
                metadata = dict(cycle.metadata_json or {})
                metadata.update(
                    {
                        "watchdog_status": state.status.value,
                        "watchdog_updated_at": state.updated_at.isoformat(),
                        "rollback_status": state.status.value
                        if state.status == SelfUpdateWatchdogStatus.ROLLBACK_RUNNING
                        else metadata.get("rollback_status"),
                    }
                )
                self._update(cycle_id, metadata_json=metadata)
                continue

            self._finalize_cycle_from_watchdog(cycle_id, task_id, state)
            return

    def _finalize_cycle_from_watchdog(
        self,
        cycle_id: str,
        task_id: str,
        state: SelfUpdateWatchdogState,
    ) -> None:
        """Translate one persisted watchdog outcome into durable task and cycle state."""

        cycle = self.get_cycle(cycle_id)
        if cycle is None:
            return

        metadata = dict(cycle.metadata_json or {})
        metadata.update(
            {
                "watchdog_status": state.status.value,
                "watchdog_updated_at": state.updated_at.isoformat(),
            }
        )

        try:
            if state.status == SelfUpdateWatchdogStatus.HEALTHY:
                self.task_service.update_status(
                    task_id,
                    TaskStatus.DONE,
                    message="Self-Update erfolgreich abgeschlossen; der Rollback-Watchdog hat den gesunden Stack bestaetigt.",
                    details={
                        "watchdog_status": state.status.value,
                        "health_url": state.health_url,
                        "watchdog_previous_sha": state.previous_sha,
                        "watchdog_current_sha": state.current_sha,
                    },
                )
                self._update(
                    cycle_id,
                    status=CycleStatus.COMPLETED.value,
                    completed_at=datetime.now(UTC),
                    latest_error=None,
                    deploy_result_json={
                        "watchdog_status": state.status.value,
                        "project_dir": state.project_dir,
                        "branch_name": state.branch_name,
                    },
                    healthcheck_result_json={
                        "status": "ok",
                        "health_url": state.health_url,
                        "observed_target_change": state.observed_target_change,
                    },
                    metadata_json=metadata,
                )
                return
        except KeyError:
            pass

        rollback_status = (
            IncidentStatus.ROLLED_BACK.value
            if state.status == SelfUpdateWatchdogStatus.ROLLED_BACK
            else state.status.value
        )
        metadata["rollback_status"] = rollback_status
        latest_error = state.last_error or (
            "Self-Update blieb nicht gesund; der Rollback-Watchdog hat einen Fehler festgestellt."
        )

        incident_id = str(metadata.get("incident_id") or "").strip()
        if incident_id:
            self.incident_service.update_rollback_status(
                incident_id,
                rollback_status=rollback_status,
                latest_error=latest_error,
                metadata_updates={"watchdog_state": state.model_dump(mode="json")},
            )
        else:
            incident = self.incident_service.create_incident(
                cycle_id=cycle_id,
                task_id=task_id,
                severity=cycle.risk_level or RiskLevel.HIGH.value,
                summary="Self-Update wurde vom Rollback-Watchdog als fehlgeschlagen erkannt.",
                failure_stage="deploy",
                latest_error=latest_error,
                root_cause=cycle.problem_hypothesis,
                commit_sha=state.current_sha,
                branch_name=state.branch_name,
                metadata={"watchdog_state": state.model_dump(mode="json")},
            )
            incident_id = incident.id
            metadata["incident_id"] = incident.id
            self.incident_service.update_rollback_status(
                incident.id,
                rollback_status=rollback_status,
                latest_error=latest_error,
                metadata_updates={"watchdog_state": state.model_dump(mode="json")},
            )

        try:
            self.task_service.update_status(
                task_id,
                TaskStatus.FAILED,
                message="Self-Update wurde vom Rollback-Watchdog als fehlgeschlagen markiert.",
                details={
                    "watchdog_status": state.status.value,
                    "health_url": state.health_url,
                    "watchdog_previous_sha": state.previous_sha,
                    "watchdog_current_sha": state.current_sha,
                },
                latest_error=latest_error,
            )
        except KeyError:
            pass

        self._update(
            cycle_id,
            status=CycleStatus.FAILED.value,
            completed_at=datetime.now(UTC),
            latest_error=latest_error,
            metadata_json=metadata,
        )

    async def _send_cycle_email(
        self,
        *,
        cycle_id: str,
        kind: str,
        subject: str,
        body: str,
        metadata: dict[str, Any],
    ) -> None:
        """Send or queue one operator mail and mirror the delivery result into cycle metadata."""

        result = await self.email_service.send_cycle_email(
            subject=subject,
            body=body,
            kind=kind,
            metadata=metadata,
        )
        cycle = self.get_cycle(cycle_id)
        if cycle is None:
            return
        meta = dict(cycle.metadata_json or {})
        meta.update(
            {
                "approval_email_status": result.status,
                "approval_email_detail": result.detail,
                "approval_email_outbox_path": result.outbox_path,
                "approval_email_message_id": result.message_id,
            }
        )
        self._update(cycle_id, metadata_json=meta)

    def _build_cycle_email(
        self,
        *,
        cycle_id: str,
        goal: str,
        risk_level: str,
        governance_decision: GovernanceDecision,
        problem_hypothesis: str,
        task_id: str | None = None,
        branch_name: str | None = None,
        changed_files: list[str] | None = None,
        latest_error: str | None = None,
        test_results: dict[str, Any] | None = None,
    ) -> tuple[str, str, dict[str, Any]]:
        """Create a concise mail payload without leaking secrets or raw prompts."""

        subject = (
            f"[Feberdin Self-Improvement] {governance_decision.email_intent.value.upper()} · "
            f"{risk_level.upper()} · Zyklus {cycle_id[:8]}"
        )
        changed_files_text = ", ".join(changed_files or []) or "Noch keine Dateiliste vorhanden."
        tests_summary = "Keine Testergebnisse vorhanden."
        if test_results:
            tests_summary = json.dumps(test_results, ensure_ascii=False, default=str)[:600]
        body = (
            "Feberdin/local-multi-agent-company hat eine neue Selbstverbesserungs-Entscheidung vorbereitet.\n\n"
            f"Zyklus: {cycle_id}\n"
            f"Modus: {governance_decision.mode}\n"
            f"Risiko: {risk_level.upper()}\n"
            f"Aktion: {governance_decision.action.value}\n"
            f"Governance-Status: {governance_decision.governance_status.value}\n"
            f"Ziel: {goal}\n"
            f"Analyse: {problem_hypothesis or 'keine Hypothese vorhanden'}\n"
            f"Task: {task_id or 'noch keiner'}\n"
            f"Branch: {branch_name or 'noch keiner'}\n"
            f"Betroffene Dateien: {changed_files_text}\n"
            f"Tests: {tests_summary}\n"
            f"Letzter Fehler: {latest_error or 'kein Fehler gemeldet'}\n\n"
            "Freigabe erfolgt im Dashboard unter /self-improvement. "
            "Die Nachricht wurde zusaetzlich im lokalen Outbox-Ordner protokolliert.\n"
        )
        metadata = {
            "cycle_id": cycle_id,
            "task_id": task_id,
            "risk_level": risk_level,
            "governance_action": governance_decision.action.value,
            "branch_name": branch_name,
            "changed_files": changed_files or [],
        }
        return subject, body, metadata

    @staticmethod
    def _extract_commit_context(task) -> tuple[str | None, str | None, list[str]]:
        """Read commit/branch/file context from task outputs without assuming every stage ran."""

        worker_results = task.worker_results or {}
        github_outputs = (worker_results.get("github") or {}).get("outputs", {})
        coding_outputs = (worker_results.get("coding") or {}).get("outputs", {})
        commit_sha = github_outputs.get("commit_sha")
        branch_name = coding_outputs.get("branch_name") or task.branch_name
        changed_files = coding_outputs.get("changed_files", [])
        return commit_sha, branch_name, changed_files

    @staticmethod
    def _extract_failure_context(task) -> tuple[str | None, str | None]:
        """Identify the first failed worker and the most useful error summary."""

        worker_results = task.worker_results or {}
        ordered_workers = [
            "requirements",
            "cost",
            "human_resources",
            "research",
            "architecture",
            "data",
            "ux",
            "coding",
            "reviewer",
            "tester",
            "security",
            "validation",
            "documentation",
            "github",
            "deploy",
            "qa",
            "memory",
        ]
        for worker_name in ordered_workers:
            result = worker_results.get(worker_name)
            if not result or result.get("success", True):
                continue
            errors = result.get("errors") or []
            if errors:
                return worker_name, "; ".join(str(item) for item in errors)
            if result.get("summary"):
                return worker_name, str(result["summary"])
        return None, task.latest_error

    def _governance_metadata_update(
        self,
        decision: GovernanceDecision,
        *,
        goal: str,
        problem_class: ProblemClass,
        hypothesis: str,
    ) -> dict[str, Any]:
        """Store governance-relevant state in one stable metadata block for API/UI reuse."""

        return {
            "governance_status": decision.governance_status.value,
            "governance_action": decision.action.value,
            "governance_note": decision.note,
            "approval_email_status": "pending" if decision.email_intent != ApprovalEmailIntent.NONE else "not_needed",
            "approval_email_detail": None,
            "current_gate_name": decision.approval_gate_name if decision.governance_status == GovernanceStatus.AWAITING_APPROVAL else None,
            "current_gate_reason": decision.note if decision.governance_status == GovernanceStatus.AWAITING_APPROVAL else None,
            "problem_class": problem_class.value,
            "problem_hypothesis": hypothesis,
            "goal": goal,
            "risk_level": decision.risk_level,
            "force_publish_approval": decision.require_publish_approval,
            "allow_deploy_after_success": decision.allow_deploy,
        }

    # ── internal pipeline ─────────────────────────────────────────────────────

    async def _run_pipeline(
        self,
        cycle_id: str,
        problem_hint: str | None,
        run_task_fn: TaskRunner | None,
    ) -> None:
        """Async pipeline: analyze → plan → (gate?) → implement → monitor."""
        try:
            # Phase 1: analyze
            self._update(cycle_id, status=CycleStatus.ANALYZING.value)
            goal, problem_class, hypothesis = await analyze_problems(
                self.task_service,
                self.llm_client,
                problem_hint=problem_hint,
            )
            risk_level, risk_reason = classify_risk(goal)
            is_risky = risk_level in {RiskLevel.HIGH, RiskLevel.CRITICAL}
            governance_decision = self.governance_service.decide(
                risk_level=risk_level.value,
                mode=self.settings.self_improvement_mode,
            )
            cycle_metadata = self._governance_metadata_update(
                governance_decision,
                goal=goal,
                problem_class=problem_class,
                hypothesis=hypothesis,
            )

            # Phase 2: plan
            self._update(
                cycle_id,
                status=CycleStatus.PLANNING.value,
                goal=goal,
                problem_class=problem_class.value,
                problem_hypothesis=hypothesis,
                risk_level=risk_level.value,
                is_risky=is_risky,
                risk_reason=risk_reason,
                metadata_json=cycle_metadata,
            )

            if governance_decision.email_intent != ApprovalEmailIntent.NONE:
                subject, body, email_metadata = self._build_cycle_email(
                    cycle_id=cycle_id,
                    goal=goal,
                    risk_level=risk_level.value,
                    governance_decision=governance_decision,
                    problem_hypothesis=hypothesis,
                )
                asyncio.create_task(
                    self._send_cycle_email(
                        cycle_id=cycle_id,
                        kind=governance_decision.email_intent.value,
                        subject=subject,
                        body=body,
                        metadata=email_metadata,
                    )
                )

            if not governance_decision.allow_task_execution:
                terminal_status = (
                    CycleStatus.AWAITING_MANUAL_REVIEW.value
                    if governance_decision.governance_status == GovernanceStatus.AWAITING_APPROVAL
                    else CycleStatus.COMPLETED.value
                )
                self._update(
                    cycle_id,
                    status=terminal_status,
                    completed_at=None if terminal_status == CycleStatus.AWAITING_MANUAL_REVIEW.value else datetime.now(UTC),
                    notes=governance_decision.note,
                )
                return  # pipeline resumes via approve_risky_cycle()

            # Phase 3: implement
            self._update(cycle_id, status=CycleStatus.IMPLEMENTING.value)
            await self._create_and_start_task(
                cycle_id=cycle_id,
                goal=goal,
                run_task_fn=run_task_fn,
                governance_decision=governance_decision,
            )

        except SelfImprovementError as exc:
            self._update(
                cycle_id,
                status=CycleStatus.FAILED.value,
                completed_at=datetime.now(UTC),
                latest_error=str(exc),
            )
        except Exception as exc:
            self._update(
                cycle_id,
                status=CycleStatus.FAILED.value,
                completed_at=datetime.now(UTC),
                latest_error=f"Unerwarteter Fehler in der Analyse-Phase: {type(exc).__name__}: {exc}",
            )

    async def _create_and_start_task(
        self,
        cycle_id: str,
        goal: str,
        run_task_fn: TaskRunner | None,
        governance_decision: GovernanceDecision | None = None,
        approved_override: bool = False,
    ) -> None:
        """Create a workflow task for this improvement and start monitoring it."""
        try:
            cycle = self.get_cycle(cycle_id)
            cycle_metadata = dict(cycle.metadata_json or {}) if cycle else {}
            decision = governance_decision
            if decision is None:
                decision = self.governance_service.decide(
                    risk_level=str(cycle.risk_level if cycle else "low"),
                    mode=self.settings.self_improvement_mode,
                )
            force_publish_approval = bool(cycle_metadata.get("force_publish_approval")) and not approved_override
            auto_deploy = (
                bool(cycle_metadata.get("allow_deploy_after_success", decision.allow_deploy))
                and not approved_override
            )
            request = TaskCreateRequest(
                goal=goal,
                repository=self.settings.self_improvement_target_repo,
                local_repo_path=self.settings.self_improvement_local_repo_path,
                base_branch=self.settings.default_base_branch,
                allow_repository_modifications=True,
                auto_deploy_staging=auto_deploy,
                metadata={
                    **cycle_metadata,
                    "self_improvement_cycle_id": cycle_id,
                    "worker_project_label": "Self-Improvement-Zyklus",
                    "allow_repository_modifications": True,
                    "deployment_target": "self" if auto_deploy else "staging",
                    "self_improvement_mode": decision.mode,
                    "self_improvement_governance_action": decision.action.value,
                    "force_publish_approval": force_publish_approval,
                },
            )
            summary = self.task_service.create_task(request)
            task_id = summary.id

            cycle_metadata.update(
                {
                    "governance_status": GovernanceStatus.PENDING.value,
                    "governance_action": decision.action.value,
                    "force_publish_approval": force_publish_approval,
                    "allow_deploy_after_success": auto_deploy,
                }
            )
            self._update(cycle_id, task_id=task_id, metadata_json=cycle_metadata)

            # run_task_fn handles adding task_id to running_tasks and starting the workflow.
            # Do NOT pre-add here — the idempotency guard in run_task_fn would block the start.
            if run_task_fn is not None:
                asyncio.create_task(run_task_fn(task_id))

            await self._monitor_task(
                cycle_id=cycle_id,
                task_id=task_id,
                run_task_fn=run_task_fn,
            )

        except Exception as exc:
            self._update(
                cycle_id,
                status=CycleStatus.FAILED.value,
                completed_at=datetime.now(UTC),
                latest_error=f"Task-Erstellung fehlgeschlagen: {type(exc).__name__}: {exc}",
            )

    async def _handle_failed_task(
        self,
        *,
        cycle_id: str,
        task,
        run_task_fn: TaskRunner | None,
    ) -> str | None:
        """Handle failed self-improvement tasks via incident creation, rollback prep, or retry."""

        cycle = self.get_cycle(cycle_id)
        if cycle is None:
            return None

        failure_stage, failure_text = self._extract_failure_context(task)
        commit_sha, branch_name, changed_files = self._extract_commit_context(task)
        incident = self.incident_service.create_incident(
            cycle_id=cycle_id,
            task_id=task.id,
            severity=cycle.risk_level or RiskLevel.LOW.value,
            summary=f"Self-Improvement-Zyklus {cycle.cycle_number} ist fehlgeschlagen.",
            failure_stage=failure_stage,
            latest_error=failure_text or task.latest_error,
            root_cause=cycle.problem_hypothesis,
            commit_sha=commit_sha,
            branch_name=branch_name,
            metadata={"changed_files": changed_files},
        )
        cycle_metadata = dict(cycle.metadata_json or {})
        cycle_metadata["incident_id"] = incident.id

        if self.settings.self_improvement_auto_rollback and commit_sha:
            rollback_request = TaskCreateRequest(
                goal=f"Revertiere Commit {commit_sha} und stelle den letzten stabilen Zustand wieder her.",
                repository=self.settings.self_improvement_target_repo,
                local_repo_path=self.settings.self_improvement_local_repo_path,
                base_branch=self.settings.default_base_branch,
                allow_repository_modifications=True,
                auto_deploy_staging=False,
                metadata={
                    "self_improvement_cycle_id": cycle_id,
                    "worker_project_label": "Self-Improvement-Rollback",
                    "allow_repository_modifications": True,
                    "rollback_commit_sha": commit_sha,
                    "rollback_incident_id": incident.id,
                    "deployment_target": "self",
                },
            )
            rollback_summary = self.task_service.create_task(rollback_request)
            self.incident_service.attach_rollback_task(
                incident.id,
                rollback_task_id=rollback_summary.id,
                rollback_status=IncidentStatus.ROLLBACK_RUNNING.value,
            )
            cycle_metadata.update(
                {
                    "rollback_task_id": rollback_summary.id,
                    "rollback_status": IncidentStatus.ROLLBACK_RUNNING.value,
                }
            )
            self._update(
                cycle_id,
                status=CycleStatus.FAILED.value,
                completed_at=datetime.now(UTC),
                latest_error=failure_text or task.latest_error,
                metadata_json=cycle_metadata,
            )
            if run_task_fn is not None:
                asyncio.create_task(run_task_fn(rollback_summary.id))
            return None

        new_retry = cycle.retry_count + 1
        if new_retry >= cycle.max_retries:
            cycle_metadata["governance_status"] = GovernanceStatus.FAILED.value
            self._update(
                cycle_id,
                status=CycleStatus.FAILED.value,
                completed_at=datetime.now(UTC),
                retry_count=new_retry,
                latest_error=(
                    f"Task fehlgeschlagen nach {new_retry} Versuch(en). "
                    f"Letzter Fehler: {task.latest_error or 'unbekannt'}"
                ),
                metadata_json=cycle_metadata,
            )
            return None

        try:
            goal = cycle.goal or ""
            retry_request = TaskCreateRequest(
                goal=goal,
                repository=self.settings.self_improvement_target_repo,
                local_repo_path=self.settings.self_improvement_local_repo_path,
                base_branch=self.settings.default_base_branch,
                allow_repository_modifications=True,
                auto_deploy_staging=bool(cycle_metadata.get("allow_deploy_after_success")),
                metadata={
                    **cycle_metadata,
                    "self_improvement_cycle_id": cycle_id,
                    "worker_project_label": "Self-Improvement-Zyklus",
                    "allow_repository_modifications": True,
                    "retry_attempt": new_retry,
                    "deployment_target": "self" if cycle_metadata.get("allow_deploy_after_success") else "staging",
                },
            )
            retry_summary = self.task_service.create_task(retry_request)
            self._update(
                cycle_id,
                retry_count=new_retry,
                status=CycleStatus.IMPLEMENTING.value,
                task_id=retry_summary.id,
                latest_error=task.latest_error,
                metadata_json=cycle_metadata,
            )
            if run_task_fn is not None:
                asyncio.create_task(run_task_fn(retry_summary.id))
            return retry_summary.id
        except Exception as retry_exc:
            cycle_metadata["governance_status"] = GovernanceStatus.FAILED.value
            self._update(
                cycle_id,
                status=CycleStatus.FAILED.value,
                completed_at=datetime.now(UTC),
                latest_error=f"Retry-Task-Erstellung fehlgeschlagen: {retry_exc}",
                metadata_json=cycle_metadata,
            )
            return None

    async def _monitor_task(
        self,
        cycle_id: str,
        task_id: str,
        poll_interval: float = 30.0,
        max_wait_hours: float = 4.0,
        run_task_fn: TaskRunner | None = None,
    ) -> None:
        """
        Poll the linked task every poll_interval seconds.
        Update the cycle record as the task progresses.
        On failure, creates a brand-new task for each retry (up to max_retries).
        """
        import time

        deadline = time.monotonic() + max_wait_hours * 3600
        current_task_id = task_id

        while time.monotonic() < deadline:
            await asyncio.sleep(poll_interval)

            try:
                task = self.task_service.get_task(current_task_id)
            except (KeyError, Exception):
                continue

            # Mirror branch and error into cycle record for UI visibility
            update_kwargs: dict[str, Any] = {}
            commit_sha, branch_name, changed_files = self._extract_commit_context(task)
            if branch_name:
                update_kwargs["branch_name"] = branch_name
            if commit_sha:
                update_kwargs["commit_sha"] = commit_sha
            if task.latest_error:
                update_kwargs["latest_error"] = task.latest_error
            if update_kwargs:
                self._update(cycle_id, **update_kwargs)

            if task.status == TaskStatus.FAILED:
                handled_task_id = await self._handle_failed_task(
                    cycle_id=cycle_id,
                    task=task,
                    run_task_fn=run_task_fn,
                )
                if handled_task_id is None:
                    return
                current_task_id = handled_task_id
                continue

            if task.status == TaskStatus.SELF_UPDATING:
                watchdog_state = read_watchdog_state(task.id, self.settings)
                if watchdog_state is not None:
                    if watchdog_state.status in {
                        SelfUpdateWatchdogStatus.ARMED,
                        SelfUpdateWatchdogStatus.MONITORING,
                        SelfUpdateWatchdogStatus.ROLLBACK_RUNNING,
                    }:
                        cycle = self.get_cycle(cycle_id)
                        if cycle is not None:
                            metadata = dict(cycle.metadata_json or {})
                            metadata.update(
                                {
                                    "watchdog_status": watchdog_state.status.value,
                                    "watchdog_updated_at": watchdog_state.updated_at.isoformat(),
                                    "rollback_status": watchdog_state.status.value
                                    if watchdog_state.status == SelfUpdateWatchdogStatus.ROLLBACK_RUNNING
                                    else metadata.get("rollback_status"),
                                }
                            )
                            self._update(cycle_id, metadata_json=metadata)
                        continue

                    self._finalize_cycle_from_watchdog(cycle_id, task.id, watchdog_state)
                    return

            if task.status in {TaskStatus.APPROVAL_REQUIRED}:
                cycle = self.get_cycle(cycle_id)
                if cycle is None:
                    return
                meta = dict(cycle.metadata_json or {})
                meta.update(
                    {
                        "governance_status": GovernanceStatus.AWAITING_APPROVAL.value,
                        "current_gate_name": task.current_approval_gate_name,
                        "current_gate_reason": task.approval_reason,
                    }
                )
                self._update(cycle_id, status=CycleStatus.AWAITING_MANUAL_REVIEW.value, metadata_json=meta)
                subject, body, email_metadata = self._build_cycle_email(
                    cycle_id=cycle_id,
                    goal=cycle.goal or "",
                    risk_level=cycle.risk_level or RiskLevel.LOW.value,
                    governance_decision=self.governance_service.decide(
                        risk_level=cycle.risk_level or RiskLevel.LOW.value,
                        mode=self.settings.self_improvement_mode,
                    ),
                    problem_hypothesis=cycle.problem_hypothesis or "",
                    task_id=task.id,
                    branch_name=branch_name,
                    changed_files=changed_files,
                    latest_error=task.approval_reason,
                    test_results=(task.worker_results or {}).get("tester", {}).get("outputs", {}),
                )
                asyncio.create_task(
                    self._send_cycle_email(
                        cycle_id=cycle_id,
                        kind="approval",
                        subject=subject,
                        body=body,
                        metadata=email_metadata,
                    )
                )
                continue

            if task.status == TaskStatus.DONE or task.status == TaskStatus.PR_CREATED:
                outputs = task.worker_results
                coding_out = outputs.get("coding", {}).get("outputs", {})
                github_out = outputs.get("github", {}).get("outputs", {})
                testing_out = outputs.get("tester", {}).get("outputs", {})
                deploy_out = outputs.get("deploy", {}).get("outputs", {})
                qa_out = outputs.get("qa", {}).get("outputs", {})
                cycle = self.get_cycle(cycle_id)
                metadata = dict(cycle.metadata_json or {}) if cycle else {}
                metadata.update(
                    {
                        "governance_status": GovernanceStatus.IMPLEMENTED.value,
                        "rollback_status": metadata.get("rollback_status"),
                    }
                )
                self._update(
                    cycle_id,
                    status=CycleStatus.COMPLETED.value,
                    completed_at=datetime.now(UTC),
                    branch_name=coding_out.get("branch_name") or task.branch_name,
                    commit_sha=github_out.get("commit_sha"),
                    changed_files_json=coding_out.get("changed_files", []),
                    test_results_json=testing_out or {},
                    deploy_result_json=deploy_out or {},
                    healthcheck_result_json=qa_out or {},
                    metadata_json=metadata,
                )
                return

        # Deadline exceeded
        self._update(
            cycle_id,
            status=CycleStatus.FAILED.value,
            completed_at=datetime.now(UTC),
            latest_error=f"Ueberwachung abgebrochen nach {max_wait_hours:.1f}h (Timeout).",
        )
