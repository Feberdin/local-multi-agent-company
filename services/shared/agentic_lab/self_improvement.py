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
import re
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel
from sqlalchemy.orm import Session

from services.shared.agentic_lab.config import Settings, get_settings
from services.shared.agentic_lab.db import SelfImprovementCycleRecord, get_session_factory
from services.shared.agentic_lab.llm import LLMClient, LLMError
from services.shared.agentic_lab.schemas import TaskCreateRequest, TaskStatus
from services.shared.agentic_lab.task_service import TaskService

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

    return goal, problem_class, hypothesis


def _fallback_goal(problem_hint: str | None, cls: ProblemClass) -> str:
    if problem_hint:
        return f"Behebe folgendes Problem im Repository: {problem_hint[:300]}"
    fallbacks = {
        ProblemClass.TIMEOUT: (
            "Erhoehe den konfigurierbaren Timeout-Wert fuer den Worker-Stage-Ablauf "
            "und ergaenze eine klarere Fehlermeldung bei Timeout."
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

    @classmethod
    def from_record(cls, r: SelfImprovementCycleRecord) -> SelfImprovementCycleResponse:
        try:
            status = CycleStatus(r.status).value
        except ValueError:
            status = r.status
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
            metadata=r.metadata_json or {},
        )


class SelfImprovementStatusResponse(BaseModel):
    mode: str
    enabled: bool
    active_cycle: SelfImprovementCycleResponse | None
    daily_cycle_count: int
    max_cycles_per_day: int
    daily_limit_reached: bool
    can_start: bool
    last_cycle: SelfImprovementCycleResponse | None


class StartCycleRequest(BaseModel):
    trigger: str = "manual"
    problem_hint: str | None = None
    force: bool = False


class ApproveCycleRequest(BaseModel):
    actor: str = "human-operator"
    reason: str | None = None


class SelfImprovementConfigResponse(BaseModel):
    enabled: bool
    mode: str
    max_auto_fix_attempts: int
    max_cycles_per_day: int
    deploy_after_success: bool
    require_approval_for_risky: bool
    preflight_required: bool
    auto_rollback: bool
    target_repo: str
    local_repo_path: str


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
        CycleStatus.AWAITING_MANUAL_REVIEW,
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

    def get_cycle(self, cycle_id: str) -> SelfImprovementCycleRecord | None:
        with self._session() as session:
            return session.get(SelfImprovementCycleRecord, cycle_id)

    def list_cycles(self, limit: int = 20) -> list[SelfImprovementCycleRecord]:
        with self._session() as session:
            return (
                session.query(SelfImprovementCycleRecord)
                .order_by(SelfImprovementCycleRecord.started_at.desc())
                .limit(limit)
                .all()
            )

    def get_status(self) -> SelfImprovementStatusResponse:
        active = self.get_active_cycle()
        all_cycles = self.list_cycles(limit=1)
        last_cycle = all_cycles[0] if all_cycles else None
        daily = self.daily_cycle_count()
        limit_reached = self.is_daily_limit_reached()
        return SelfImprovementStatusResponse(
            mode=self.settings.self_improvement_mode,
            enabled=self.settings.self_improvement_enabled,
            active_cycle=SelfImprovementCycleResponse.from_record(active) if active else None,
            daily_cycle_count=daily,
            max_cycles_per_day=self.settings.self_improvement_max_cycles_per_day,
            daily_limit_reached=limit_reached,
            can_start=not limit_reached and active is None and self.settings.self_improvement_enabled,
            last_cycle=SelfImprovementCycleResponse.from_record(last_cycle) if last_cycle else None,
        )

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

    # ── public control API ────────────────────────────────────────────────────

    async def start_cycle(
        self,
        *,
        trigger: str = "manual",
        problem_hint: str | None = None,
        force: bool = False,
        run_task_fn: Callable[[str], Awaitable[None]] | None = None,
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

    def stop_cycle(self, cycle_id: str, *, actor: str = "human-operator") -> SelfImprovementCycleResponse:
        """Mark an active cycle as paused/stopped by the operator."""
        record = self._update(
            cycle_id,
            status=CycleStatus.PAUSED.value,
            completed_at=datetime.now(UTC),
            notes=f"Manuell gestoppt von: {actor}",
        )
        return SelfImprovementCycleResponse.from_record(record)

    async def approve_risky_cycle(
        self,
        cycle_id: str,
        *,
        actor: str,
        reason: str | None,
        run_task_fn: Callable[[str], Awaitable[None]] | None = None,
        running_tasks_set: set[str] | None = None,
    ) -> SelfImprovementCycleResponse:
        """Operator approves a cycle that was waiting for manual review of a risky goal."""
        cycle = self.get_cycle(cycle_id)
        if cycle is None:
            raise KeyError(f"Zyklus {cycle_id} nicht gefunden.")
        if cycle.status != CycleStatus.AWAITING_MANUAL_REVIEW.value:
            raise SelfImprovementError(
                f"Zyklus ist nicht im Status 'awaiting_manual_review' (aktuell: {cycle.status})."
            )
        meta = dict(cycle.metadata_json or {})
        meta.update({"approved_by": actor, "approved_reason": reason, "approved_at": datetime.now(UTC).isoformat()})
        self._approved_cycle_ids.add(cycle_id)
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
            )
        )
        return SelfImprovementCycleResponse.from_record(self.get_cycle(cycle_id))

    def resume_orphaned_cycles(
        self,
        run_task_fn: Callable[[str], Awaitable[None]] | None = None,
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
            self._update(
                active.id,
                status=CycleStatus.FAILED.value,
                completed_at=datetime.now(UTC),
                latest_error=(
                    "Zyklus war beim Neustart des Orchestrators aktiv. "
                    "Starte einen neuen Zyklus, um fortzufahren."
                ),
            )

    # ── internal pipeline ─────────────────────────────────────────────────────

    async def _run_pipeline(
        self,
        cycle_id: str,
        problem_hint: str | None,
        run_task_fn: Callable[[str], Awaitable[None]] | None,
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
            )

            # Gate: risky changes need approval
            if is_risky and self.settings.self_improvement_require_approval_for_risky:
                self._update(cycle_id, status=CycleStatus.AWAITING_MANUAL_REVIEW.value)
                return  # pipeline resumes via approve_risky_cycle()

            # Phase 3: implement
            self._update(cycle_id, status=CycleStatus.IMPLEMENTING.value)
            await self._create_and_start_task(
                cycle_id=cycle_id,
                goal=goal,
                run_task_fn=run_task_fn,
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
        run_task_fn: Callable[[str], Awaitable[None]] | None,
    ) -> None:
        """Create a workflow task for this improvement and start monitoring it."""
        try:
            request = TaskCreateRequest(
                goal=goal,
                repository=self.settings.self_improvement_target_repo,
                local_repo_path=self.settings.self_improvement_local_repo_path,
                base_branch=self.settings.default_base_branch,
                allow_repository_modifications=True,
                auto_deploy_staging=self.settings.self_improvement_deploy_after_success,
                metadata={
                    "self_improvement_cycle_id": cycle_id,
                    "worker_project_label": "Self-Improvement-Zyklus",
                    "allow_repository_modifications": True,
                    "deployment_target": "self",
                },
            )
            summary = self.task_service.create_task(request)
            task_id = summary.id

            self._update(cycle_id, task_id=task_id)

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

    async def _monitor_task(
        self,
        cycle_id: str,
        task_id: str,
        poll_interval: float = 30.0,
        max_wait_hours: float = 4.0,
        run_task_fn: Callable[[str], Awaitable[None]] | None = None,
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
            if task.branch_name:
                update_kwargs["branch_name"] = task.branch_name
            if task.latest_error:
                update_kwargs["latest_error"] = task.latest_error
            if update_kwargs:
                self._update(cycle_id, **update_kwargs)

            if task.status == TaskStatus.FAILED:
                cycle = self.get_cycle(cycle_id)
                if cycle is None:
                    return

                new_retry = cycle.retry_count + 1
                if new_retry >= cycle.max_retries:
                    self._update(
                        cycle_id,
                        status=CycleStatus.FAILED.value,
                        completed_at=datetime.now(UTC),
                        retry_count=new_retry,
                        latest_error=(
                            f"Task fehlgeschlagen nach {new_retry} Versuch(en). "
                            f"Letzter Fehler: {task.latest_error or 'unbekannt'}"
                        ),
                    )
                    return

                # Create a fresh task for the retry (the old failed task cannot be restarted).
                try:
                    goal = cycle.goal or ""
                    retry_request = TaskCreateRequest(
                        goal=goal,
                        repository=self.settings.self_improvement_target_repo,
                        local_repo_path=self.settings.self_improvement_local_repo_path,
                        base_branch=self.settings.default_base_branch,
                        allow_repository_modifications=True,
                        auto_deploy_staging=self.settings.self_improvement_deploy_after_success,
                        metadata={
                            "self_improvement_cycle_id": cycle_id,
                            "worker_project_label": "Self-Improvement-Zyklus",
                            "allow_repository_modifications": True,
                            "retry_attempt": new_retry,
                            "deployment_target": "self",
                        },
                    )
                    retry_summary = self.task_service.create_task(retry_request)
                    current_task_id = retry_summary.id
                    self._update(
                        cycle_id,
                        retry_count=new_retry,
                        status=CycleStatus.IMPLEMENTING.value,
                        task_id=current_task_id,
                        latest_error=task.latest_error,
                    )
                    if run_task_fn is not None:
                        asyncio.create_task(run_task_fn(current_task_id))
                except Exception as retry_exc:
                    self._update(
                        cycle_id,
                        status=CycleStatus.FAILED.value,
                        completed_at=datetime.now(UTC),
                        latest_error=f"Retry-Task-Erstellung fehlgeschlagen: {retry_exc}",
                    )
                    return
                continue

            if task.status in {TaskStatus.APPROVAL_REQUIRED}:
                self._update(cycle_id, status=CycleStatus.AWAITING_MANUAL_REVIEW.value)
                continue

            if task.status == TaskStatus.DONE or task.status == TaskStatus.PR_CREATED:
                outputs = task.worker_results
                coding_out = outputs.get("coding", {}).get("outputs", {})
                testing_out = outputs.get("tester", {}).get("outputs", {})
                deploy_out = outputs.get("deploy", {}).get("outputs", {})
                qa_out = outputs.get("qa", {}).get("outputs", {})
                self._update(
                    cycle_id,
                    status=CycleStatus.COMPLETED.value,
                    completed_at=datetime.now(UTC),
                    branch_name=coding_out.get("branch_name") or task.branch_name,
                    changed_files_json=coding_out.get("changed_files", []),
                    test_results_json=testing_out or {},
                    deploy_result_json=deploy_out or {},
                    healthcheck_result_json=qa_out or {},
                )
                return

        # Deadline exceeded
        self._update(
            cycle_id,
            status=CycleStatus.FAILED.value,
            completed_at=datetime.now(UTC),
            latest_error=f"Ueberwachung abgebrochen nach {max_wait_hours:.1f}h (Timeout).",
        )
