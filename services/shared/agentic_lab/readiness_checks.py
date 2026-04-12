"""
Purpose: Modular readiness and diagnostics checks for the operator dashboard.
Input/Output: The runner returns one structured report with independent check results so the API
and UI can keep working even when individual checks fail or time out.
Important invariants: No single check may crash the whole report, slow local hardware must be
treated as a diagnosable state instead of a generic outage, and operator hints stay concrete.
How to debug: Start with one failed check in the final report, then inspect the `detail`, `hint`,
and `raw_value` fields before touching the broader system.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

import httpx

from services.shared.agentic_lab.config import Settings, inspect_secret_file
from services.shared.agentic_lab.db import TaskRecord
from services.shared.agentic_lab.readiness_models import (
    ReadinessCategorySummary,
    ReadinessCheckResult,
    ReadinessCheckStatus,
    ReadinessMode,
    ReadinessRecommendation,
    ReadinessReport,
    ReadinessSeverity,
    ReadinessSummary,
)
from services.shared.agentic_lab.repo_tools import prepare_git_environment
from services.shared.agentic_lab.schemas import TaskStatus

CATEGORY_LABELS: dict[str, str] = {
    "backend": "Backend / Core",
    "workers": "Worker",
    "llm": "LLM / Modelle",
    "git": "Git / Workspace",
    "secrets": "Secrets / Konfiguration",
    "integrations": "Integrationen",
    "performance": "Performance / Stabilitaet",
}

WORKER_TARGETS: tuple[tuple[str, str, str], ...] = (
    ("requirements", "Requirements Worker", "requirements_worker_url"),
    ("cost", "Cost Worker", "cost_worker_url"),
    ("human_resources", "Human Resources Worker", "human_resources_worker_url"),
    ("research", "Research Worker", "research_worker_url"),
    ("architecture", "Architecture Worker", "architecture_worker_url"),
    ("data", "Data Worker", "data_worker_url"),
    ("ux", "UX Worker", "ux_worker_url"),
    ("coding", "Coding Worker", "coding_worker_url"),
    ("reviewer", "Reviewer Worker", "reviewer_worker_url"),
    ("tester", "Test Worker", "test_worker_url"),
    ("security", "Security Worker", "security_worker_url"),
    ("validation", "Validation Worker", "validation_worker_url"),
    ("documentation", "Documentation Worker", "documentation_worker_url"),
    ("github", "GitHub Worker", "github_worker_url"),
    ("deploy", "Deploy Worker", "deploy_worker_url"),
    ("qa", "QA Worker", "qa_worker_url"),
    ("memory", "Memory Worker", "memory_worker_url"),
)

ACTIVE_TASK_STATUSES = {
    TaskStatus.REQUIREMENTS.value,
    TaskStatus.RESOURCE_PLANNING.value,
    TaskStatus.RESEARCHING.value,
    TaskStatus.ARCHITECTING.value,
    TaskStatus.CODING.value,
    TaskStatus.REVIEWING.value,
    TaskStatus.TESTING.value,
    TaskStatus.SECURITY_REVIEW.value,
    TaskStatus.VALIDATING.value,
    TaskStatus.DOCUMENTING.value,
    TaskStatus.PR_CREATED.value,
    TaskStatus.STAGING_DEPLOYED.value,
    TaskStatus.QA_PENDING.value,
    TaskStatus.MEMORY_UPDATING.value,
}

WORKER_STATE_GROUPS = {
    "running": "aktuell laufend",
    "waiting": "wartend",
    "blocked": "fehlerhaft",
    "failed": "fehlerhaft",
    "complete": "aktiv / gesund",
    "queued": "wartend",
    "idle": "nicht benoetigt / inaktiv",
    "skipped": "nicht benoetigt / inaktiv",
}


@dataclass(slots=True)
class ReadinessServices:
    """Optional service handles injected from the orchestrator so deep checks can reuse runtime state."""

    task_service: Any | None = None
    worker_governance_service: Any | None = None
    policy_service: Any | None = None
    search_provider_service: Any | None = None


@dataclass(slots=True)
class ReadinessContext:
    """Shared runtime context for all checks."""

    settings: Settings
    mode: ReadinessMode
    services: ReadinessServices = field(default_factory=ReadinessServices)
    runtime_insights: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ReadinessCheckDefinition:
    """Declarative metadata for one concrete readiness check."""

    id: str
    category: str
    name: str
    runner: Callable[[ReadinessContext], Awaitable[dict[str, Any]]]
    depends_on: tuple[str, ...] = ()
    deep_only: bool = False


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _trim_text(value: Any, limit: int = 320) -> str:
    """Keep operator-visible error text compact without hiding the core cause."""

    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit - 1].rstrip()}…"


def _serialize_exception(exc: Exception) -> str:
    """Render exceptions consistently so the UI can show technical context without a traceback wall."""

    return f"{type(exc).__name__}: {_trim_text(exc, 600)}"


def _payload(
    status: ReadinessCheckStatus,
    severity: ReadinessSeverity,
    message: str,
    *,
    detail: str = "",
    hint: str = "",
    target: str | None = None,
    raw_value: Any | None = None,
) -> dict[str, Any]:
    """Create one normalized intermediate payload that the safe runner turns into a typed result."""

    return {
        "status": status,
        "severity": severity,
        "message": message,
        "detail": detail,
        "hint": hint,
        "target": target,
        "raw_value": raw_value,
    }


def _status_rank(status: ReadinessCheckStatus) -> int:
    order = {
        ReadinessCheckStatus.FAIL: 4,
        ReadinessCheckStatus.WARNING: 3,
        ReadinessCheckStatus.RUNNING: 2,
        ReadinessCheckStatus.OK: 1,
        ReadinessCheckStatus.SKIPPED: 0,
    }
    return order[status]


def _overall_status(checks: list[ReadinessCheckResult]) -> ReadinessCheckStatus:
    if any(check.status is ReadinessCheckStatus.FAIL for check in checks):
        return ReadinessCheckStatus.FAIL
    if any(check.status is ReadinessCheckStatus.WARNING for check in checks):
        return ReadinessCheckStatus.WARNING
    if any(check.status is ReadinessCheckStatus.RUNNING for check in checks):
        return ReadinessCheckStatus.WARNING
    if any(check.status is ReadinessCheckStatus.OK for check in checks):
        return ReadinessCheckStatus.OK
    return ReadinessCheckStatus.SKIPPED


def _build_summary(checks: list[ReadinessCheckResult]) -> ReadinessSummary:
    counts = Counter(check.status for check in checks)
    return ReadinessSummary(
        total=len(checks),
        ok=counts.get(ReadinessCheckStatus.OK, 0),
        warning=counts.get(ReadinessCheckStatus.WARNING, 0),
        fail=counts.get(ReadinessCheckStatus.FAIL, 0),
        skipped=counts.get(ReadinessCheckStatus.SKIPPED, 0),
        running=counts.get(ReadinessCheckStatus.RUNNING, 0),
    )


def _build_category_summaries(checks: list[ReadinessCheckResult]) -> list[ReadinessCategorySummary]:
    grouped: dict[str, list[ReadinessCheckResult]] = defaultdict(list)
    for check in checks:
        grouped[check.category].append(check)

    summaries: list[ReadinessCategorySummary] = []
    for category in CATEGORY_LABELS:
        members = grouped.get(category, [])
        summary = _build_summary(members)
        status = _overall_status(members)
        summaries.append(
            ReadinessCategorySummary(
                id=category,
                label=CATEGORY_LABELS.get(category, category),
                status=status,
                total=summary.total,
                ok=summary.ok,
                warning=summary.warning,
                fail=summary.fail,
                skipped=summary.skipped,
                running=summary.running,
            )
        )
    return summaries


def _headline_and_message(status: ReadinessCheckStatus, summary: ReadinessSummary) -> tuple[bool, str, str]:
    """Return the headline and short operator message for the dashboard hero area."""

    if status is ReadinessCheckStatus.OK:
        return True, "Bereit", "Die Kernchecks sind gruen. Das System wirkt fuer neue Aufgaben einsatzbereit."
    if status is ReadinessCheckStatus.WARNING:
        return True, "Bereit mit Warnungen", (
            f"Es gibt {summary.warning} Warnung(en). Das System ist nutzbar, aber einzelne Teile sind langsam, "
            "degradiert oder optional nicht sauber konfiguriert."
        )
    if status is ReadinessCheckStatus.FAIL:
        return False, "Nicht bereit", (
            f"Es gibt {summary.fail} kritische oder harte Fehler. Neue Aufgaben koennen scheitern, bis die "
            "markierten Ursachen behoben sind."
        )
    return False, "Pruefung unvollstaendig", "Es konnten keine belastbaren Check-Ergebnisse erzeugt werden."


def _priority_for_check(check: ReadinessCheckResult) -> int:
    base = {
        ReadinessSeverity.CRITICAL: 100,
        ReadinessSeverity.HIGH: 80,
        ReadinessSeverity.MEDIUM: 60,
        ReadinessSeverity.LOW: 40,
        ReadinessSeverity.INFO: 20,
    }[check.severity]
    if check.status is ReadinessCheckStatus.FAIL:
        return base + 20
    if check.status is ReadinessCheckStatus.WARNING:
        return base + 10
    return base


def _build_recommendations(checks: list[ReadinessCheckResult]) -> list[ReadinessRecommendation]:
    """Turn actionable fails and warnings into a compact to-do list for operators."""

    recommendations: list[ReadinessRecommendation] = []
    seen: set[tuple[str, str]] = set()
    for check in checks:
        if check.status not in {ReadinessCheckStatus.FAIL, ReadinessCheckStatus.WARNING}:
            continue
        if not check.hint:
            continue
        key = (check.category, check.hint)
        if key in seen:
            continue
        seen.add(key)
        recommendations.append(
            ReadinessRecommendation(
                id=f"recommendation-{check.id}",
                priority=_priority_for_check(check),
                category=check.category,
                title=check.name,
                message=check.hint,
                related_check_ids=[check.id],
            )
        )
    recommendations.sort(key=lambda item: item.priority, reverse=True)
    return recommendations[:8]


def _read_secret_file_state(path: Path | None, *, raw_env_value: str | None = None) -> dict[str, Any]:
    """Return a small secret-file probe without reading secret contents into the report."""

    return inspect_secret_file(path, raw_env_value=raw_env_value).as_dict()


def _recent_task_records(task_service: Any | None) -> list[TaskRecord]:
    """Load a small window of recent tasks so worker and performance checks can explain current state."""

    if task_service is None:
        return []
    with task_service.session() as session:
        return list(session.query(TaskRecord).order_by(TaskRecord.updated_at.desc()).limit(30).all())


def _parse_any_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if value in {None, ""}:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _collect_runtime_insights(task_service: Any | None) -> dict[str, Any]:
    """Summarize recent task rows into worker snapshots and stability counters."""

    records = _recent_task_records(task_service)
    worker_snapshot: dict[str, dict[str, Any]] = {}
    failure_counts: Counter[str] = Counter()
    timeout_counts: Counter[str] = Counter()
    active_tasks: list[dict[str, Any]] = []

    for record in records:
        metadata = dict(record.metadata_json or {})
        worker_progress = dict(metadata.get("worker_progress") or {})

        if record.status in ACTIVE_TASK_STATUSES:
            active_tasks.append(
                {
                    "task_id": record.id,
                    "repository": record.repository,
                    "goal": _trim_text(record.goal, 160),
                    "status": record.status,
                    "updated_at": record.updated_at.isoformat(),
                }
            )

        for worker_name, progress in worker_progress.items():
            if not isinstance(progress, dict):
                continue
            updated_at = _parse_any_timestamp(progress.get("updated_at")) or record.updated_at
            previous = worker_snapshot.get(worker_name)
            previous_updated = _parse_any_timestamp(previous.get("updated_at")) if previous else None
            if previous_updated is not None and previous_updated >= updated_at:
                continue
            worker_snapshot[worker_name] = {
                "worker_name": worker_name,
                "task_id": record.id,
                "repository": record.repository,
                "goal": _trim_text(record.goal, 160),
                "task_status": record.status,
                "state": str(progress.get("state") or "idle"),
                "current_action": str(progress.get("current_action") or ""),
                "current_instruction": str(
                    progress.get("current_instruction")
                    or progress.get("current_prompt_summary")
                    or progress.get("current_step")
                    or ""
                ),
                "waiting_for": str(progress.get("waiting_for") or ""),
                "blocked_by": str(progress.get("blocked_by") or ""),
                "next_worker": str(progress.get("next_worker") or ""),
                "last_result_summary": str(progress.get("last_result_summary") or ""),
                "progress_message": str(progress.get("progress_message") or ""),
                "last_error": str(progress.get("last_error") or record.latest_error or ""),
                "updated_at": updated_at.isoformat(),
                "started_at": str(progress.get("started_at") or ""),
                "elapsed_seconds": progress.get("elapsed_seconds"),
                "event_kind": str(progress.get("event_kind") or ""),
                "service_url": str(progress.get("service_url") or ""),
                "model_route": str(progress.get("model_route") or ""),
            }

        for worker_name, result in dict(record.worker_results_json or {}).items():
            if not isinstance(result, dict):
                continue
            errors = [str(item) for item in result.get("errors", []) if str(item).strip()]
            if errors:
                failure_counts[worker_name] += 1
            joined = " ".join(errors + [str(record.latest_error or "")]).lower()
            if "timeout" in joined or "timed out" in joined:
                timeout_counts[worker_name] += 1
            if "http 500" in joined or "internal server error" in joined:
                failure_counts[worker_name] += 1

    return {
        "worker_snapshot": worker_snapshot,
        "failure_counts": dict(failure_counts),
        "timeout_counts": dict(timeout_counts),
        "active_tasks": active_tasks,
        "recent_task_count": len(records),
    }


def _openai_endpoint(base_url: str, suffix: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/v1"):
        return f"{normalized}{suffix}"
    return f"{normalized}/v1{suffix}"


async def _http_get(
    url: str,
    *,
    timeout: httpx.Timeout,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.get(url, headers=headers)


async def _http_post_json(
    url: str,
    *,
    timeout: httpx.Timeout,
    json_payload: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.post(url, json=json_payload, headers=headers)


def _http_timeout(settings: Settings, *, deep: bool = False) -> httpx.Timeout:
    seconds = (
        settings.readiness_http_deep_timeout_seconds if deep else settings.readiness_http_fast_timeout_seconds
    )
    return httpx.Timeout(connect=min(seconds, 10.0), read=seconds, write=min(seconds, 15.0), pool=10.0)


def _slow_duration_warning(ctx: ReadinessContext, seconds: float) -> bool:
    return seconds >= ctx.settings.readiness_slow_warning_seconds


async def _run_check(definition: ReadinessCheckDefinition, ctx: ReadinessContext) -> ReadinessCheckResult:
    """Run one check defensively and always return a typed result."""

    started_at = _utc_now()
    started_perf = perf_counter()

    if definition.deep_only and ctx.mode is ReadinessMode.QUICK:
        finished_at = _utc_now()
        return ReadinessCheckResult(
            id=definition.id,
            category=definition.category,
            name=definition.name,
            status=ReadinessCheckStatus.SKIPPED,
            severity=ReadinessSeverity.INFO,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=round((perf_counter() - started_perf) * 1000, 1),
            message="Nur im Tiefencheck aktiv.",
            hint="Starte den Tiefencheck, wenn du Netz-, Git- oder Modell-Details genauer pruefen willst.",
            depends_on=list(definition.depends_on),
        )

    try:
        payload = await definition.runner(ctx)
    except Exception as exc:  # pragma: no cover - safety net verified via tests through monkeypatching
        payload = _payload(
            ReadinessCheckStatus.FAIL,
            ReadinessSeverity.HIGH,
            f"{definition.name} ist intern fehlgeschlagen.",
            detail=_serialize_exception(exc),
            hint="Pruefe die Web-UI- oder Orchestrator-Logs. Andere Checks wurden trotzdem weiter ausgewertet.",
        )

    finished_at = _utc_now()
    return ReadinessCheckResult(
        id=definition.id,
        category=definition.category,
        name=definition.name,
        status=payload["status"],
        severity=payload["severity"],
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=round((perf_counter() - started_perf) * 1000, 1),
        message=payload["message"],
        detail=payload.get("detail", ""),
        hint=payload.get("hint", ""),
        target=payload.get("target"),
        raw_value=payload.get("raw_value"),
        depends_on=list(definition.depends_on),
    )


async def _backend_orchestrator_health(ctx: ReadinessContext) -> dict[str, Any]:
    url = f"{ctx.settings.orchestrator_internal_url.rstrip('/')}/health"
    response = await _http_get(url, timeout=_http_timeout(ctx.settings))
    if response.status_code < 400:
        return _payload(
            ReadinessCheckStatus.OK,
            ReadinessSeverity.INFO,
            "Orchestrator antwortet normal.",
            target=url,
            raw_value={"http_status": response.status_code},
        )
    return _payload(
        ReadinessCheckStatus.FAIL,
        ReadinessSeverity.CRITICAL,
        f"Orchestrator /health meldet HTTP {response.status_code}.",
        detail=_trim_text(response.text, 260),
        hint="Pruefe `docker compose logs --tail=200 orchestrator`.",
        target=url,
        raw_value={"http_status": response.status_code},
    )


async def _backend_web_ui_health(ctx: ReadinessContext) -> dict[str, Any]:
    url = f"{ctx.settings.web_ui_internal_url.rstrip('/')}/health"
    try:
        response = await _http_get(url, timeout=_http_timeout(ctx.settings))
    except httpx.HTTPError as exc:
        return _payload(
            ReadinessCheckStatus.FAIL,
            ReadinessSeverity.HIGH,
            "Web-UI-Health ist nicht erreichbar.",
            detail=_serialize_exception(exc),
            hint="Pruefe `docker compose logs --tail=200 web-ui` und die interne WEB_UI_INTERNAL_URL.",
            target=url,
        )
    if response.status_code < 400:
        return _payload(
            ReadinessCheckStatus.OK,
            ReadinessSeverity.INFO,
            "Web-UI antwortet normal.",
            target=url,
            raw_value={"http_status": response.status_code},
        )
    return _payload(
        ReadinessCheckStatus.FAIL,
        ReadinessSeverity.HIGH,
        f"Web-UI /health meldet HTTP {response.status_code}.",
        detail=_trim_text(response.text, 260),
        hint="Pruefe `docker compose logs --tail=200 web-ui`.",
        target=url,
        raw_value={"http_status": response.status_code},
    )


def _database_probe(ctx: ReadinessContext) -> dict[str, Any]:
    db_path = ctx.settings.orchestrator_db_path
    if not db_path.exists():
        return _payload(
            ReadinessCheckStatus.FAIL,
            ReadinessSeverity.HIGH,
            "Die SQLite-Datenbankdatei fehlt.",
            hint="Pruefe DATA_DIR, ORCHESTRATOR_DB_PATH und die Schreibrechte des Datenverzeichnisses.",
            target=str(db_path),
        )
    try:
        connection = sqlite3.connect(str(db_path))
        try:
            cursor = connection.execute("select 1")
            cursor.fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        return _payload(
            ReadinessCheckStatus.FAIL,
            ReadinessSeverity.HIGH,
            "Datenbank ist vorhanden, aber nicht lesbar oder konsistent ansprechbar.",
            detail=_serialize_exception(exc),
            hint="Pruefe Dateirechte, freien Speicher und ob mehrere Services dieselbe DB-Datei blockieren.",
            target=str(db_path),
        )
    return _payload(
        ReadinessCheckStatus.OK,
        ReadinessSeverity.INFO,
        "SQLite-Datenbank ist erreichbar.",
        target=str(db_path),
    )


async def _backend_database(ctx: ReadinessContext) -> dict[str, Any]:
    return await asyncio.to_thread(_database_probe, ctx)


def _task_service_probe(ctx: ReadinessContext) -> dict[str, Any]:
    task_service = ctx.services.task_service
    if task_service is None:
        return _payload(
            ReadinessCheckStatus.SKIPPED,
            ReadinessSeverity.INFO,
            "Task-Service wurde fuer diese Pruefung nicht injiziert.",
            hint="Im Orchestrator ist dieser Check aktiv. In isolierten Tests kann er bewusst uebersprungen sein.",
        )
    tasks = task_service.list_tasks()
    return _payload(
        ReadinessCheckStatus.OK,
        ReadinessSeverity.INFO,
        f"Task-Service antwortet. {len(tasks)} Aufgabe(n) sind aktuell bekannt.",
        raw_value={"task_count": len(tasks)},
    )


async def _backend_task_service(ctx: ReadinessContext) -> dict[str, Any]:
    return await asyncio.to_thread(_task_service_probe, ctx)


def _suggestions_probe(ctx: ReadinessContext) -> dict[str, Any]:
    service = ctx.services.worker_governance_service
    if service is None:
        return _payload(
            ReadinessCheckStatus.SKIPPED,
            ReadinessSeverity.INFO,
            "Suggestion-Registry wurde fuer diese Pruefung nicht injiziert.",
        )
    registry = service.load_suggestion_registry()
    return _payload(
        ReadinessCheckStatus.OK,
        ReadinessSeverity.INFO,
        f"Suggestion-Registry ist lesbar. {len(registry.suggestions)} Vorschlag/Vorschlaege gespeichert.",
        raw_value={"suggestion_count": len(registry.suggestions)},
    )


async def _backend_suggestions_registry(ctx: ReadinessContext) -> dict[str, Any]:
    return await asyncio.to_thread(_suggestions_probe, ctx)


def _repository_policy_probe(ctx: ReadinessContext) -> dict[str, Any]:
    service = ctx.services.policy_service
    if service is None:
        return _payload(
            ReadinessCheckStatus.SKIPPED,
            ReadinessSeverity.INFO,
            "Repository-Policy-Service wurde fuer diese Pruefung nicht injiziert.",
        )
    policy = service.load()
    return _payload(
        ReadinessCheckStatus.OK,
        ReadinessSeverity.INFO,
        f"Repository-Policy ist lesbar. {len(policy.allowed_repositories)} Repository-Eintrag/Eintraege freigegeben.",
        raw_value={"allowed_repositories": policy.allowed_repositories},
    )


async def _backend_repository_policy(ctx: ReadinessContext) -> dict[str, Any]:
    return await asyncio.to_thread(_repository_policy_probe, ctx)


async def _worker_health(ctx: ReadinessContext, worker_name: str, display_name: str, url: str) -> dict[str, Any]:
    target = f"{url.rstrip('/')}/health"
    snapshot = dict(ctx.runtime_insights.get("worker_snapshot", {}).get(worker_name, {}))
    timeout = httpx.Timeout(
        connect=min(ctx.settings.readiness_http_fast_timeout_seconds, 10.0),
        read=ctx.settings.readiness_worker_smoke_timeout_seconds,
        write=10.0,
        pool=10.0,
    )
    try:
        response = await _http_get(target, timeout=timeout)
        if response.status_code >= 400:
            return _payload(
                ReadinessCheckStatus.FAIL,
                ReadinessSeverity.HIGH,
                f"{display_name} antwortet mit HTTP {response.status_code}.",
                detail=_trim_text(response.text, 260),
                hint=f"Pruefe `docker compose logs --tail=200 {worker_name}-worker` oder den Container dieses Workers.",
                target=target,
                raw_value=snapshot | {"http_status": response.status_code},
            )
    except httpx.TimeoutException as exc:
        return _payload(
            ReadinessCheckStatus.FAIL,
            ReadinessSeverity.HIGH,
            f"{display_name} hat innerhalb des Fast-Timeouts nicht geantwortet.",
            detail=_serialize_exception(exc),
            hint="Ein haeufiger Grund sind langsame Containerstarts, haengende lokale Modelle oder wiederholte Worker-Fehler.",
            target=target,
            raw_value=snapshot,
        )
    except httpx.HTTPError as exc:
        return _payload(
            ReadinessCheckStatus.FAIL,
            ReadinessSeverity.HIGH,
            f"{display_name} ist per /health nicht erreichbar.",
            detail=_serialize_exception(exc),
            hint=f"Pruefe `docker compose ps` und die URL {target}.",
            target=target,
            raw_value=snapshot,
        )

    state = str(snapshot.get("state") or "idle")
    waiting_for = str(snapshot.get("waiting_for") or "")
    current_action = str(snapshot.get("current_action") or snapshot.get("current_instruction") or "")
    elapsed_seconds = snapshot.get("elapsed_seconds")
    last_error = str(snapshot.get("last_error") or "")
    runtime_target = str(snapshot.get("service_url") or target)

    if state == "failed" or last_error:
        return _payload(
            ReadinessCheckStatus.WARNING,
            ReadinessSeverity.MEDIUM,
            f"{display_name} ist erreichbar, aber der letzte bekannte Lauf endete mit einem Fehler.",
            detail=last_error or "Es liegt ein fehlgeschlagener Worker-Zustand vor, aber kein Detailtext wurde gespeichert.",
            hint="Sieh in die Task-Detailseite oder die Worker-Logs, um den konkreten Fehlerlauf nachzuvollziehen.",
            target=runtime_target,
            raw_value=snapshot | {"state_group": WORKER_STATE_GROUPS.get(state, "aktiv / gesund")},
        )
    if state == "running":
        detail = current_action or "Kein aktueller Arbeitsauftrag gespeichert."
        if isinstance(elapsed_seconds, (int, float)) and _slow_duration_warning(ctx, float(elapsed_seconds)):
            return _payload(
                ReadinessCheckStatus.WARNING,
                ReadinessSeverity.LOW,
                f"{display_name} arbeitet noch. Die aktuelle Laufzeit ist fuer lokale Hardware bereits auffaellig lang.",
                detail=detail,
                hint=(
                    "Wenn das Modell lokal laeuft, ist das oft kein harter Fehler. "
                    "Beobachte last_progress_at, waiting_for und die Worker-Logs."
                ),
                target=runtime_target,
                raw_value=snapshot | {"state_group": WORKER_STATE_GROUPS.get(state, "aktuell laufend")},
            )
        return _payload(
            ReadinessCheckStatus.OK,
            ReadinessSeverity.INFO,
            f"{display_name} arbeitet aktuell.",
            detail=detail,
            target=runtime_target,
            raw_value=snapshot | {"state_group": WORKER_STATE_GROUPS.get(state, "aktuell laufend")},
        )
    if state in {"waiting", "queued", "blocked"}:
        wait_text = waiting_for or "einen anderen Schritt oder Dienst"
        severity = ReadinessSeverity.LOW if state != "blocked" else ReadinessSeverity.MEDIUM
        status = ReadinessCheckStatus.WARNING if state == "blocked" else ReadinessCheckStatus.OK
        return _payload(
            status,
            severity,
            f"{display_name} ist erreichbar und wartet aktuell auf {wait_text}.",
            detail=current_action or snapshot.get("progress_message") or "Kein genauer Wartegrund gespeichert.",
            hint=(
                "Wenn die Wartezeit stark ansteigt, pruefe Modell-Inferenz, Approval-Gates oder vorangehende Worker."
                if state != "blocked"
                else "Der Worker ist nicht komplett down, aber in einem blockierten Zustand. Pruefe last_error und die vorherige Stage."
            ),
            target=runtime_target,
            raw_value=snapshot | {"state_group": WORKER_STATE_GROUPS.get(state, "wartend")},
        )
    return _payload(
        ReadinessCheckStatus.OK,
        ReadinessSeverity.INFO,
        f"{display_name} ist gesund und derzeit nicht aktiv geblockt.",
        detail=current_action or "Kein aktiver Lauf gespeichert.",
        target=runtime_target,
        raw_value=snapshot | {"state_group": WORKER_STATE_GROUPS.get(state, "nicht benoetigt / inaktiv")},
    )


async def _llm_default_provider(ctx: ReadinessContext) -> dict[str, Any]:
    providers = ctx.settings.model_provider_configs()
    provider_name = ctx.settings.default_model_provider.strip().lower()
    provider = providers.get(provider_name)
    if provider is None:
        return _payload(
            ReadinessCheckStatus.FAIL,
            ReadinessSeverity.CRITICAL,
            f"DEFAULT_MODEL_PROVIDER `{ctx.settings.default_model_provider}` ist unbekannt.",
            hint="Nutze einen vorhandenen Provider wie `mistral` oder `qwen` und prüfe die .env-Datei.",
            raw_value={"configured_provider": ctx.settings.default_model_provider, "available": sorted(providers)},
        )
    if not provider.get("base_url") or not provider.get("model_name"):
        return _payload(
            ReadinessCheckStatus.FAIL,
            ReadinessSeverity.HIGH,
            f"Der Default-Provider `{provider_name}` ist konfiguriert, aber Base-URL oder Modellname fehlen.",
            hint="Pruefe BASE_URL- und MODEL_NAME-Werte des Default-Providers.",
            raw_value={"provider": provider_name, **provider},
        )
    return _payload(
        ReadinessCheckStatus.OK,
        ReadinessSeverity.INFO,
        f"DEFAULT_MODEL_PROVIDER zeigt auf `{provider_name}`.",
        raw_value={"provider": provider_name, **provider},
    )


async def _llm_models_endpoint(ctx: ReadinessContext, provider_name: str, provider: dict[str, Any]) -> dict[str, Any]:
    base_url = str(provider.get("base_url") or "")
    model_name = str(provider.get("model_name") or "")
    target = _openai_endpoint(base_url, "/models") if base_url else None
    if not base_url or not model_name:
        return _payload(
            ReadinessCheckStatus.SKIPPED,
            ReadinessSeverity.INFO,
            f"{provider_name} ist nicht vollstaendig konfiguriert.",
            hint="Setze Base-URL und Modellnamen, wenn dieser Provider aktiv genutzt werden soll.",
            target=target,
            raw_value={"provider": provider_name, **provider},
        )

    target = _openai_endpoint(base_url, "/models")
    headers = {"Authorization": f"Bearer {provider['api_key']}"} if provider.get("api_key") else None
    try:
        response = await _http_get(target, timeout=_http_timeout(ctx.settings), headers=headers)
    except httpx.TimeoutException as exc:
        return _payload(
            ReadinessCheckStatus.FAIL,
            ReadinessSeverity.HIGH,
            f"{provider_name}: /models hat nicht rechtzeitig geantwortet.",
            detail=_serialize_exception(exc),
            hint="Pruefe den lokalen Modell-Endpoint und erhoehe bei langsamer Hardware die Readiness-Timeouts.",
            target=target,
        )
    except httpx.HTTPError as exc:
        return _payload(
            ReadinessCheckStatus.FAIL,
            ReadinessSeverity.HIGH,
            f"{provider_name}: /models ist nicht erreichbar.",
            detail=_serialize_exception(exc),
            hint="Pruefe Base-URL, Reverse-Proxy und ob der Modellserver laeuft.",
            target=target,
        )

    if response.status_code >= 400:
        return _payload(
            ReadinessCheckStatus.FAIL,
            ReadinessSeverity.HIGH,
            f"{provider_name}: /models meldet HTTP {response.status_code}.",
            detail=_trim_text(response.text, 260),
            hint="Ein haeufiger Grund sind falsche OpenAI-kompatible Pfade oder nicht gestartete lokale Modelle.",
            target=target,
            raw_value={"http_status": response.status_code},
        )

    try:
        body = response.json()
    except ValueError as exc:
        return _payload(
            ReadinessCheckStatus.FAIL,
            ReadinessSeverity.HIGH,
            f"{provider_name}: /models antwortet, aber nicht mit gueltigem JSON.",
            detail=_serialize_exception(exc),
            hint="Pruefe, ob dein Endpoint wirklich OpenAI-kompatibel ist.",
            target=target,
        )

    models = [str(item.get("id")) for item in body.get("data", []) if isinstance(item, dict)]
    if model_name not in models:
        return _payload(
            ReadinessCheckStatus.WARNING,
            ReadinessSeverity.MEDIUM,
            f"{provider_name}: Endpoint antwortet, aber das gewuenschte Modell `{model_name}` wurde nicht gefunden.",
            detail=f"Bekannte Modelle: {', '.join(models[:12]) or 'keine'}",
            hint="Pruefe den Modellnamen oder lade das Modell zuerst in den lokalen Backend-Service.",
            target=target,
            raw_value={"models": models, "expected_model": model_name},
        )
    return _payload(
        ReadinessCheckStatus.OK,
        ReadinessSeverity.INFO,
        f"{provider_name}: Modell-Registry ist erreichbar und `{model_name}` ist vorhanden.",
        target=target,
        raw_value={"models": models, "expected_model": model_name},
    )


def _json_candidates(text: str) -> list[str]:
    """Return candidate strings to try parsing as JSON (handles markdown code-block wrappers)."""
    candidates = [text.strip()]
    if "```" in text:
        block = text.split("```")[1]
        candidates.append(block.replace("json", "", 1).strip())
    brace_start = text.find("{")
    brace_end = text.rfind("}")
    if brace_start != -1 and brace_end > brace_start:
        candidates.append(text[brace_start : brace_end + 1])
    return candidates


async def _llm_chat_smoke(ctx: ReadinessContext, provider_name: str, provider: dict[str, Any]) -> dict[str, Any]:
    base_url = str(provider.get("base_url") or "")
    model_name = str(provider.get("model_name") or "")
    if not base_url or not model_name:
        return _payload(
            ReadinessCheckStatus.SKIPPED,
            ReadinessSeverity.INFO,
            f"{provider_name}: Smoke-Test uebersprungen, weil Base-URL oder Modellname fehlen.",
        )

    target = _openai_endpoint(base_url, "/chat/completions")
    headers = {"Content-Type": "application/json"}
    if provider.get("api_key"):
        headers["Authorization"] = f"Bearer {provider['api_key']}"
    timeout = httpx.Timeout(
        connect=min(ctx.settings.readiness_http_fast_timeout_seconds, 10.0),
        read=ctx.settings.readiness_llm_smoke_timeout_seconds,
        write=15.0,
        pool=10.0,
    )
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": "Antworte ausschliesslich mit gueltigem JSON."},
            {"role": "user", "content": 'Gib exakt {"ok": true, "kind": "readiness"} zurueck.'},
        ],
        "temperature": 0.0,
        "max_tokens": 64,
        # Force JSON output at inference level (Ollama) and for OpenAI-compatible backends.
        "format": "json",
        "response_format": {"type": "json_object"},
    }

    started = perf_counter()
    try:
        response = await _http_post_json(target, timeout=timeout, json_payload=payload, headers=headers)
        elapsed_seconds = perf_counter() - started
    except httpx.TimeoutException as exc:
        return _payload(
            ReadinessCheckStatus.WARNING,
            ReadinessSeverity.MEDIUM,
            f"{provider_name}: Chat-Smoke-Test lief in den Timeout.",
            detail=_serialize_exception(exc),
            hint=(
                "Der Modellserver ist eventuell erreichbar, aber fuer tiefe Readiness-Checks oder grosse Workflows sehr langsam. "
                "Erhoehe bei Bedarf READINESS_LLM_SMOKE_TIMEOUT_SECONDS, LLM_READ_TIMEOUT_SECONDS und WORKER_STAGE_TIMEOUT_SECONDS."
            ),
            target=target,
            raw_value={"expected_model": model_name},
        )
    except httpx.HTTPError as exc:
        return _payload(
            ReadinessCheckStatus.FAIL,
            ReadinessSeverity.HIGH,
            f"{provider_name}: Chat-Smoke-Test ist fehlgeschlagen.",
            detail=_serialize_exception(exc),
            hint="Pruefe Modellserver, Routing und Auth-Header.",
            target=target,
        )

    if response.status_code >= 400:
        return _payload(
            ReadinessCheckStatus.FAIL,
            ReadinessSeverity.HIGH,
            f"{provider_name}: Chat-Smoke-Test meldet HTTP {response.status_code}.",
            detail=_trim_text(response.text, 260),
            hint="Pruefe Modellname, API-Key und ob der Endpoint Chat-Completions unterstuetzt.",
            target=target,
        )

    try:
        body = response.json()
        content = str(body["choices"][0]["message"]["content"]).strip()
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        return _payload(
            ReadinessCheckStatus.WARNING,
            ReadinessSeverity.MEDIUM,
            f"{provider_name}: Chat-Smoke-Test antwortet, aber das Antwortformat ist unerwartet.",
            detail=_serialize_exception(exc),
            hint="Das Modell ist moeglicherweise nutzbar, liefert aber keine stabile OpenAI-kompatible Struktur.",
            target=target,
        )

    json_valid = False
    for candidate in _json_candidates(content):
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict) and parsed.get("ok") is True:
                json_valid = True
                break
        except json.JSONDecodeError:
            continue

    raw_value = {"response_preview": _trim_text(content, 200), "elapsed_seconds": round(elapsed_seconds, 1)}
    if not json_valid:
        return _payload(
            ReadinessCheckStatus.WARNING,
            ReadinessSeverity.MEDIUM,
            f"{provider_name}: Chat-Smoke-Test antwortet, aber nicht mit dem erwarteten JSON.",
            detail=f"Antwortvorschau: {_trim_text(content, 220)}",
            hint="Wenn strukturierte Worker-Outputs erwartet werden, ist das ein echter Hinweis auf Prompt- oder Modellstabilitaet.",
            target=target,
            raw_value=raw_value,
        )
    if _slow_duration_warning(ctx, elapsed_seconds):
        return _payload(
            ReadinessCheckStatus.WARNING,
            ReadinessSeverity.LOW,
            f"{provider_name}: JSON-Smoke-Test erfolgreich, aber langsam ({elapsed_seconds:.1f}s).",
            hint="Fuer Homelab-Hardware ist das nicht automatisch kritisch. Beobachte nur, ob Worker-Timeouts passend konfiguriert sind.",
            target=target,
            raw_value=raw_value,
        )
    return _payload(
        ReadinessCheckStatus.OK,
        ReadinessSeverity.INFO,
        f"{provider_name}: JSON-Smoke-Test erfolgreich.",
        target=target,
        raw_value=raw_value,
    )


def _primary_repo_path(settings: Settings) -> Path:
    self_improvement_path = Path(settings.self_improvement_local_repo_path)
    if settings.self_improvement_enabled or self_improvement_path.exists():
        return self_improvement_path
    return Path(settings.default_local_repo_path)


def _filesystem_state(path: Path, *, must_be_writable: bool = False) -> dict[str, Any]:
    if not path.exists():
        return _payload(
            ReadinessCheckStatus.FAIL,
            ReadinessSeverity.HIGH,
            f"Pfad fehlt: {path}",
            hint="Pruefe Bind-Mounts, Host-Pfade und ob das Verzeichnis im Container vorhanden ist.",
            target=str(path),
        )
    readable = os.access(path, os.R_OK)
    writable = os.access(path, os.W_OK)
    if not readable:
        return _payload(
            ReadinessCheckStatus.FAIL,
            ReadinessSeverity.HIGH,
            f"Pfad ist nicht lesbar: {path}",
            hint="Pruefe Besitzer, PUID/PGID und Host-Berechtigungen.",
            target=str(path),
        )
    if must_be_writable and not writable:
        return _payload(
            ReadinessCheckStatus.FAIL,
            ReadinessSeverity.HIGH,
            f"Pfad ist lesbar, aber nicht schreibbar: {path}",
            hint="Task-Workspaces, Daten- und Report-Verzeichnisse brauchen Schreibrechte.",
            target=str(path),
            raw_value={"readable": readable, "writable": writable},
        )
    if not writable:
        return _payload(
            ReadinessCheckStatus.WARNING,
            ReadinessSeverity.LOW,
            f"Pfad ist nur lesbar: {path}",
            hint="Das kann fuer reine Lesepfade okay sein, blockiert aber spaetere Schreiboperationen.",
            target=str(path),
            raw_value={"readable": readable, "writable": writable},
        )
    return _payload(
        ReadinessCheckStatus.OK,
        ReadinessSeverity.INFO,
        f"Pfad ist lesbar und schreibbar: {path}",
        target=str(path),
        raw_value={"readable": readable, "writable": writable},
    )


async def _git_workspace_root(ctx: ReadinessContext) -> dict[str, Any]:
    return await asyncio.to_thread(_filesystem_state, ctx.settings.workspace_root, must_be_writable=True)


async def _git_task_workspace_root(ctx: ReadinessContext) -> dict[str, Any]:
    return await asyncio.to_thread(_filesystem_state, ctx.settings.effective_task_workspace_root, must_be_writable=True)


async def _git_repo_path(ctx: ReadinessContext) -> dict[str, Any]:
    return await asyncio.to_thread(_filesystem_state, _primary_repo_path(ctx.settings), must_be_writable=False)


def _run_git_command(repo_path: Path, args: list[str], settings: Settings) -> subprocess.CompletedProcess[str]:
    env = prepare_git_environment()
    return subprocess.run(
        ["git", *args],
        cwd=str(repo_path),
        env=env,
        text=True,
        capture_output=True,
        timeout=max(5, int(settings.readiness_git_timeout_seconds)),
        check=False,
    )


def _git_safe_directory_probe(ctx: ReadinessContext) -> dict[str, Any]:
    repo_path = _primary_repo_path(ctx.settings)
    if not repo_path.exists():
        return _payload(
            ReadinessCheckStatus.SKIPPED,
            ReadinessSeverity.INFO,
            "safe.directory wird uebersprungen, weil der Repo-Pfad fehlt.",
            target=str(repo_path),
        )
    env = prepare_git_environment(repo_path)
    gitconfig_path = env.get("GIT_CONFIG_GLOBAL")
    result = subprocess.run(
        ["git", "config", "--global", "--get-all", "safe.directory"],
        env=env,
        text=True,
        capture_output=True,
        timeout=max(5, int(ctx.settings.readiness_git_timeout_seconds)),
        check=False,
    )
    if result.returncode != 0:
        return _payload(
            ReadinessCheckStatus.FAIL,
            ReadinessSeverity.HIGH,
            "Globale Git-Konfiguration konnte nicht gelesen werden.",
            detail=_trim_text(result.stderr or result.stdout, 260),
            hint=(
                f"Pruefe HOME und GIT_CONFIG_GLOBAL. Erwartet wird ein beschreibbarer Runtime-Pfad wie "
                f"`{ctx.settings.runtime_home_dir}`."
            ),
            target=gitconfig_path,
        )
    entries = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    required = {str(repo_path.resolve())}
    git_dir = repo_path / ".git"
    if git_dir.exists():
        required.add(str(git_dir.resolve()))
    missing = sorted(path for path in required if path not in entries and "*" not in entries)
    if missing:
        return _payload(
            ReadinessCheckStatus.FAIL,
            ReadinessSeverity.HIGH,
            "safe.directory ist fuer das gemountete Repository noch nicht vollstaendig gesetzt.",
            detail=f"Fehlende Eintraege: {', '.join(missing)}",
            hint="Die Worker sollten Repo-Wurzel und gegebenenfalls `/.git` als safe.directory kennen.",
            target=gitconfig_path,
            raw_value={"known_entries": sorted(entries), "missing_entries": missing},
        )
    return _payload(
        ReadinessCheckStatus.OK,
        ReadinessSeverity.INFO,
        "safe.directory deckt das gemountete Repository ab.",
        target=gitconfig_path,
        raw_value={"known_entries": sorted(entries)},
    )


async def _git_safe_directory(ctx: ReadinessContext) -> dict[str, Any]:
    return await asyncio.to_thread(_git_safe_directory_probe, ctx)


def _git_status_probe(ctx: ReadinessContext) -> dict[str, Any]:
    repo_path = _primary_repo_path(ctx.settings)
    if not (repo_path / ".git").exists():
        return _payload(
            ReadinessCheckStatus.FAIL,
            ReadinessSeverity.HIGH,
            "Im konfigurierten Repo-Pfad wurde kein Git-Checkout gefunden.",
            hint="Pruefe SELF_IMPROVEMENT_LOCAL_REPO_PATH oder DEFAULT_LOCAL_REPO_PATH und die gemountete Arbeitskopie.",
            target=str(repo_path),
        )
    result = _run_git_command(repo_path, ["status", "--short"], ctx.settings)
    if result.returncode != 0:
        stderr = _trim_text(result.stderr or result.stdout, 320)
        hint = "Pruefe safe.directory, Ownership und ob das gemountete Repo lesbar ist."
        if "dubious ownership" in stderr.lower():
            hint = "Git meldet dubious ownership. safe.directory oder die Repo-Rechte sind noch nicht korrekt."
        return _payload(
            ReadinessCheckStatus.FAIL,
            ReadinessSeverity.HIGH,
            "git status ist fehlgeschlagen.",
            detail=stderr,
            hint=hint,
            target=str(repo_path),
        )
    dirty_lines = [line for line in result.stdout.splitlines() if line.strip()]
    if dirty_lines:
        return _payload(
            ReadinessCheckStatus.WARNING,
            ReadinessSeverity.MEDIUM,
            f"Das Basis-Repository ist nicht sauber ({len(dirty_lines)} geaenderte Datei(en)).",
            detail="\n".join(dirty_lines[:15]),
            hint="Das ist fuer die gemeinsame Arbeitskopie riskant. Task-Workspaces sollten isoliert genutzt werden.",
            target=str(repo_path),
            raw_value={"dirty_file_count": len(dirty_lines)},
        )
    return _payload(
        ReadinessCheckStatus.OK,
        ReadinessSeverity.INFO,
        "git status funktioniert und das Basis-Repository wirkt sauber.",
        target=str(repo_path),
    )


async def _git_status(ctx: ReadinessContext) -> dict[str, Any]:
    return await asyncio.to_thread(_git_status_probe, ctx)


def _git_branch_probe(ctx: ReadinessContext) -> dict[str, Any]:
    repo_path = _primary_repo_path(ctx.settings)
    if not (repo_path / ".git").exists():
        return _payload(
            ReadinessCheckStatus.SKIPPED,
            ReadinessSeverity.INFO,
            "Branch-Pruefung uebersprungen, weil kein Git-Checkout vorliegt.",
            target=str(repo_path),
        )
    result = _run_git_command(repo_path, ["rev-parse", "--verify", f"{ctx.settings.default_base_branch}^{{commit}}"], ctx.settings)
    if result.returncode != 0:
        return _payload(
            ReadinessCheckStatus.WARNING,
            ReadinessSeverity.MEDIUM,
            f"Der erwartete Basis-Branch `{ctx.settings.default_base_branch}` konnte nicht verifiziert werden.",
            detail=_trim_text(result.stderr or result.stdout, 260),
            hint="Pruefe den konfigurierten Default-Branch und ob das Repo vollstaendig ausgecheckt wurde.",
            target=str(repo_path),
        )
    return _payload(
        ReadinessCheckStatus.OK,
        ReadinessSeverity.INFO,
        f"Basis-Branch `{ctx.settings.default_base_branch}` ist verifizierbar.",
        target=str(repo_path),
    )


async def _git_branch(ctx: ReadinessContext) -> dict[str, Any]:
    return await asyncio.to_thread(_git_branch_probe, ctx)


def _git_fetch_probe(ctx: ReadinessContext) -> dict[str, Any]:
    repo_path = _primary_repo_path(ctx.settings)
    if not (repo_path / ".git").exists():
        return _payload(
            ReadinessCheckStatus.SKIPPED,
            ReadinessSeverity.INFO,
            "git fetch wird uebersprungen, weil kein Git-Checkout vorliegt.",
            target=str(repo_path),
        )
    result = _run_git_command(repo_path, ["fetch", "origin"], ctx.settings)
    if result.returncode != 0:
        stderr = _trim_text(result.stderr or result.stdout, 320)
        severity = ReadinessSeverity.MEDIUM
        hint = "Pruefe Netzwerk, origin-URL, safe.directory und Repo-Rechte."
        if "dubious ownership" in stderr.lower():
            severity = ReadinessSeverity.HIGH
            hint = "git fetch scheitert an dubious ownership. safe.directory und Repo-Rechte muessen zuerst stimmen."
        return _payload(
            ReadinessCheckStatus.WARNING,
            severity,
            "git fetch ist fehlgeschlagen.",
            detail=stderr,
            hint=hint,
            target=str(repo_path),
        )
    return _payload(
        ReadinessCheckStatus.OK,
        ReadinessSeverity.INFO,
        "git fetch gegen origin war erfolgreich.",
        target=str(repo_path),
    )


async def _git_fetch(ctx: ReadinessContext) -> dict[str, Any]:
    return await asyncio.to_thread(_git_fetch_probe, ctx)


async def _secrets_env_loaded(ctx: ReadinessContext) -> dict[str, Any]:
    env_file = Path(".env")
    message = "Laufzeitkonfiguration wurde geladen."
    if env_file.exists():
        message = f"Laufzeitkonfiguration geladen. .env vorhanden unter {env_file.resolve()}."
    return _payload(
        ReadinessCheckStatus.OK,
        ReadinessSeverity.INFO,
        message,
        raw_value={
            "default_model_provider": ctx.settings.default_model_provider,
            "app_env": ctx.settings.app_env,
            "log_level": ctx.settings.log_level,
        },
    )


async def _secrets_required_keys(ctx: ReadinessContext) -> dict[str, Any]:
    problems: list[str] = []
    providers = ctx.settings.model_provider_configs()
    default_provider = providers.get(ctx.settings.default_model_provider)
    if default_provider is None:
        problems.append(f"DEFAULT_MODEL_PROVIDER `{ctx.settings.default_model_provider}` ist unbekannt.")
    else:
        if not default_provider.get("base_url"):
            problems.append("Default-Provider hat keine Base-URL.")
        if not default_provider.get("model_name"):
            problems.append("Default-Provider hat keinen Modellnamen.")
    if problems:
        return _payload(
            ReadinessCheckStatus.FAIL,
            ReadinessSeverity.HIGH,
            "Pflichtwerte fuer das Standardmodell sind unvollstaendig.",
            detail=" ".join(problems),
            hint="Pruefe DEFAULT_MODEL_PROVIDER, die Provider-Base-URL und den Modellnamen in der .env-Datei.",
            raw_value={"problems": problems},
        )
    return _payload(
        ReadinessCheckStatus.OK,
        ReadinessSeverity.INFO,
        "Pflichtwerte fuer das Standardmodell sind gesetzt.",
    )


async def _secrets_files(ctx: ReadinessContext) -> dict[str, Any]:
    states = {
        "default_model_api_key_file": _read_secret_file_state(
            ctx.settings.default_model_api_key_file,
            raw_env_value=os.getenv("MODEL_API_KEY_FILE"),
        ),
        "mistral_api_key_file": _read_secret_file_state(
            ctx.settings.mistral_api_key_file,
            raw_env_value=os.getenv("MISTRAL_API_KEY_FILE"),
        ),
        "qwen_api_key_file": _read_secret_file_state(
            ctx.settings.qwen_api_key_file,
            raw_env_value=os.getenv("QWEN_API_KEY_FILE"),
        ),
        "web_search_api_key_file": _read_secret_file_state(
            ctx.settings.web_search_api_key_file,
            raw_env_value=os.getenv("WEB_SEARCH_API_KEY_FILE"),
        ),
        "github_token_file": _read_secret_file_state(
            ctx.settings.github_token_file,
            raw_env_value=os.getenv("GITHUB_TOKEN_FILE"),
        ),
    }
    failures = [
        name
        for name, state in states.items()
        if state["state"] in {"directory", "invalid_path", "not_readable"}
    ]
    warnings = [name for name, state in states.items() if state["state"] == "missing"]
    if failures:
        return _payload(
            ReadinessCheckStatus.FAIL,
            ReadinessSeverity.HIGH,
            "Mindestens ein Secret-Dateipfad ist ungueltig oder nicht lesbar.",
            detail=", ".join(f"{name}: {states[name]['detail']}" for name in failures),
            hint=(
                "Pruefe, ob der Pfad auf eine echte Datei zeigt und der Service-User sie lesen darf. "
                "Leere optionale *_FILE-Werte koennen leer bleiben und werden ignoriert."
            ),
            raw_value=states,
        )
    if warnings:
        return _payload(
            ReadinessCheckStatus.WARNING,
            ReadinessSeverity.LOW,
            "Einige optionale Secret-Dateien fehlen.",
            detail=", ".join(f"{name}: {states[name]['detail']}" for name in warnings),
            hint="Das ist nur kritisch, wenn der zugehoerige Dienst wirklich aktiviert genutzt werden soll.",
            raw_value=states,
        )
    return _payload(
        ReadinessCheckStatus.OK,
        ReadinessSeverity.INFO,
        "Konfigurierte Secret-Dateien wirken plausibel.",
        raw_value=states,
    )


async def _secrets_conflicts(ctx: ReadinessContext) -> dict[str, Any]:
    issues: list[str] = []
    if (
        ctx.settings.web_research_enabled
        and not ctx.settings.web_search_base_url
        and ctx.services.search_provider_service is None
    ):
        issues.append(
            "WEB_RESEARCH_ENABLED ist aktiv, aber es gibt keinen direkten WEB_SEARCH_BASE_URL-Wert "
            "und keinen SearchProviderService."
        )
    if ctx.settings.github_mcp_enabled and not ctx.settings.github_mcp_base_url:
        issues.append("GITHUB_MCP_ENABLED ist aktiv, aber GITHUB_MCP_BASE_URL fehlt.")
    if ctx.settings.openhands_enabled and not ctx.settings.openhands_base_url:
        issues.append("OPENHANDS_ENABLED ist aktiv, aber OPENHANDS_BASE_URL fehlt.")
    if issues:
        return _payload(
            ReadinessCheckStatus.WARNING,
            ReadinessSeverity.MEDIUM,
            "Es gibt widerspruechliche oder unvollstaendige Integrationskonfigurationen.",
            detail=" ".join(issues),
            hint="Aktivierte Integrationen sollten auch ihre Ziel-URL oder Provider-Konfiguration mitbringen.",
            raw_value={"issues": issues},
        )
    return _payload(
        ReadinessCheckStatus.OK,
        ReadinessSeverity.INFO,
        "Keine offensichtlichen Konfigurationskonflikte gefunden.",
    )


async def _integration_github_mcp(ctx: ReadinessContext) -> dict[str, Any]:
    target = ctx.settings.github_mcp_base_url
    if not ctx.settings.github_mcp_enabled:
        return _payload(
            ReadinessCheckStatus.SKIPPED,
            ReadinessSeverity.INFO,
            "GitHub MCP ist deaktiviert.",
            target=target,
        )
    try:
        response = await _http_get(target, timeout=_http_timeout(ctx.settings))
    except httpx.HTTPError as exc:
        return _payload(
            ReadinessCheckStatus.WARNING,
            ReadinessSeverity.MEDIUM,
            "GitHub MCP ist aktiviert, aber nicht erreichbar.",
            detail=_serialize_exception(exc),
            hint="Pruefe Service-URL, Containerstatus und ob die Instanz wirklich mit HTTP erreichbar sein soll.",
            target=target,
        )
    if response.status_code >= 500:
        return _payload(
            ReadinessCheckStatus.WARNING,
            ReadinessSeverity.MEDIUM,
            f"GitHub MCP antwortet mit HTTP {response.status_code}.",
            detail=_trim_text(response.text, 260),
            hint="Die Integration ist konfiguriert, aber serverseitig instabil.",
            target=target,
        )
    return _payload(
        ReadinessCheckStatus.OK,
        ReadinessSeverity.INFO,
        "GitHub MCP antwortet auf HTTP-Anfragen.",
        target=target,
        raw_value={"http_status": response.status_code},
    )


async def _integration_web_search(ctx: ReadinessContext) -> dict[str, Any]:
    service = ctx.services.search_provider_service
    if service is None:
        if not ctx.settings.web_research_enabled:
            return _payload(
                ReadinessCheckStatus.SKIPPED,
                ReadinessSeverity.INFO,
                "Web-Recherche ist deaktiviert.",
            )
        if not ctx.settings.web_search_base_url:
            return _payload(
                ReadinessCheckStatus.WARNING,
                ReadinessSeverity.LOW,
                "Web-Recherche ist aktiv, aber es gibt keinen direkten WEB_SEARCH_BASE_URL-Wert.",
                hint="Nutze die Search-Provider-Konfiguration oder setze eine direkte Basis-URL fuer die Fallback-Suche.",
            )
        try:
            response = await _http_get(ctx.settings.web_search_base_url, timeout=_http_timeout(ctx.settings))
        except httpx.HTTPError as exc:
            return _payload(
                ReadinessCheckStatus.WARNING,
                ReadinessSeverity.MEDIUM,
                "Direkte Web-Search-Basis-URL ist nicht erreichbar.",
                detail=_serialize_exception(exc),
                hint="Pruefe die Search-Instanz, Reverse-Proxies oder den konfigurierten Host.",
                target=ctx.settings.web_search_base_url,
            )
        return _payload(
            ReadinessCheckStatus.OK,
            ReadinessSeverity.INFO,
            "Direkte Web-Search-Basis-URL antwortet.",
            target=ctx.settings.web_search_base_url,
            raw_value={"http_status": response.status_code},
        )

    settings = service.load_settings()
    enabled_providers = [provider for provider in settings.providers if provider.enabled]
    if not enabled_providers:
        return _payload(
            ReadinessCheckStatus.SKIPPED,
            ReadinessSeverity.INFO,
            "Kein Web-Search-Provider ist aktiviert.",
        )
    try:
        health = await service.health_check(enabled_providers[0].id)
    except Exception as exc:
        return _payload(
            ReadinessCheckStatus.WARNING,
            ReadinessSeverity.MEDIUM,
            "Web-Search-Provider-Healthcheck konnte nicht abgeschlossen werden.",
            detail=_serialize_exception(exc),
            hint="Pruefe die Provider-Einstellungen und den Zielserver der aktiven Web-Suche.",
            raw_value={"provider_id": enabled_providers[0].id},
        )
    status = ReadinessCheckStatus.OK if health.api_ready else ReadinessCheckStatus.WARNING
    severity = ReadinessSeverity.INFO if health.api_ready else ReadinessSeverity.MEDIUM
    return _payload(
        status,
        severity,
        health.message,
        detail=health.technical_cause or "",
        hint=(
            "Wenn nur HTML funktioniert, aber JSON nicht, ist der Provider fuer Worker noch nicht produktiv nutzbar."
            if not health.api_ready
            else ""
        ),
        target=health.checked_url,
        raw_value=health.model_dump(mode="json"),
    )


async def _integration_staging(ctx: ReadinessContext) -> dict[str, Any]:
    target = ctx.settings.staging_healthcheck_url
    if not ctx.settings.auto_deploy_staging:
        return _payload(
            ReadinessCheckStatus.SKIPPED,
            ReadinessSeverity.INFO,
            "Auto-Deploy nach Staging ist deaktiviert.",
            target=target,
        )
    if not target:
        return _payload(
            ReadinessCheckStatus.WARNING,
            ReadinessSeverity.LOW,
            "Auto-Deploy ist aktiv, aber es gibt keine Staging-Health-URL.",
            hint="Setze STAGING_HEALTHCHECK_URL, damit Deploy-Ziele plausibel geprueft werden koennen.",
        )
    try:
        response = await _http_get(target, timeout=_http_timeout(ctx.settings, deep=True))
    except httpx.HTTPError as exc:
        return _payload(
            ReadinessCheckStatus.WARNING,
            ReadinessSeverity.MEDIUM,
            "Das konfigurierte Staging-Ziel ist nicht erreichbar.",
            detail=_serialize_exception(exc),
            hint="Pruefe Host, Healthcheck-URL und Netzpfad zum Staging-System.",
            target=target,
        )
    status = ReadinessCheckStatus.OK if response.status_code < 400 else ReadinessCheckStatus.WARNING
    return _payload(
        status,
        ReadinessSeverity.INFO if status is ReadinessCheckStatus.OK else ReadinessSeverity.MEDIUM,
        (
            "Staging-Healthcheck antwortet."
            if status is ReadinessCheckStatus.OK
            else f"Staging-Healthcheck meldet HTTP {response.status_code}."
        ),
        detail=_trim_text(response.text, 240) if response.status_code >= 400 else "",
        hint="Bei instabilem Staging pruefe Compose, Zielpfad und Healthcheck-Endpunkt.",
        target=target,
    )


async def _integration_openhands(ctx: ReadinessContext) -> dict[str, Any]:
    target = ctx.settings.openhands_base_url
    if not ctx.settings.openhands_enabled:
        return _payload(
            ReadinessCheckStatus.SKIPPED,
            ReadinessSeverity.INFO,
            "OpenHands ist deaktiviert.",
            target=target,
        )
    try:
        response = await _http_get(target, timeout=_http_timeout(ctx.settings))
    except httpx.HTTPError as exc:
        return _payload(
            ReadinessCheckStatus.WARNING,
            ReadinessSeverity.MEDIUM,
            "OpenHands ist aktiviert, aber nicht erreichbar.",
            detail=_serialize_exception(exc),
            hint="Pruefe OPENHANDS_BASE_URL und den Zielservice.",
            target=target,
        )
    return _payload(
        ReadinessCheckStatus.OK if response.status_code < 500 else ReadinessCheckStatus.WARNING,
        ReadinessSeverity.INFO if response.status_code < 500 else ReadinessSeverity.MEDIUM,
        "OpenHands antwortet auf HTTP-Anfragen." if response.status_code < 500 else f"OpenHands meldet HTTP {response.status_code}.",
        detail=_trim_text(response.text, 240) if response.status_code >= 400 else "",
        target=target,
    )


async def _performance_recent_failures(ctx: ReadinessContext) -> dict[str, Any]:
    failures = dict(ctx.runtime_insights.get("failure_counts") or {})
    if not failures:
        return _payload(
            ReadinessCheckStatus.OK,
            ReadinessSeverity.INFO,
            "In den zuletzt gesehenen Aufgaben wurden keine wiederholten Worker-Fehler erkannt.",
        )
    top_worker, top_count = max(failures.items(), key=lambda item: item[1])
    severity = ReadinessSeverity.MEDIUM if top_count < 3 else ReadinessSeverity.HIGH
    status = ReadinessCheckStatus.WARNING if top_count < 5 else ReadinessCheckStatus.FAIL
    detail = ", ".join(f"{worker}: {count}" for worker, count in sorted(failures.items(), key=lambda item: item[1], reverse=True))
    return _payload(
        status,
        severity,
        f"Wiederholte Worker-Fehler erkannt. Am haeufigsten betroffen: {top_worker} ({top_count}).",
        detail=detail,
        hint="Konzentriere dich zuerst auf die Worker mit den meisten Fehlern oder HTTP-500-Signalen.",
        raw_value={"failure_counts": failures},
    )


async def _performance_timeouts(ctx: ReadinessContext) -> dict[str, Any]:
    timeouts = dict(ctx.runtime_insights.get("timeout_counts") or {})
    if not timeouts:
        return _payload(
            ReadinessCheckStatus.OK,
            ReadinessSeverity.INFO,
            "Keine wiederholten Timeout-Signale in den zuletzt gespeicherten Worker-Ergebnissen gefunden.",
        )
    detail = ", ".join(f"{worker}: {count}" for worker, count in sorted(timeouts.items(), key=lambda item: item[1], reverse=True))
    return _payload(
        ReadinessCheckStatus.WARNING,
        ReadinessSeverity.MEDIUM,
        "Wiederholte Timeout-Situationen in frueheren Worker-Laeufen erkannt.",
        detail=detail,
        hint="Bei lokaler Inferenz sind grosszuegige Worker- und LLM-Timeouts wichtig. Beobachte besonders die betroffenen Worker.",
        raw_value={"timeout_counts": timeouts},
    )


async def _performance_active_waits(ctx: ReadinessContext) -> dict[str, Any]:
    snapshot = dict(ctx.runtime_insights.get("worker_snapshot") or {})
    long_waits: list[str] = []
    for worker_name, progress in snapshot.items():
        state = str(progress.get("state") or "")
        elapsed_seconds = progress.get("elapsed_seconds")
        if state not in {"waiting", "running"} or not isinstance(elapsed_seconds, (int, float)):
            continue
        if float(elapsed_seconds) >= ctx.settings.readiness_slow_warning_seconds:
            wait_target = str(progress.get("waiting_for") or progress.get("current_action") or "unbekannten Grund")
            long_waits.append(f"{worker_name}: {float(elapsed_seconds):.1f}s auf {wait_target}")
    if not long_waits:
        return _payload(
            ReadinessCheckStatus.OK,
            ReadinessSeverity.INFO,
            "Keine aktuell auffaellig langen Warte- oder Rechenphasen erkannt.",
        )
    return _payload(
        ReadinessCheckStatus.WARNING,
        ReadinessSeverity.LOW,
        "Es gibt aktive Worker mit bereits spuerbar langen Lauf- oder Wartezeiten.",
        detail=" | ".join(long_waits[:8]),
        hint="Das ist auf langsamer Hardware nicht automatisch ein Defekt. Nutze es als Hinweis fuer Timeout- und Routing-Tuning.",
        raw_value={"long_waits": long_waits},
    )


def _environment_overview(ctx: ReadinessContext) -> dict[str, Any]:
    """Expose a tiny, safe environment summary for the operator header and JSON export."""

    return {
        "mode": ctx.mode.value,
        "default_model_provider": ctx.settings.default_model_provider,
        "orchestrator_internal_url": ctx.settings.orchestrator_internal_url,
        "web_ui_internal_url": ctx.settings.web_ui_internal_url,
        "workspace_root": str(ctx.settings.workspace_root),
        "task_workspace_root": str(ctx.settings.effective_task_workspace_root),
        "runtime_home_dir": str(ctx.settings.runtime_home_dir),
        "primary_repo_path": str(_primary_repo_path(ctx.settings)),
        "timeouts": {
            "http_fast_seconds": ctx.settings.readiness_http_fast_timeout_seconds,
            "http_deep_seconds": ctx.settings.readiness_http_deep_timeout_seconds,
            "llm_smoke_seconds": ctx.settings.readiness_llm_smoke_timeout_seconds,
            "worker_smoke_seconds": ctx.settings.readiness_worker_smoke_timeout_seconds,
            "git_seconds": ctx.settings.readiness_git_timeout_seconds,
            "slow_warning_seconds": ctx.settings.readiness_slow_warning_seconds,
        },
        "feature_flags": {
            "web_research_enabled": ctx.settings.web_research_enabled,
            "github_mcp_enabled": ctx.settings.github_mcp_enabled,
            "openhands_enabled": ctx.settings.openhands_enabled,
            "auto_deploy_staging": ctx.settings.auto_deploy_staging,
            "self_improvement_enabled": ctx.settings.self_improvement_enabled,
        },
        "recent_task_count": ctx.runtime_insights.get("recent_task_count", 0),
        "active_tasks": ctx.runtime_insights.get("active_tasks", []),
    }


def _make_worker_runner(
    worker_name: str,
    display_name: str,
    url: str,
) -> Callable[[ReadinessContext], Awaitable[dict[str, Any]]]:
    async def runner(current_ctx: ReadinessContext) -> dict[str, Any]:
        return await _worker_health(current_ctx, worker_name, display_name, url)

    return runner


def _make_llm_models_runner(
    provider_name: str,
    provider: dict[str, Any],
) -> Callable[[ReadinessContext], Awaitable[dict[str, Any]]]:
    async def runner(current_ctx: ReadinessContext) -> dict[str, Any]:
        return await _llm_models_endpoint(current_ctx, provider_name, provider)

    return runner


def _make_llm_smoke_runner(
    provider_name: str,
    provider: dict[str, Any],
) -> Callable[[ReadinessContext], Awaitable[dict[str, Any]]]:
    async def runner(current_ctx: ReadinessContext) -> dict[str, Any]:
        return await _llm_chat_smoke(current_ctx, provider_name, provider)

    return runner


def _definitions_for_context(ctx: ReadinessContext) -> list[ReadinessCheckDefinition]:
    providers = ctx.settings.model_provider_configs()
    definitions: list[ReadinessCheckDefinition] = [
        ReadinessCheckDefinition("backend-orchestrator-health", "backend", "Orchestrator /health", _backend_orchestrator_health),
        ReadinessCheckDefinition("backend-web-ui-health", "backend", "Web-UI /health", _backend_web_ui_health),
        ReadinessCheckDefinition("backend-database", "backend", "Datenbank erreichbar", _backend_database),
        ReadinessCheckDefinition("backend-task-service", "backend", "Task-Service verfuegbar", _backend_task_service),
        ReadinessCheckDefinition("backend-suggestions", "backend", "Suggestions-/Registry-Service", _backend_suggestions_registry),
        ReadinessCheckDefinition("backend-policy", "backend", "Repository-Policy verfuegbar", _backend_repository_policy),
        ReadinessCheckDefinition("llm-default-provider", "llm", "Default-Provider aufloesbar", _llm_default_provider),
        ReadinessCheckDefinition("git-workspace-root", "git", "Workspace-Verzeichnis", _git_workspace_root),
        ReadinessCheckDefinition("git-task-workspaces", "git", "Task-Workspace-Root", _git_task_workspace_root),
        ReadinessCheckDefinition("git-primary-repo", "git", "Primaeres Repository", _git_repo_path),
        ReadinessCheckDefinition("git-safe-directory", "git", "safe.directory", _git_safe_directory, depends_on=("git-primary-repo",)),
        ReadinessCheckDefinition("git-status", "git", "git status", _git_status, depends_on=("git-safe-directory",)),
        ReadinessCheckDefinition("git-branch", "git", "Branch-Verifikation", _git_branch, depends_on=("git-status",)),
        ReadinessCheckDefinition("git-fetch", "git", "git fetch", _git_fetch, depends_on=("git-status",), deep_only=True),
        ReadinessCheckDefinition("secrets-env", "secrets", "Umgebung geladen", _secrets_env_loaded),
        ReadinessCheckDefinition("secrets-required", "secrets", "Pflichtwerte", _secrets_required_keys),
        ReadinessCheckDefinition("secrets-files", "secrets", "Secret-Dateien", _secrets_files),
        ReadinessCheckDefinition("secrets-conflicts", "secrets", "Konfigurationskonflikte", _secrets_conflicts),
        ReadinessCheckDefinition("integrations-github-mcp", "integrations", "GitHub / MCP", _integration_github_mcp),
        ReadinessCheckDefinition("integrations-web-search", "integrations", "Web-Search-Provider", _integration_web_search),
        ReadinessCheckDefinition("integrations-staging", "integrations", "Staging-Ziel", _integration_staging),
        ReadinessCheckDefinition("integrations-openhands", "integrations", "OpenHands", _integration_openhands),
        ReadinessCheckDefinition("performance-failures", "performance", "Wiederholte Worker-Fehler", _performance_recent_failures),
        ReadinessCheckDefinition("performance-timeouts", "performance", "Wiederholte Timeouts", _performance_timeouts),
        ReadinessCheckDefinition("performance-active-waits", "performance", "Aktuelle Langlaeufer", _performance_active_waits),
    ]

    for worker_name, display_name, setting_attr in WORKER_TARGETS:
        url = getattr(ctx.settings, setting_attr)
        definitions.append(
            ReadinessCheckDefinition(
                f"worker-{worker_name}",
                "workers",
                display_name,
                _make_worker_runner(worker_name, display_name, url),
            )
        )

    for provider_name, provider in providers.items():
        definitions.append(
            ReadinessCheckDefinition(
                f"llm-{provider_name}-models",
                "llm",
                f"{provider_name} /models",
                _make_llm_models_runner(provider_name, provider),
            )
        )
        if provider_name == ctx.settings.default_model_provider.strip().lower():
            definitions.append(
                ReadinessCheckDefinition(
                    f"llm-{provider_name}-smoke",
                    "llm",
                    f"{provider_name} Chat-Smoke-Test",
                    _make_llm_smoke_runner(provider_name, provider),
                    depends_on=(f"llm-{provider_name}-models",),
                    deep_only=True,
                )
            )

    return definitions


async def build_readiness_report(
    settings: Settings,
    *,
    mode: ReadinessMode = ReadinessMode.QUICK,
    services: ReadinessServices | None = None,
) -> ReadinessReport:
    """Run the structured readiness report and always return a renderable result."""

    started_at = _utc_now()
    started_perf = perf_counter()
    services = services or ReadinessServices()
    ctx = ReadinessContext(settings=settings, mode=mode, services=services)
    ctx.runtime_insights = await asyncio.to_thread(_collect_runtime_insights, services.task_service)

    definitions = _definitions_for_context(ctx)
    checks = await asyncio.gather(*[_run_check(definition, ctx) for definition in definitions])
    checks = sorted(checks, key=lambda item: (list(CATEGORY_LABELS).index(item.category), -_status_rank(item.status), item.name))
    summary = _build_summary(checks)
    categories = _build_category_summaries(checks)
    overall_status = _overall_status(checks)
    ready_for_workflows, headline, summary_message = _headline_and_message(overall_status, summary)
    finished_at = _utc_now()

    return ReadinessReport(
        mode=mode,
        overall_status=overall_status,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=round((perf_counter() - started_perf) * 1000, 1),
        summary=summary,
        categories=categories,
        checks=checks,
        environment_overview=_environment_overview(ctx),
        recommendations=_build_recommendations(checks),
        ready_for_workflows=ready_for_workflows,
        headline=headline,
        summary_message=summary_message,
    )


def build_catastrophic_readiness_report(
    settings: Settings,
    *,
    mode: ReadinessMode = ReadinessMode.QUICK,
    exc: Exception,
) -> ReadinessReport:
    """Return a last-resort report when the runner itself breaks before it can produce a normal report."""

    started_at = _utc_now()
    failed_check = ReadinessCheckResult(
        id="readiness-runner-crash",
        category="backend",
        name="Readiness-Runner",
        status=ReadinessCheckStatus.FAIL,
        severity=ReadinessSeverity.CRITICAL,
        started_at=started_at,
        finished_at=_utc_now(),
        duration_ms=0.0,
        message="Die Bereitschaftspruefung selbst ist intern abgestuerzt.",
        detail=_serialize_exception(exc),
        hint="Pruefe Orchestrator-Logs. Der Bericht zeigt absichtlich trotzdem diesen Mindestzustand statt einer nackten 500-Seite.",
    )
    summary = _build_summary([failed_check])
    categories = _build_category_summaries([failed_check])
    ready_for_workflows, headline, summary_message = _headline_and_message(ReadinessCheckStatus.FAIL, summary)
    return ReadinessReport(
        mode=mode,
        overall_status=ReadinessCheckStatus.FAIL,
        started_at=started_at,
        finished_at=_utc_now(),
        duration_ms=0.0,
        summary=summary,
        categories=categories,
        checks=[failed_check],
        environment_overview={
            "mode": mode.value,
            "default_model_provider": settings.default_model_provider,
            "orchestrator_internal_url": settings.orchestrator_internal_url,
            "web_ui_internal_url": settings.web_ui_internal_url,
        },
        recommendations=_build_recommendations([failed_check]),
        ready_for_workflows=ready_for_workflows,
        headline=headline,
        summary_message=summary_message,
    )
