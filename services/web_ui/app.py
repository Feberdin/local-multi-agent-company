"""
Purpose: Minimal dashboard and operations UI for tasks, trusted sources, and web-search provider settings.
Input/Output: Operators use HTML forms backed by the orchestrator API to manage workflow and research guardrails.
Important invariants: The UI is read-mostly, approval actions remain explicit, and it never bypasses orchestrator state management.
How to debug: If a form stops working, inspect the orchestrator base URL, the called endpoint, and the returned JSON error detail.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import zipfile
from collections import Counter
from datetime import UTC, datetime, tzinfo
from functools import lru_cache
from importlib import metadata
from pathlib import Path
from statistics import mean, median
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from services.shared.agentic_lab.config import get_settings
from services.shared.agentic_lab.logging_utils import configure_logging
from services.shared.agentic_lab.model_routing import resolve_fallback_provider, resolve_worker_route
from services.shared.agentic_lab.readiness import ReadinessMode, build_catastrophic_readiness_report
from services.shared.agentic_lab.schemas import HealthResponse, TaskStatus, WorkerProbeMode
from services.shared.agentic_lab.worker_probe_service import PROBE_DEFINITIONS, PROBE_WORKER_LABELS, PROBE_WORKERS

settings = get_settings()
logger = configure_logging(settings.service_name, settings.log_level)
READINESS_MODE_QUERY = Query(default=ReadinessMode.QUICK)
READINESS_MODE_FORM = Form(default=ReadinessMode.QUICK)
WORKER_PROBE_MODE_FORM = Form(default=WorkerProbeMode.FULL)
DEFAULT_WORKER_PROBE_GOAL = (
    "Verbessere Beobachtbarkeit, Fehlermeldungen und Healthchecks einer lokalen Docker-basierten Anwendung."
)
DEFAULT_OK_WORKER_PROBE_GOAL = "Leerer OK-Kurztest fuer alle Worker-Vertraege ohne Repository-Aenderungen."
DEFAULT_TARGETED_WORKER_PROBE_GOAL = (
    "Pruefe nur den ausgewaehlten Worker gegen den zuletzt gefixten Bereich ohne Repository-Aenderungen."
)
DEFAULT_MICRO_FIX_WORKER_PROBE_GOAL = (
    "README-Mini-Fix in einem Wegwerf-Repo: Setze `:)` an den Anfang der ersten README-Zeile und aendere sonst nichts."
)
TARGETED_WORKER_FOCUS_LIMIT = 6

# Why this exists:
# The UI should import both inside the container (`/app/...`) and in local tests where the
# repository lives in a normal workspace path. Resolving from this file keeps the setup portable.
WEB_UI_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = WEB_UI_DIR.parent.parent
DEFAULT_BUILD_INFO_PATH = Path(os.getenv("FEBERDIN_BUILD_INFO_PATH", "/opt/feberdin/build-info.json"))


def _resolve_package_version() -> str:
    """Return the installed package version and fall back to the checked-in default during local tests."""

    try:
        return metadata.version("feberdin-agent-team")
    except metadata.PackageNotFoundError:
        return "0.1.0"


APP_VERSION = _resolve_package_version()
app = FastAPI(title="Feberdin Agent Team Dashboard", version=APP_VERSION)
app.mount("/static", StaticFiles(directory=str(WEB_UI_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(WEB_UI_DIR / "templates"))


def _safe_json(value: Any) -> str:
    """Render debug payloads without crashing templates on unexpected runtime types."""

    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


templates.env.filters["safe_json"] = _safe_json


def _run_git_metadata_command(repo_path: Path, *args: str) -> str | None:
    """Read small Git facts for the operator header without crashing the UI if Git is unavailable."""

    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_path), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip()
    return value or None


def _ui_repo_candidate_paths() -> list[Path]:
    """Return likely repository roots for UI metadata and targeted fix helpers."""

    candidate_paths: list[Path] = [PROJECT_ROOT]
    if settings.self_improvement_local_repo_path.strip():
        candidate_paths.append(Path(settings.self_improvement_local_repo_path))
    return candidate_paths


def _resolve_ui_repo_path() -> Path | None:
    """Pick the first reachable Git checkout so targeted tests can suggest the latest changed files."""

    seen_paths: set[Path] = set()
    for candidate in _ui_repo_candidate_paths():
        resolved_candidate = candidate.resolve()
        if resolved_candidate in seen_paths:
            continue
        seen_paths.add(resolved_candidate)
        if not resolved_candidate.exists():
            continue
        if not (resolved_candidate / ".git").exists():
            continue
        return resolved_candidate
    return None


def _recent_fix_focus_paths(limit: int = TARGETED_WORKER_FOCUS_LIMIT) -> list[str]:
    """Suggest the latest changed tracked files so targeted worker tests can follow the real last fix."""

    repo_path = _resolve_ui_repo_path()
    if repo_path is None:
        return []
    output = _run_git_metadata_command(repo_path, "show", "--pretty=format:", "--name-only", "HEAD")
    if not output:
        return []
    paths: list[str] = []
    for raw_line in output.splitlines():
        path = raw_line.strip().replace("\\", "/")
        if not path or path in paths:
            continue
        paths.append(path)
        if len(paths) >= limit:
            break
    return paths


def _normalize_focus_paths_text(value: str | None) -> list[str]:
    """Normalize operator-entered focus paths so the part-test stays readable and bounded."""

    normalized: list[str] = []
    seen: set[str] = set()
    for raw_line in _split_lines(value or ""):
        path = raw_line.strip().replace("\\", "/")
        if not path or path in seen:
            continue
        seen.add(path)
        normalized.append(path)
        if len(normalized) >= TARGETED_WORKER_FOCUS_LIMIT:
            break
    return normalized

WORKER_SEQUENCE: tuple[dict[str, str], ...] = (
    {
        "worker_name": "requirements",
        "label": "Anforderungen",
        "description": "Auftrag, Annahmen und Akzeptanzkriterien werden strukturiert.",
    },
    {
        "worker_name": "cost",
        "label": "Ressourcen",
        "description": "Modell- und Ressourcenbedarf werden eingeschätzt.",
    },
    {
        "worker_name": "human_resources",
        "label": "Team",
        "description": "Empfohlene Spezialisten und ihr Einsatz werden festgelegt.",
    },
    {
        "worker_name": "research",
        "label": "Recherche",
        "description": "Repository und erlaubte Quellen werden ausgewertet.",
    },
    {
        "worker_name": "architecture",
        "label": "Architektur",
        "description": "Struktur, Schnittstellen und Umsetzungsschritte werden vorbereitet.",
    },
    {
        "worker_name": "data",
        "label": "Daten",
        "description": "Datenlogik oder Parsing werden vertieft betrachtet, falls noetig.",
    },
    {
        "worker_name": "ux",
        "label": "UX",
        "description": "Nutzerfuehrung und Bedienfluss werden bei UI-Themen geprueft.",
    },
    {
        "worker_name": "coding",
        "label": "Code",
        "description": "Codeaenderungen werden vorbereitet oder umgesetzt.",
    },
    {
        "worker_name": "reviewer",
        "label": "Review",
        "description": "Korrektheit, Risiken und Wartbarkeit werden geprueft.",
    },
    {
        "worker_name": "tester",
        "label": "Tests",
        "description": "Tests, Linting und Typpruefungen werden bewertet oder ausgefuehrt.",
    },
    {
        "worker_name": "security",
        "label": "Sicherheit",
        "description": "Sicherheits- und Secret-Risiken werden ueberprueft.",
    },
    {
        "worker_name": "validation",
        "label": "Validierung",
        "description": "Ergebnis und Auftrag werden gegenprueft.",
    },
    {
        "worker_name": "documentation",
        "label": "Doku",
        "description": "Betriebs- und Handover-Hinweise werden verdichtet.",
    },
    {
        "worker_name": "github",
        "label": "GitHub",
        "description": "Commit, Push und Pull Request werden vorbereitet oder erstellt.",
    },
    {
        "worker_name": "deploy",
        "label": "Staging",
        "description": "Der Staging-Rollout wird ausgefuehrt, falls aktiviert.",
    },
    {
        "worker_name": "qa",
        "label": "QA",
        "description": "Smoke-Checks und Abnahmehinweise werden gesammelt.",
    },
    {
        "worker_name": "memory",
        "label": "Wissen",
        "description": "Entscheidungen und Learnings werden dauerhaft gespeichert.",
    },
)
WORKER_SEQUENCE_INDEX = {item["worker_name"]: index for index, item in enumerate(WORKER_SEQUENCE)}
WORKER_LABELS = {item["worker_name"]: item["label"] for item in WORKER_SEQUENCE}
WORKER_DESCRIPTIONS = {item["worker_name"]: item["description"] for item in WORKER_SEQUENCE}
SUGGESTION_STATUS_LABELS = {
    "pending": "Offen",
    "approved": "Nur fuer diese Aufgabe freigegeben",
    "implemented": "Repo-weit umgesetzt",
    "dismissed": "Repo-weit verworfen",
    "suppressed_for_repository": "Repo-weit ausgeblendet",
    "rejected": "Repo-weit verworfen",
}
SUGGESTION_STATUS_CLASSES = {
    "pending": "status-waiting",
    "approved": "status-complete",
    "implemented": "status-complete",
    "dismissed": "status-blocked",
    "suppressed_for_repository": "status-idle",
    "rejected": "status-blocked",
}
SUGGESTION_SCOPE_LABELS = {
    "task_local": "Nur diese Aufgabe",
    "repository_wide": "Ganzer Repository-Kontext",
}
STATUS_TO_WORKER_HINT = {
    TaskStatus.REQUIREMENTS.value: "requirements",
    TaskStatus.RESOURCE_PLANNING.value: "human_resources",
    TaskStatus.RESEARCHING.value: "research",
    TaskStatus.ARCHITECTING.value: "architecture",
    TaskStatus.CODING.value: "coding",
    TaskStatus.ROLLING_BACK.value: "rollback",
    TaskStatus.REVIEWING.value: "reviewer",
    TaskStatus.TESTING.value: "tester",
    TaskStatus.SECURITY_REVIEW.value: "security",
    TaskStatus.VALIDATING.value: "validation",
    TaskStatus.DOCUMENTING.value: "documentation",
    TaskStatus.PR_CREATED.value: "github",
    TaskStatus.STAGING_DEPLOYED.value: "deploy",
    TaskStatus.SELF_UPDATING.value: "rollback",
    TaskStatus.QA_PENDING.value: "qa",
    TaskStatus.MEMORY_UPDATING.value: "memory",
}
ACTIVE_TASK_STATUSES = {
    TaskStatus.REQUIREMENTS.value,
    TaskStatus.RESOURCE_PLANNING.value,
    TaskStatus.RESEARCHING.value,
    TaskStatus.ARCHITECTING.value,
    TaskStatus.CODING.value,
    TaskStatus.ROLLING_BACK.value,
    TaskStatus.REVIEWING.value,
    TaskStatus.TESTING.value,
    TaskStatus.SECURITY_REVIEW.value,
    TaskStatus.VALIDATING.value,
    TaskStatus.DOCUMENTING.value,
    TaskStatus.PR_CREATED.value,
    TaskStatus.STAGING_DEPLOYED.value,
    TaskStatus.SELF_UPDATING.value,
    TaskStatus.QA_PENDING.value,
    TaskStatus.MEMORY_UPDATING.value,
}
AUTO_REFRESH_SECONDS = 15
WORKER_STATE_LABELS = {
    "running": "arbeitet",
    "slow": "auffaellig langsam",
    "waiting": "wartet",
    "blocked": "pausiert",
    "complete": "fertig",
    "failed": "Fehler",
    "queued": "wartet danach",
    "idle": "ungenutzt",
    "skipped": "uebersprungen",
}
WORKER_STATE_ICONS = {
    "running": "💭",
    "slow": "🐢",
    "waiting": "☕",
    "blocked": "⏸",
    "complete": "💬",
    "failed": "⚠",
    "queued": "⌛",
    "idle": "💤",
    "skipped": "↷",
}
READINESS_STATUS_LABELS = {
    "ok": "OK",
    "warning": "Warnung",
    "fail": "Fehler",
    "skipped": "Uebersprungen",
    "running": "Laeuft",
}
READINESS_STATUS_CLASSES = {
    "ok": "status-complete",
    "warning": "status-waiting",
    "fail": "status-failed",
    "skipped": "status-idle",
    "running": "status-running",
}
READINESS_SEVERITY_LABELS = {
    "info": "Info",
    "low": "Niedrig",
    "medium": "Mittel",
    "high": "Hoch",
    "critical": "Kritisch",
}
SYSTEM_SNAPSHOT_ARTIFACTS: tuple[dict[str, str], ...] = (
    {
        "key": "orchestrator-health",
        "filename": "orchestrator-health.json",
        "label": "Orchestrator Health",
        "description": "Health-Snapshot des Orchestrators fuer Verfuegbarkeit und degradierte Antworten.",
    },
    {
        "key": "web-ui-health",
        "filename": "web-ui-health.json",
        "label": "Web-UI Health",
        "description": "Lokaler Health-Snapshot des Dashboards.",
    },
    {
        "key": "tasks",
        "filename": "tasks.json",
        "label": "Aufgabenliste",
        "description": "Aktuelle Aufgaben mit Status, Stage und letzten Aktivitaeten.",
    },
    {
        "key": "repository-access",
        "filename": "repository-access.json",
        "label": "Repository-Allowlist",
        "description": "Aktive Freigaben fuer analysierbare Repositories.",
    },
    {
        "key": "trusted-sources",
        "filename": "trusted-sources.json",
        "label": "Trusted Sources",
        "description": "Aktive Source-Routing-Konfiguration fuer Coding-Recherche.",
    },
    {
        "key": "web-search",
        "filename": "web-search.json",
        "label": "Web Search Settings",
        "description": "Fallback-Provider, Prioritaeten und Search-Health.",
    },
    {
        "key": "worker-guidance",
        "filename": "worker-guidance.json",
        "label": "Worker Guidance",
        "description": "Operator-Vorgaben und Kompetenzgrenzen pro Worker.",
    },
    {
        "key": "suggestions-registry",
        "filename": "suggestions-registry.json",
        "label": "Suggestion Registry",
        "description": "Alle persistierten Mitarbeiterideen inklusive Entscheidungen.",
    },
    {
        "key": "runtime-summary",
        "filename": "runtime-summary.json",
        "label": "Runtime Summary",
        "description": "Sanitierter Laufzeit-Snapshot mit Pfaden, Timeouts und aktivierten Flags.",
    },
    {
        "key": "reports-manifest",
        "filename": "reports-manifest.json",
        "label": "Reports Manifest",
        "description": "Uebersicht aller bekannten Task-Report-Verzeichnisse unter /reports.",
    },
    {
        "key": "host-log-commands",
        "filename": "host-log-commands.txt",
        "label": "Host-Log-Befehle",
        "description": "Shell-Befehle fuer Docker-Logs, die aus der Web-UI nicht direkt lesbar sind.",
    },
)
TASK_SNAPSHOT_ARTIFACTS: tuple[dict[str, str], ...] = (
    {
        "key": "detail",
        "filename": "task-detail.json",
        "label": "Task-Detail",
        "description": "Rohdaten der Aufgabe direkt aus dem Orchestrator.",
    },
    {
        "key": "ui-state",
        "filename": "task-ui-state.json",
        "label": "UI-State",
        "description": "Vom Dashboard abgeleiteter Stage-, Worker- und Fortschrittszustand.",
    },
    {
        "key": "events",
        "filename": "task-events.json",
        "label": "Event-Historie",
        "description": "Persistierte Events inklusive Heartbeats und Fehlern.",
    },
    {
        "key": "worker-results",
        "filename": "task-worker-results.json",
        "label": "Worker-Ergebnisse",
        "description": "Bisher gespeicherte Worker-Outputs fuer diese Aufgabe.",
    },
    {
        "key": "suggestions",
        "filename": "task-suggestions.json",
        "label": "Mitarbeiterideen",
        "description": "Alle Suggestions, die zu dieser Aufgabe gehoeren.",
    },
    {
        "key": "reports-manifest",
        "filename": "task-reports-manifest.json",
        "label": "Task-Report-Manifest",
        "description": "Liste aller Report-Dateien, die unter /reports fuer diese Aufgabe liegen.",
    },
)
DATA_STORE_FILE_DESCRIPTIONS: dict[str, str] = {
    "repository-access-policy.json": "Persistierte Repository-Allowlist aus DATA_DIR.",
    "trusted_sources.json": "Persistierte Trusted-Source-Konfiguration aus DATA_DIR.",
    "web_search_providers.json": "Persistierte Web-Search-Provider aus DATA_DIR.",
    "worker_guidance.json": "Persistierte Worker-Guidance aus DATA_DIR.",
    "improvement_suggestions.json": "Persistierte Suggestions-Registry aus DATA_DIR.",
    "benchmark_state.json": "Persistierter Benchmark-Startpunkt nach einem manuellen Reset.",
}
BENCHMARK_STATE_FILENAME = "benchmark_state.json"
HOST_LOG_COMMANDS = """\
Diese Befehle laufen auf dem Unraid-Host und koennen nicht direkt aus der Web-UI geladen werden.

docker compose ps
docker compose logs --tail=200 web-ui
docker compose logs --tail=200 orchestrator
docker compose logs --tail=200 requirements-worker
docker compose logs --tail=200 coding-worker
docker compose logs --tail=200 research-worker
docker logs --tail=200 fmac-web
docker logs --tail=200 fmac-orch
docker logs --tail=200 fmac-req
docker logs --tail=200 fmac-code
docker logs --tail=200 fmac-rsch
"""


def _as_mapping(value: Any) -> dict[str, Any]:
    """Normalize uncertain payload sections so older or malformed rows do not crash the dashboard."""

    return dict(value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    """Normalize uncertain list fields because operators should see a degraded page instead of a 500."""

    return list(value) if isinstance(value, list) else []


def _worker_initials(label: str) -> str:
    """Create a compact avatar label for the worker theatre cards."""

    parts = [part[:1].upper() for part in label.replace("-", " ").split() if part]
    return "".join(parts[:2]) or "WK"


def _normalize_worker_results(value: Any) -> dict[str, dict[str, Any]]:
    """Defensively coerce stored worker results to the dict shape expected by the templates."""

    if not isinstance(value, dict):
        return {}

    normalized: dict[str, dict[str, Any]] = {}
    for worker_name, raw_result in value.items():
        if isinstance(raw_result, dict):
            result = dict(raw_result)
            result["outputs"] = _as_mapping(result.get("outputs"))
            result["warnings"] = _as_list(result.get("warnings"))
            result["errors"] = _as_list(result.get("errors"))
            result["risk_flags"] = _as_list(result.get("risk_flags"))
            result["artifacts"] = _as_list(result.get("artifacts"))
            result.setdefault("summary", "Kein Summary gespeichert.")
            normalized[str(worker_name)] = result
            continue

        normalized[str(worker_name)] = {
            "worker": str(worker_name),
            "summary": str(raw_result),
            "success": True,
            "outputs": {},
            "artifacts": [],
            "warnings": [],
            "errors": [],
            "risk_flags": [],
            "raw_value": raw_result,
        }
    return normalized


def _normalize_worker_progress(value: Any) -> dict[str, dict[str, Any]]:
    """Normalize persisted worker progress so compact theatre cards can rely on stable keys."""

    if not isinstance(value, dict):
        return {}

    normalized: dict[str, dict[str, Any]] = {}
    for worker_name, raw_progress in value.items():
        if not isinstance(raw_progress, dict):
            continue
        progress = dict(raw_progress)
        elapsed_seconds = progress.get("elapsed_seconds")
        normalized[str(worker_name)] = {
            **progress,
            "state": str(progress.get("state") or "idle"),
            "current_action": str(progress.get("current_action") or ""),
            "current_step": str(progress.get("current_step") or ""),
            "current_prompt_summary": str(progress.get("current_prompt_summary") or ""),
            "current_instruction": str(progress.get("current_instruction") or ""),
            "waiting_for": str(progress.get("waiting_for") or ""),
            "blocked_by": str(progress.get("blocked_by") or ""),
            "next_worker": str(progress.get("next_worker") or ""),
            "last_result_summary": str(progress.get("last_result_summary") or ""),
            "progress_message": str(progress.get("progress_message") or ""),
            "last_error": str(progress.get("last_error") or ""),
            "event_kind": str(progress.get("event_kind") or "note"),
            "started_at_display": _format_timestamp(progress.get("started_at")),
            "updated_at_display": _format_timestamp(progress.get("updated_at")),
            "elapsed_display": (
                _format_duration(float(elapsed_seconds))
                if isinstance(elapsed_seconds, (int, float))
                else "noch nicht sichtbar"
            ),
        }
    return normalized


def _normalize_events(value: Any) -> list[dict[str, Any]]:
    """Coerce event payloads to a stable list-of-dicts shape for rendering and filtering."""

    normalized: list[dict[str, Any]] = []
    for raw_event in _as_list(value):
        if not isinstance(raw_event, dict):
            continue
        event = dict(raw_event)
        event["details"] = _as_mapping(event.get("details"))
        normalized.append(event)
    return normalized


def _normalize_suggestions(value: Any) -> list[dict[str, Any]]:
    """Prepare suggestion rows for the detail page without assuming a perfectly shaped registry payload."""

    normalized: list[dict[str, Any]] = []
    for raw_suggestion in _as_list(value):
        if not isinstance(raw_suggestion, dict):
            continue
        suggestion = dict(raw_suggestion)
        suggestion["worker_name"] = str(suggestion.get("worker_name") or "unknown")
        suggestion["worker_label"] = WORKER_LABELS.get(suggestion["worker_name"], suggestion["worker_name"])
        suggestion["status"] = str(suggestion.get("status") or "unknown")
        if suggestion["status"] == "rejected":
            suggestion["status"] = "dismissed"
        suggestion["scope"] = str(suggestion.get("scope") or "task_local")
        suggestion["status_label"] = SUGGESTION_STATUS_LABELS.get(suggestion["status"], suggestion["status"])
        suggestion["status_class"] = SUGGESTION_STATUS_CLASSES.get(suggestion["status"], "status-info")
        suggestion["scope_label"] = SUGGESTION_SCOPE_LABELS.get(suggestion["scope"], "Unbekannter Geltungsbereich")
        suggestion["repository_label"] = str(suggestion.get("repository") or "unbekanntes Repository")
        suggestion["fingerprint_short"] = str(suggestion.get("fingerprint") or "")[:12]
        suggestion["created_at_display"] = _format_timestamp(suggestion.get("created_at"))
        suggestion["updated_at_display"] = _format_timestamp(suggestion.get("updated_at"))
        suggestion["decision_note_display"] = str(suggestion.get("decision_note") or "").strip()
        suggestion["is_repository_wide"] = suggestion["scope"] == "repository_wide"
        normalized.append(suggestion)
    return normalized


def _benchmark_state_path() -> Path:
    """Return the persistent benchmark state path so operators can reset the visible history window."""

    return settings.data_dir / BENCHMARK_STATE_FILENAME


def _load_benchmark_state() -> dict[str, Any]:
    """Load the benchmark reset state defensively so a malformed file never breaks the page."""

    state_path = _benchmark_state_path()
    if not state_path.exists():
        return {}
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_benchmark_state(state: dict[str, Any]) -> None:
    """Persist the benchmark reset state in DATA_DIR so the filtered window survives restarts."""

    state_path = _benchmark_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=True), encoding="utf-8")


def _benchmark_reset_at() -> datetime | None:
    """Return the persisted benchmark reset timestamp if one is active."""

    return _parse_timestamp(_load_benchmark_state().get("reset_at"))


def _task_matches_benchmark_window(task: dict[str, Any], reset_at: datetime | None) -> bool:
    """Decide whether one task should contribute to the current benchmark window."""

    if reset_at is None:
        return True
    updated_at = _parse_timestamp(task.get("updated_at"))
    if updated_at is None:
        return False
    return updated_at >= reset_at


def _next_worker_name(worker_name: str) -> str | None:
    """Return the next worker in the visual sequence so completed stages can visibly hand off work."""

    index = WORKER_SEQUENCE_INDEX.get(worker_name)
    if index is None:
        return None
    next_index = index + 1
    if next_index >= len(WORKER_SEQUENCE):
        return None
    return WORKER_SEQUENCE[next_index]["worker_name"]


def _orchestrator_timeout() -> httpx.Timeout:
    """Keep UI requests snappy so the dashboard can degrade gracefully instead of hanging."""

    return httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0)


def _error_response(method: str, url: str, detail: str, *, status_code: int = 503) -> httpx.Response:
    """Build a synthetic JSON response so route handlers can stay simple even on backend failures."""

    return httpx.Response(status_code, request=httpx.Request(method, url), json={"detail": detail})


def _response_detail(response: httpx.Response, default_message: str) -> str:
    """Return a human-readable error detail without assuming the backend returned valid JSON."""

    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if isinstance(payload, dict) and payload.get("detail"):
        return str(payload["detail"])
    response_text = response.text.strip()
    if response_text:
        return f"{default_message} Backend-Antwort: {response_text[:300]}"
    return default_message


def _response_json(response: httpx.Response, default_value: Any) -> Any:
    """Return parsed JSON or a safe default when a degraded backend response is not JSON."""

    try:
        return response.json()
    except ValueError:
        return default_value


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse ISO timestamps from API responses without crashing the UI on malformed values."""

    if isinstance(value, datetime):
        # Why this exists:
        # Older persisted rows may contain naive UTC timestamps from SQLite or earlier service versions.
        # The runtime still compares timestamps in UTC, so we normalize naive values first and only convert
        # to the operator-facing display timezone when formatting for the UI.
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value
    if value in {None, ""}:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
    except ValueError:
        return None


@lru_cache(maxsize=1)
def _display_timezone() -> tuple[tzinfo, str]:
    """Resolve the operator-facing timezone once and fall back to UTC if the config is invalid."""

    timezone_name = (settings.ui_timezone or "Europe/Berlin").strip() or "Europe/Berlin"
    try:
        return ZoneInfo(timezone_name), timezone_name
    except ZoneInfoNotFoundError:
        logger.warning(
            "Configured UI timezone %r is unknown. Falling back to UTC for timestamp rendering.",
            timezone_name,
        )
        return UTC, "UTC"


def _format_timestamp(value: Any) -> str:
    """Render timestamps consistently so long-running stages remain readable at a glance."""

    parsed = _parse_timestamp(value)
    if parsed is None:
        return str(value or "unbekannt")
    display_tz, display_name = _display_timezone()
    localized = parsed.astimezone(display_tz)
    timezone_label = localized.tzname() or display_name
    return f"{localized.strftime('%Y-%m-%d %H:%M:%S')} {timezone_label}"


def _format_build_timestamp(value: Any) -> str:
    """Render build timestamps with weekday and local operator timezone for the top navigation."""

    parsed = _parse_timestamp(value)
    if parsed is None:
        return ""
    display_tz, display_name = _display_timezone()
    localized = parsed.astimezone(display_tz)
    timezone_label = localized.tzname() or display_name
    weekday_names = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
    weekday = weekday_names[localized.weekday()]
    return f"{weekday} {localized.strftime('%d.%m.%Y %H:%M:%S')} {timezone_label}"


def _read_baked_build_info(build_info_path: Path = DEFAULT_BUILD_INFO_PATH) -> dict[str, str]:
    """Read optional build metadata baked into the container image without depending on the live repo state."""

    try:
        payload = json.loads(build_info_path.read_text("utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key): str(value) for key, value in payload.items() if value not in {None, ""}}


@lru_cache(maxsize=1)
def _ui_build_info() -> dict[str, str]:
    """Return one stable operator-facing build badge with version, running commit, and build time."""

    candidate_paths: list[Path] = [PROJECT_ROOT]
    if settings.self_improvement_local_repo_path.strip():
        candidate_paths.append(Path(settings.self_improvement_local_repo_path))

    seen_paths: set[Path] = set()
    git_sha: str | None = None
    git_branch: str | None = None
    repo_path_display: str | None = None

    for candidate in candidate_paths:
        resolved_candidate = candidate.resolve()
        if resolved_candidate in seen_paths:
            continue
        seen_paths.add(resolved_candidate)
        if not resolved_candidate.exists():
            continue
        if not (resolved_candidate / ".git").exists():
            continue
        sha = _run_git_metadata_command(resolved_candidate, "rev-parse", "--short=12", "HEAD")
        if not sha:
            continue
        git_sha = sha
        git_branch = _run_git_metadata_command(resolved_candidate, "rev-parse", "--abbrev-ref", "HEAD") or "unbekannt"
        repo_path_display = str(resolved_candidate)
        break

    build_info = _read_baked_build_info()
    build_timestamp_raw = build_info.get("build_timestamp_utc", "")
    build_timestamp_display = _format_build_timestamp(build_timestamp_raw)
    build_commit_sha = build_info.get("build_commit_sha", "")
    build_ref = build_info.get("build_git_ref", "")
    build_mismatch = bool(build_commit_sha and git_sha and build_commit_sha != git_sha)
    display_commit_sha = build_commit_sha or git_sha or ""

    display_parts = [f"Version {APP_VERSION}"]
    if display_commit_sha:
        display_parts.append(display_commit_sha)
    if build_timestamp_display:
        display_parts.append(f"Build {build_timestamp_display}")
    if build_mismatch and git_sha:
        display_parts.append(f"Host {git_sha}")

    full_parts = [f"Version {APP_VERSION}"]
    if git_branch:
        full_parts.append(f"Branch {git_branch}")
    if build_commit_sha:
        full_parts.append(f"Build-Commit {build_commit_sha}")
    if git_sha:
        full_parts.append(f"Laufender Commit {git_sha}")
    if build_ref:
        full_parts.append(f"Build-Ref {build_ref}")
    if build_timestamp_display:
        full_parts.append(f"Gebaut {build_timestamp_display}")
    if build_mismatch:
        full_parts.append("Warnung Host-Checkout und laufender Build unterscheiden sich")
    if repo_path_display:
        full_parts.append(f"Repo {repo_path_display}")

    return {
        "app_version": APP_VERSION,
        "git_sha": git_sha or "",
        "git_branch": git_branch or "",
        "repo_path": repo_path_display or "",
        "build_timestamp_utc": build_timestamp_raw,
        "build_timestamp_display": build_timestamp_display,
        "build_commit_sha": build_commit_sha,
        "build_git_ref": build_ref,
        "build_mismatch": "true" if build_mismatch else "",
        "display": " · ".join(display_parts),
        "full_label": " · ".join(full_parts),
    }


templates.env.globals["ui_build"] = _ui_build_info()


def _format_duration(seconds: float | int | None) -> str:
    """Render elapsed seconds in a compact operator-friendly format."""

    if seconds is None:
        return "unbekannt"
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _format_bytes(size: int | None) -> str:
    """Render file sizes in a compact way so debug downloads stay scannable."""

    if size is None:
        return "unbekannt"
    value = float(max(size, 0))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{int(size)} B"


def _attachment_headers(filename: str) -> dict[str, str]:
    """Return safe attachment headers for downloadable debug artifacts."""

    safe_filename = filename.replace('"', "'")
    return {"Content-Disposition": f'attachment; filename="{safe_filename}"'}


def _json_bytes(payload: Any) -> bytes:
    """Serialize debug payloads to UTF-8 JSON without crashing on datetime objects."""

    return (json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n").encode("utf-8")


def _text_bytes(text: str) -> bytes:
    """Encode plain-text debug guidance consistently."""

    return text.rstrip().encode("utf-8") + b"\n"


def _download_json_response(filename: str, payload: Any) -> Response:
    """Return a JSON attachment response for one generated debug snapshot."""

    return Response(
        content=_json_bytes(payload),
        media_type="application/json",
        headers=_attachment_headers(filename),
    )


def _download_text_response(filename: str, text: str) -> Response:
    """Return a text attachment response for shell commands or bundle notes."""

    return Response(
        content=_text_bytes(text),
        media_type="text/plain; charset=utf-8",
        headers=_attachment_headers(filename),
    )


def _zip_response(filename: str, files: dict[str, bytes]) -> Response:
    """Build a ZIP archive in memory so operators can download one reproducible debug bundle."""

    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for archive_name, content in sorted(files.items()):
            archive.writestr(archive_name, content)

    return Response(
        content=archive_buffer.getvalue(),
        media_type="application/zip",
        headers=_attachment_headers(filename),
    )


def _path_diagnostics(path: Path | None) -> dict[str, Any]:
    """Inspect path accessibility without turning permission issues into new UI crashes."""

    if path is None:
        return {"configured": False, "path": None}

    resolved = str(path)
    exists = False
    is_dir = False
    is_file = False
    readable = False
    writable = False
    size: int | None = None
    error: str | None = None

    try:
        exists = path.exists()
        is_dir = path.is_dir()
        is_file = path.is_file()
        readable = os.access(path, os.R_OK)
        writable = os.access(path, os.W_OK)
        if exists and is_file:
            size = path.stat().st_size
    except OSError as exc:
        error = f"{exc.__class__.__name__}: {exc}"

    return {
        "configured": True,
        "path": resolved,
        "exists": exists,
        "is_dir": is_dir,
        "is_file": is_file,
        "readable": readable,
        "writable": writable,
        "size_bytes": size,
        "size_display": _format_bytes(size),
        "error": error,
    }


def _known_data_store_paths() -> dict[str, Path]:
    """Return the persisted runtime files that are most useful for operator debugging."""

    return {
        "repository-access-policy.json": settings.data_dir / "repository-access-policy.json",
        "trusted_sources.json": settings.data_dir / "trusted_sources.json",
        "web_search_providers.json": settings.data_dir / "web_search_providers.json",
        "worker_guidance.json": settings.data_dir / "worker_guidance.json",
        "improvement_suggestions.json": settings.data_dir / "improvement_suggestions.json",
    }


def _runtime_summary_snapshot() -> dict[str, Any]:
    """Expose a sanitized runtime summary so operators can compare live config with expectations."""

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "service": "web-ui",
        "app_env": settings.app_env,
        "log_level": settings.log_level,
        "orchestrator_internal_url": settings.orchestrator_internal_url,
        "default_target_repo": settings.default_target_repo,
        "default_local_repo_path": settings.default_local_repo_path,
        "default_base_branch": settings.default_base_branch,
        "coding_provider": settings.coding_provider,
        "default_model_provider": settings.default_model_provider,
        "llm": {
            "mistral_base_url": settings.mistral_base_url,
            "mistral_model_name": settings.mistral_model_name,
            "qwen_base_url": settings.qwen_base_url,
            "qwen_model_name": settings.qwen_model_name,
            "timeouts": {
                "connect_seconds": settings.llm_connect_timeout_seconds,
                "read_seconds": settings.llm_read_timeout_seconds,
                "write_seconds": settings.llm_write_timeout_seconds,
                "pool_seconds": settings.llm_pool_timeout_seconds,
                "deadline_seconds": settings.llm_request_deadline_seconds,
            },
        },
        "worker_transport": {
            "connect_seconds": settings.worker_connect_timeout_seconds,
            "stage_timeout_seconds": settings.worker_stage_timeout_seconds,
            "write_seconds": settings.worker_write_timeout_seconds,
            "pool_seconds": settings.worker_pool_timeout_seconds,
            "retry_attempts": settings.worker_retry_attempts,
            "heartbeat_interval_seconds": settings.stage_heartbeat_interval_seconds,
        },
        "paths": {
            "data_dir": _path_diagnostics(settings.data_dir),
            "reports_dir": _path_diagnostics(settings.reports_dir),
            "workspace_root": _path_diagnostics(settings.workspace_root),
            "task_workspace_root": _path_diagnostics(settings.effective_task_workspace_root),
            "runtime_home_dir": _path_diagnostics(settings.runtime_home_dir),
            "staging_stack_root": _path_diagnostics(settings.staging_stack_root),
            "orchestrator_db_path": _path_diagnostics(settings.orchestrator_db_path),
            "model_api_key_file": _path_diagnostics(settings.default_model_api_key_file),
            "mistral_api_key_file": _path_diagnostics(settings.mistral_api_key_file),
            "qwen_api_key_file": _path_diagnostics(settings.qwen_api_key_file),
            "web_search_api_key_file": _path_diagnostics(settings.web_search_api_key_file),
            "github_token_file": _path_diagnostics(settings.github_token_file),
        },
        "flags": {
            "web_research_enabled": settings.web_research_enabled,
            "openhands_enabled": settings.openhands_enabled,
            "github_mcp_enabled": settings.github_mcp_enabled,
            "auto_deploy_staging": settings.auto_deploy_staging,
        },
        "worker_urls": {
            "requirements": settings.requirements_worker_url,
            "research": settings.research_worker_url,
            "architecture": settings.architecture_worker_url,
            "coding": settings.coding_worker_url,
            "reviewer": settings.reviewer_worker_url,
            "test": settings.test_worker_url,
            "security": settings.security_worker_url,
            "validation": settings.validation_worker_url,
            "documentation": settings.documentation_worker_url,
            "github": settings.github_worker_url,
            "deploy": settings.deploy_worker_url,
            "qa": settings.qa_worker_url,
            "memory": settings.memory_worker_url,
            "data": settings.data_worker_url,
            "ux": settings.ux_worker_url,
            "cost": settings.cost_worker_url,
            "human_resources": settings.human_resources_worker_url,
        },
    }


def _reports_root_manifest() -> dict[str, Any]:
    """Summarize all report folders so the debug center can show which tasks produced artifacts."""

    root = settings.reports_dir
    manifest: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "reports_dir": str(root),
        "exists": False,
        "tasks": [],
    }
    if not root.exists():
        return manifest

    tasks: list[dict[str, Any]] = []
    for task_dir in sorted((path for path in root.iterdir() if path.is_dir()), key=lambda item: item.name):
        files: list[dict[str, Any]] = []
        total_bytes = 0
        for report_path in sorted((path for path in task_dir.rglob("*") if path.is_file()), key=lambda item: item.name):
            relative_path = report_path.relative_to(task_dir).as_posix()
            size = report_path.stat().st_size
            total_bytes += size
            files.append(
                {
                    "path": relative_path,
                    "size_bytes": size,
                    "size_display": _format_bytes(size),
                }
            )
        tasks.append(
            {
                "task_id": task_dir.name,
                "file_count": len(files),
                "total_bytes": total_bytes,
                "total_size_display": _format_bytes(total_bytes),
                "files": files,
            }
        )

    manifest["exists"] = True
    manifest["tasks"] = tasks
    return manifest


def _current_worker_name(task: dict[str, Any]) -> str | None:
    """Infer the active worker from the newest event first, then fall back to the coarse task status."""

    metadata = _as_mapping(task.get("metadata"))
    worker_progress = _normalize_worker_progress(metadata.get("worker_progress"))
    active_progress_rows: list[tuple[datetime, str]] = []
    for worker_name, progress in worker_progress.items():
        if progress.get("state") not in {"running", "waiting", "blocked", "failed"}:
            continue
        parsed_updated = _parse_timestamp(progress.get("updated_at"))
        if parsed_updated is None:
            continue
        active_progress_rows.append((parsed_updated, worker_name))
    if active_progress_rows:
        active_progress_rows.sort(key=lambda item: item[0])
        return active_progress_rows[-1][1]

    for event in reversed(_as_list(task.get("events"))):
        if not isinstance(event, dict):
            continue
        details = _as_mapping(event.get("details"))
        event_worker_name = details.get("worker_name")
        if event_worker_name:
            return str(event_worker_name)
    if task.get("status") == TaskStatus.APPROVAL_REQUIRED.value and task.get("resume_target"):
        return str(task["resume_target"])
    return STATUS_TO_WORKER_HINT.get(str(task.get("status")))


def _find_last_worker_event(task: dict[str, Any], worker_name: str | None) -> dict[str, Any] | None:
    """Return the most recent event for a worker so the UI can show the last visible activity."""

    if not worker_name:
        return None
    for event in reversed(_as_list(task.get("events"))):
        if not isinstance(event, dict):
            continue
        details = _as_mapping(event.get("details"))
        if details.get("worker_name") == worker_name:
            return event
    return None


def _running_since(task: dict[str, Any], worker_name: str | None) -> datetime | None:
    """Estimate when the current worker became active by scanning the newest contiguous event block."""

    if not worker_name:
        return None
    started_at: datetime | None = None
    seen_current_worker = False
    for event in reversed(_as_list(task.get("events"))):
        if not isinstance(event, dict):
            continue
        details = _as_mapping(event.get("details"))
        event_worker = details.get("worker_name")
        if event_worker == worker_name:
            seen_current_worker = True
            parsed = _parse_timestamp(event.get("created_at"))
            if parsed is not None:
                started_at = parsed
            continue
        if seen_current_worker:
            break
    return started_at


def _is_task_archived(task: dict[str, Any]) -> bool:
    """Treat archive state defensively so old payloads and new metadata remain compatible."""

    if bool(task.get("archived")):
        return True
    metadata = _as_mapping(task.get("metadata"))
    return bool(metadata.get("archived"))


async def _load_task_lookup(task_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Load a compact task lookup so history views can label archived task references honestly."""

    normalized_ids = sorted({str(task_id).strip() for task_id in task_ids if str(task_id).strip()})
    if not normalized_ids:
        return {}

    responses = await asyncio.gather(
        *[_api_request("GET", f"/api/tasks/{task_id}") for task_id in normalized_ids],
        return_exceptions=False,
    )
    lookup: dict[str, dict[str, Any]] = {}
    for task_id, response in zip(normalized_ids, responses, strict=False):
        if response.status_code >= 400:
            continue
        payload = _response_json(response, {})
        if isinstance(payload, dict):
            lookup[task_id] = _decorate_task(payload)
    return lookup


def _build_task_reference(task_id: Any, task_lookup: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """Describe one linked task so templates can distinguish active work from archived history cleanly."""

    normalized_task_id = str(task_id or "").strip()
    if not normalized_task_id:
        return None

    task = task_lookup.get(normalized_task_id)
    archived = bool(task and task.get("archived"))
    return {
        "id": normalized_task_id,
        "short_id": f"{normalized_task_id[:8]}…",
        "exists": task is not None,
        "archived": archived,
        "status": str(task.get("status") or "") if task else "",
        "goal": str(task.get("goal") or "") if task else "",
        "archived_reason": str(task.get("archived_reason") or "") if task else "",
        "archived_at_display": str(task.get("archived_at_display") or "") if task else "",
        "href": (
            f"/archive?task_id={normalized_task_id}#task-{normalized_task_id}"
            if archived
            else f"/tasks/{normalized_task_id}"
        ),
    }


def _attach_task_reference(
    item: dict[str, Any],
    *,
    source_key: str,
    target_key: str,
    task_lookup: dict[str, dict[str, Any]],
) -> None:
    """Attach one decorated task reference without changing the original task id field."""

    item[target_key] = _build_task_reference(item.get(source_key), task_lookup)


def _is_active_worker_heartbeat(
    *,
    task_status: str,
    worker_name: str,
    current_worker: str | None,
    progress: dict[str, Any],
    latest_details: dict[str, Any],
) -> bool:
    """Treat heartbeat updates for the current active worker as active work, not passive waiting."""

    if task_status not in ACTIVE_TASK_STATUSES:
        return False
    if worker_name != current_worker:
        return False
    return bool(latest_details.get("heartbeat") or progress.get("event_kind") == "stage_heartbeat")


def _is_slow_worker_progress(progress: dict[str, Any], latest_details: dict[str, Any]) -> bool:
    """Flag one active worker as slow when it has exceeded its stored heartbeat warning budget."""

    state = str(progress.get("state") or latest_details.get("state") or "")
    if state == "slow":
        return True

    try:
        elapsed_seconds = float(progress.get("elapsed_seconds") or latest_details.get("elapsed_seconds") or 0.0)
    except (TypeError, ValueError):
        elapsed_seconds = 0.0

    try:
        slow_warning_seconds = float(
            progress.get("slow_warning_seconds") or latest_details.get("slow_warning_seconds") or 0.0
        )
    except (TypeError, ValueError):
        slow_warning_seconds = 0.0

    return slow_warning_seconds > 0 and elapsed_seconds >= slow_warning_seconds


def _normalize_active_worker_message(message: str, worker_label: str) -> str:
    """Rewrite older 'waiting' heartbeat texts into clearer 'working' language for operators."""

    normalized = str(message or "")
    waiting_prefix = f"{worker_label} wartet seit "
    if waiting_prefix in normalized and "auf Modell- oder Worker-Antwort." in normalized:
        normalized = normalized.replace(waiting_prefix, f"{worker_label} arbeitet seit ", 1)
        normalized = normalized.replace(
            " auf Modell- oder Worker-Antwort.",
            " im Hintergrund. Modell- oder Worker-Antwort steht noch aus.",
            1,
        )
    normalized = normalized.replace(" warten auf Modellantwort.", " arbeiten gerade mit lokalem Modell.")
    normalized = normalized.replace(" wartet auf Modellantwort.", " arbeitet gerade mit lokalem Modell.")
    normalized = normalized.replace(
        "Der Worker wartet noch auf eine Antwort aus dem Worker-Service oder vom lokalen Modell.",
        "Der Worker arbeitet weiter. Die Antwort aus dem Worker-Service oder vom lokalen Modell steht noch aus.",
    )
    return normalized


def _build_worker_timeline(task: dict[str, Any]) -> list[dict[str, Any]]:
    """Build a deterministic per-worker timeline so slow stages remain readable and explainable."""

    current_worker = _current_worker_name(task)
    task_status = str(task.get("status", TaskStatus.NEW.value))
    completed_workers = set(_normalize_worker_results(task.get("worker_results")).keys())
    worker_results = _normalize_worker_results(task.get("worker_results"))
    worker_progress = _normalize_worker_progress(_as_mapping(_as_mapping(task.get("metadata")).get("worker_progress")))
    completed_indices = [WORKER_SEQUENCE_INDEX[name] for name in completed_workers if name in WORKER_SEQUENCE_INDEX]
    last_completed_index = max(completed_indices) if completed_indices else -1
    current_index = WORKER_SEQUENCE_INDEX.get(current_worker or "", -1)
    timeline: list[dict[str, Any]] = []

    for step in WORKER_SEQUENCE:
        worker_name = step["worker_name"]
        latest_event = _find_last_worker_event(task, worker_name)
        latest_details = _as_mapping(latest_event.get("details")) if latest_event else {}
        progress = worker_progress.get(worker_name, {})
        result = worker_results.get(worker_name, {})
        active_heartbeat = _is_active_worker_heartbeat(
            task_status=task_status,
            worker_name=worker_name,
            current_worker=current_worker,
            progress=progress,
            latest_details=latest_details,
        )
        slow_worker = _is_slow_worker_progress(progress, latest_details)
        state = "waiting"
        if progress.get("state") in WORKER_STATE_LABELS:
            state = str(progress["state"])
        elif worker_name in completed_workers:
            state = "complete"
        elif task_status == TaskStatus.FAILED.value and worker_name == current_worker:
            state = "failed"
        elif task_status == TaskStatus.APPROVAL_REQUIRED.value and worker_name == current_worker:
            state = "blocked"
        elif active_heartbeat and slow_worker:
            state = "slow"
        elif active_heartbeat:
            state = "running"
        elif task_status in ACTIVE_TASK_STATUSES and worker_name == current_worker:
            state = "running"
        elif current_index >= 0 and WORKER_SEQUENCE_INDEX[worker_name] > current_index:
            state = "queued"
        elif last_completed_index > WORKER_SEQUENCE_INDEX[worker_name]:
            state = "skipped"
        else:
            state = "idle"

        if active_heartbeat and slow_worker and state in {"waiting", "running"}:
            state = "slow"
        if active_heartbeat and state == "waiting":
            state = "running"

        running_since = _running_since(task, worker_name)
        waiting_for = str(progress.get("waiting_for") or "")
        if not waiting_for and state == "queued" and current_worker:
            waiting_for = WORKER_LABELS.get(current_worker, current_worker)
        if not waiting_for and state == "blocked":
            waiting_for = "menschliche Freigabe"
        next_worker = str(progress.get("next_worker") or "")
        next_worker_label = WORKER_LABELS.get(next_worker, next_worker) if next_worker else ""
        waiting_for_label = WORKER_LABELS.get(waiting_for, waiting_for) if waiting_for else ""
        last_result_summary = str(progress.get("last_result_summary") or result.get("summary") or "")
        last_error = str(progress.get("last_error") or ("; ".join(result.get("errors", [])) if result.get("errors") else ""))
        progress_message = str(progress.get("progress_message") or (latest_event.get("message") if latest_event else step["description"]))
        if active_heartbeat:
            progress_message = _normalize_active_worker_message(progress_message, step["label"])
        current_instruction = str(
            progress.get("current_instruction")
            or progress.get("current_prompt_summary")
            or step["description"]
        )

        timeline.append(
            {
                **step,
                "state": state,
                "state_label": WORKER_STATE_LABELS.get(state, state),
                "state_icon": WORKER_STATE_ICONS.get(state, ""),
                "last_event_at_display": _format_timestamp(latest_event.get("created_at")) if latest_event else "noch keine Aktivitaet",
                "last_event_message": latest_event.get("message") if latest_event else "Noch kein Ereignis fuer diesen Schritt vorhanden.",
                "started_at_display": (
                    progress.get("started_at_display")
                    or (_format_timestamp(running_since) if running_since else "noch nicht gestartet")
                ),
                "elapsed_display": progress.get("elapsed_display") or "noch nicht sichtbar",
                "waiting_for_display": waiting_for_label or "niemanden",
                "next_worker_label": next_worker_label,
                "current_instruction": current_instruction,
                "progress_message": progress_message,
                "last_result_summary": last_result_summary or "noch kein Ergebnis",
                "last_error": last_error,
                "event_kind": str(progress.get("event_kind") or latest_details.get("event_kind") or "note"),
            }
        )
    return timeline


def _decorate_events(task: dict[str, Any]) -> list[dict[str, Any]]:
    """Prepare event timestamps and badges for the template without mutating the API contract."""

    decorated: list[dict[str, Any]] = []
    for event in _normalize_events(task.get("events")):
        details = _as_mapping(event.get("details"))
        state = str(details.get("state") or "note")
        if bool(details.get("heartbeat")) and state in {"waiting", "note"}:
            state = "running"
        if _is_slow_worker_progress(details, details):
            state = "slow"
        waiting_for = str(details.get("waiting_for") or "")
        progress_message = str(details.get("progress_message") or event.get("message") or "")
        worker_label = WORKER_LABELS.get(str(details.get("worker_name")), str(details.get("worker_name", "")))
        if bool(details.get("heartbeat")) and state in {"running", "slow"} and worker_label:
            progress_message = _normalize_active_worker_message(progress_message, worker_label)
        decorated.append(
            {
                **event,
                "created_at_display": _format_timestamp(event.get("created_at")),
                "level_lower": str(event.get("level", "info")).lower(),
                "worker_label": worker_label,
                "is_heartbeat": bool(details.get("heartbeat")),
                "state": state,
                "event_kind": str(details.get("event_kind") or "note"),
                "state_label": WORKER_STATE_LABELS.get(state, ""),
                "progress_message": progress_message,
                "waiting_for_display": WORKER_LABELS.get(waiting_for, waiting_for) if waiting_for else "",
            }
        )
    return decorated


def _build_worker_cast(task: dict[str, Any]) -> list[dict[str, Any]]:
    """Build a visual worker overview with avatar cards and short thought or speech bubbles."""

    current_worker = _current_worker_name(task)
    worker_results = _normalize_worker_results(task.get("worker_results"))
    cast: list[dict[str, Any]] = []

    for step in _build_worker_timeline(task):
        worker_name = step["worker_name"]
        result = worker_results.get(worker_name, {})
        state = str(step["state"])
        bubble_kind = "quiet"
        bubble_text = step["progress_message"]

        if state in {"running", "slow"}:
            bubble_kind = "thought"
        elif state == "waiting":
            bubble_kind = "coffee"
            bubble_text = (
                f"Wartet auf {step['waiting_for_display']}."
                if step["waiting_for_display"] != "niemanden"
                else step["progress_message"]
            )
        elif state == "complete":
            bubble_kind = "speech"
            bubble_text = str(result.get("summary") or step["last_result_summary"])
        elif state == "blocked":
            bubble_kind = "speech"
            bubble_text = str(task.get("approval_reason") or step["progress_message"])
        elif state == "failed":
            bubble_kind = "speech"
            bubble_text = str(task.get("latest_error") or step["last_error"] or step["progress_message"])
        elif state == "queued":
            bubble_kind = "quiet"
            bubble_text = f"Wartet auf {step['waiting_for_display']}."
        elif state in {"idle", "skipped"}:
            bubble_kind = "sleep"
            bubble_text = "Dieser Worker wird fuer die aktuelle Aufgabe derzeit nicht aktiv genutzt."

        cast.append(
            {
                **step,
                "initials": _worker_initials(step["label"]),
                "state_icon": WORKER_STATE_ICONS.get(state, ""),
                "bubble_kind": bubble_kind,
                "bubble_text": bubble_text,
                "activity_display": step["last_event_at_display"],
                "is_current_worker": worker_name == current_worker,
                "directed_to": step.get("next_worker_label", ""),
                "current_instruction": step["current_instruction"],
                "last_result_summary": step["last_result_summary"],
                "last_error": step["last_error"],
                "waiting_for_display": step["waiting_for_display"],
                "started_at_display": step["started_at_display"],
                "elapsed_display": step["elapsed_display"],
                "state_label": step["state_label"],
                "event_kind": step["event_kind"],
            }
        )
    return cast


def _group_worker_cast(cast: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Split workers into compact theatre sections so active, waiting, paused, and unused workers are easier to scan."""

    groups = (
        ("active", "Aktiv", "Worker, die gerade rechnen oder sichtbar arbeiten.", {"running", "slow"}),
        ("waiting", "Wartet", "Worker, die aktuell auf Modell-, Service- oder Stage-Antworten warten.", {"waiting", "queued"}),
        ("paused", "Pausiert / Fehler", "Worker, die geblockt wurden oder Hilfe brauchen.", {"blocked", "failed"}),
        ("done", "Bereits fertig", "Worker, die ihren Teil dieser Aufgabe schon geliefert haben.", {"complete"}),
        ("unused", "Derzeit ungenutzt", "Worker, die fuer diese Aufgabe gerade nicht aktiv eingeplant sind.", {"idle", "skipped"}),
    )
    sections: list[dict[str, Any]] = []
    for key, label, description, states in groups:
        members = [worker for worker in cast if worker.get("state") in states]
        sections.append({"key": key, "label": label, "description": description, "workers": members})
    return sections


def _build_restartable_stage_options(task: dict[str, Any], worker_timeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Offer only the already reached workflow stages for partial restarts so operators do not need a brand-new task."""

    current_worker = _current_worker_name(task)
    metadata = _as_mapping(task.get("metadata"))
    last_restart = _as_mapping(metadata.get("last_restart_request"))
    selected_worker_name = str(last_restart.get("worker_name") or current_worker or "")

    relevant_indices: list[int] = []
    for worker_name in _normalize_worker_results(task.get("worker_results")):
        if worker_name in WORKER_SEQUENCE_INDEX:
            relevant_indices.append(WORKER_SEQUENCE_INDEX[worker_name])
    for worker_name in _normalize_worker_progress(metadata.get("worker_progress")):
        if worker_name in WORKER_SEQUENCE_INDEX:
            relevant_indices.append(WORKER_SEQUENCE_INDEX[worker_name])
    if current_worker in WORKER_SEQUENCE_INDEX:
        relevant_indices.append(WORKER_SEQUENCE_INDEX[current_worker])

    if not relevant_indices:
        return []

    max_index = max(relevant_indices)
    options: list[dict[str, Any]] = []
    for step in worker_timeline:
        worker_name = str(step.get("worker_name") or "")
        worker_index = WORKER_SEQUENCE_INDEX.get(worker_name)
        if worker_index is None or worker_index > max_index:
            continue
        if step.get("state") in {"idle", "queued"} and worker_name != current_worker:
            continue
        options.append(
            {
                "worker_name": worker_name,
                "label": step.get("label", worker_name),
                "description": step.get("description", ""),
                "state": step.get("state", "waiting"),
                "state_label": step.get("state_label", "wartet"),
                "selected": worker_name == selected_worker_name or (not selected_worker_name and worker_name == current_worker),
            }
        )
    return options


def _decorate_task(task: dict[str, Any]) -> dict[str, Any]:
    """Enrich a raw task payload with operator-focused progress details for the dashboard and detail page."""

    decorated = dict(task)
    decorated["metadata"] = _as_mapping(decorated.get("metadata"))
    decorated["worker_results"] = _normalize_worker_results(decorated.get("worker_results"))
    decorated["worker_progress"] = _normalize_worker_progress(decorated["metadata"].get("worker_progress"))
    decorated["events"] = _normalize_events(decorated.get("events"))
    decorated["risk_flags"] = [str(item) for item in _as_list(decorated.get("risk_flags"))]
    decorated["archived"] = _is_task_archived(decorated)
    decorated["archived_at"] = _parse_timestamp(decorated.get("archived_at") or decorated["metadata"].get("archived_at"))
    decorated["archived_by"] = str(decorated.get("archived_by") or decorated["metadata"].get("archived_by") or "")
    decorated["archived_reason"] = str(
        decorated.get("archived_reason") or decorated["metadata"].get("archived_reason") or ""
    )
    decorated["archived_at_display"] = (
        _format_timestamp(decorated["archived_at"]) if decorated["archived_at"] else "noch nicht archiviert"
    )
    current_worker = _current_worker_name(decorated)
    current_progress = decorated["worker_progress"].get(current_worker or "", {})
    last_event = decorated["events"][-1] if decorated.get("events") else None
    last_event_details = _as_mapping(last_event.get("details")) if last_event else {}
    running_since = _running_since(decorated, current_worker)
    if running_since is not None:
        running_for_seconds = round((datetime.now(UTC) - running_since).total_seconds(), 1)
    else:
        running_for_seconds = None

    decorated["events"] = _decorate_events(decorated)
    decorated["worker_timeline"] = _build_worker_timeline(decorated)
    decorated["worker_cast"] = _build_worker_cast(decorated)
    decorated["worker_cast_groups"] = _group_worker_cast(decorated["worker_cast"])
    decorated["restartable_stage_options"] = _build_restartable_stage_options(decorated, decorated["worker_timeline"])
    decorated["can_restart_partially"] = bool(decorated["restartable_stage_options"])
    decorated["status_lower"] = str(decorated.get("status", "")).lower()
    decorated["created_at_display"] = _format_timestamp(decorated.get("created_at"))
    decorated["updated_at_display"] = _format_timestamp(decorated.get("updated_at"))
    decorated["last_activity_at_display"] = (
        _format_timestamp(last_event.get("created_at")) if last_event else decorated["updated_at_display"]
    )
    decorated["current_worker_name"] = current_worker
    decorated["current_worker_label"] = WORKER_LABELS.get(current_worker or "", current_worker or "noch keiner sichtbar")
    decorated["current_stage_label"] = WORKER_LABELS.get(current_worker or "", "Wartet auf den naechsten Schritt")
    decorated["current_stage_description"] = WORKER_DESCRIPTIONS.get(
        current_worker or "",
        "Der Workflow wartet auf den naechsten sinnvollen Arbeitsschritt.",
    )
    current_stage_state = str(
        current_progress.get("state")
        or (
            "blocked"
            if decorated.get("status") == TaskStatus.APPROVAL_REQUIRED.value
            else "failed"
            if decorated.get("status") == TaskStatus.FAILED.value
            else "running"
            if decorated.get("status") in ACTIVE_TASK_STATUSES
            else "complete"
            if decorated.get("status") == TaskStatus.DONE.value
            else "waiting"
        )
    )
    if _is_active_worker_heartbeat(
        task_status=str(decorated.get("status") or ""),
        worker_name=current_worker or "",
        current_worker=current_worker,
        progress=current_progress,
        latest_details=last_event_details,
    ):
        if _is_slow_worker_progress(current_progress, last_event_details):
            current_stage_state = "slow"
        elif current_stage_state == "waiting":
            current_stage_state = "running"
    decorated["current_stage_state"] = current_stage_state
    decorated["current_stage_state_label"] = WORKER_STATE_LABELS.get(
        current_stage_state,
        current_stage_state,
    )
    decorated["running_since_display"] = _format_timestamp(running_since) if running_since else "noch nicht sichtbar"
    decorated["running_for_display"] = current_progress.get("elapsed_display") or _format_duration(running_for_seconds)
    decorated["current_instruction"] = str(
        current_progress.get("current_instruction")
        or current_progress.get("current_prompt_summary")
        or decorated["current_stage_description"]
    )
    current_progress_message = str(
        current_progress.get("progress_message") or (last_event.get("message") if last_event else "Noch keine Detailmeldung sichtbar.")
    )
    if current_worker and decorated["current_stage_state"] in {"running", "slow"}:
        current_progress_message = _normalize_active_worker_message(
            current_progress_message,
            decorated["current_stage_label"],
        )
    decorated["current_progress_message"] = current_progress_message
    decorated["current_waiting_for_display"] = WORKER_LABELS.get(
        str(current_progress.get("waiting_for") or ""),
        str(current_progress.get("waiting_for") or ""),
    ) or "niemanden"
    decorated["current_last_result_summary"] = str(current_progress.get("last_result_summary") or "noch kein Ergebnis")
    decorated["current_last_error"] = str(current_progress.get("last_error") or decorated.get("latest_error") or "")
    last_restart = _as_mapping(decorated["metadata"].get("last_restart_request"))
    restarted_worker_name = str(last_restart.get("worker_name") or "")
    decorated["last_restart"] = last_restart
    decorated["last_restart_display"] = (
        f"{WORKER_LABELS.get(restarted_worker_name, restarted_worker_name)} · {_format_timestamp(last_restart.get('requested_at'))}"
        if restarted_worker_name
        else "noch kein Teil-Neustart"
    )
    decorated["events_latest_first"] = list(reversed(decorated["events"]))
    decorated["is_active"] = str(decorated.get("status")) in ACTIVE_TASK_STATUSES and not decorated["archived"]
    decorated["auto_refresh_seconds"] = AUTO_REFRESH_SECONDS if decorated["is_active"] else 0
    return decorated


def _clip_text(value: str, max_length: int = 140) -> str:
    """Keep long benchmark snippets readable in compact cards and tables."""

    compact = " ".join(str(value or "").split())
    if len(compact) <= max_length:
        return compact or "—"
    return compact[: max_length - 1].rstrip() + "…"


def _text_metrics(text: str) -> dict[str, int]:
    """Measure visible text size honestly without pretending to know real token usage."""

    normalized = " ".join(str(text or "").split())
    return {
        "chars": len(normalized),
        "words": len(normalized.split()) if normalized else 0,
    }


def _format_ratio(numerator: float | None, denominator: float | None) -> str:
    """Render a compact ratio while avoiding division-by-zero and misleading noise."""

    if numerator is None or denominator is None or denominator <= 0:
        return "n/a"
    return f"{numerator / denominator:.2f}x"


def _benchmark_recommendations(summary: dict[str, Any]) -> list[str]:
    """Translate raw worker metrics into clear follow-up ideas for HR, QA, or prompt tuning."""

    recommendations: list[str] = []
    run_count = int(summary.get("run_count") or 0)
    failed_count = int(summary.get("failed_count") or 0)
    active_count = int(summary.get("active_count") or 0)
    avg_duration = summary.get("average_duration_seconds")
    warning_total = int(summary.get("warning_total") or 0)

    if run_count == 0:
        return ["Noch keine belastbaren Laufdaten vorhanden."]
    if failed_count >= 2 and (summary.get("failure_rate") or 0.0) >= 0.34:
        recommendations.append("QA sollte die haeufigsten Fehlerbilder und deren Reproduzierbarkeit priorisieren.")
    if isinstance(avg_duration, (int, float)) and avg_duration >= 300:
        recommendations.append("HR oder QA sollte diesen Langlaeufer in kleinere, besser sichtbare Zwischenschritte zerlegen.")
    if warning_total >= max(2, run_count):
        recommendations.append("Die Worker-Guidance sollte geschaerft werden, weil fast jeder Lauf Warnungen produziert.")
    if active_count > 0:
        recommendations.append("Es gibt noch laufende oder wartende Durchgaenge. Die Sichtbarkeit im Worker-Theater weiter beobachten.")
    if not recommendations:
        recommendations.append("Der Worker wirkt in den sichtbaren Laufdaten derzeit stabil und nachvollziehbar.")
    return recommendations


def _worker_run_records(task: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten one decorated task into benchmarkable worker runs with readable input, outcome, and runtime data."""

    worker_results = _normalize_worker_results(task.get("worker_results"))
    worker_progress = _normalize_worker_progress(_as_mapping(_as_mapping(task.get("metadata")).get("worker_progress")))
    runs: list[dict[str, Any]] = []

    for step in _build_worker_timeline(task):
        worker_name = str(step.get("worker_name") or "")
        progress = worker_progress.get(worker_name, {})
        result = worker_results.get(worker_name, {})
        last_event = _find_last_worker_event(task, worker_name)
        if not progress and not result and last_event is None:
            continue

        visible_input = str(progress.get("current_prompt_summary") or progress.get("current_instruction") or "")
        visible_output = str(progress.get("last_result_summary") or result.get("summary") or "")
        input_metrics = _text_metrics(visible_input)
        output_metrics = _text_metrics(visible_output)
        elapsed_seconds = progress.get("elapsed_seconds") if isinstance(progress.get("elapsed_seconds"), (int, float)) else None
        model_route = _as_mapping(progress.get("model_route"))
        errors = [str(item) for item in _as_list(result.get("errors")) if str(item).strip()]
        warnings = [str(item) for item in _as_list(result.get("warnings")) if str(item).strip()]
        risk_flags = [str(item) for item in _as_list(result.get("risk_flags")) if str(item).strip()]
        artifacts = _as_list(result.get("artifacts"))
        last_error = str(progress.get("last_error") or ("; ".join(errors) if errors else ""))
        finished_at = _parse_timestamp(progress.get("updated_at")) or (
            _parse_timestamp(last_event.get("created_at")) if last_event else None
        ) or _parse_timestamp(task.get("updated_at"))
        started_at = _parse_timestamp(progress.get("started_at")) or _running_since(task, worker_name)

        runs.append(
            {
                "task_id": task.get("id"),
                "task_goal": str(task.get("goal") or ""),
                "task_goal_preview": _clip_text(str(task.get("goal") or ""), max_length=96),
                "repository": str(task.get("repository") or ""),
                "task_href": f"/tasks/{task.get('id')}",
                "worker_name": worker_name,
                "worker_label": WORKER_LABELS.get(worker_name, worker_name),
                "worker_description": WORKER_DESCRIPTIONS.get(worker_name, ""),
                "state": str(step.get("state") or "idle"),
                "state_label": str(step.get("state_label") or "unbekannt"),
                "state_icon": str(step.get("state_icon") or ""),
                "started_at": started_at.isoformat() if started_at else None,
                "started_at_display": _format_timestamp(started_at) if started_at else "nicht sichtbar",
                "finished_at": finished_at.isoformat() if finished_at else None,
                "finished_at_display": _format_timestamp(finished_at) if finished_at else "nicht sichtbar",
                "elapsed_seconds": float(elapsed_seconds) if elapsed_seconds is not None else None,
                "elapsed_display": _format_duration(float(elapsed_seconds)) if elapsed_seconds is not None else "nicht sichtbar",
                "visible_input": visible_input or "—",
                "visible_input_preview": _clip_text(visible_input),
                "visible_input_chars": input_metrics["chars"],
                "visible_input_words": input_metrics["words"],
                "visible_output": visible_output or "—",
                "visible_output_preview": _clip_text(visible_output),
                "visible_output_chars": output_metrics["chars"],
                "visible_output_words": output_metrics["words"],
                "output_input_ratio_display": _format_ratio(
                    float(output_metrics["chars"]) if output_metrics["chars"] else None,
                    float(input_metrics["chars"]) if input_metrics["chars"] else None,
                ),
                "provider": str(model_route.get("provider") or "unbekannt"),
                "model_name": str(model_route.get("model_name") or "unbekannt"),
                "base_url": str(model_route.get("base_url") or ""),
                "model_display": (
                    f"{model_route.get('provider')} / {model_route.get('model_name')}"
                    if model_route.get("provider") and model_route.get("model_name")
                    else str(model_route.get("model_name") or model_route.get("provider") or "unbekannt")
                ),
                "request_timeout_seconds": model_route.get("request_timeout_seconds"),
                "warning_count": len(warnings),
                "error_count": len(errors),
                "artifact_count": len(artifacts),
                "risk_flag_count": len(risk_flags),
                "warnings": warnings,
                "errors": errors,
                "risk_flags": risk_flags,
                "current_instruction": str(progress.get("current_instruction") or ""),
                "progress_message": str(progress.get("progress_message") or step.get("progress_message") or ""),
                "waiting_for": str(progress.get("waiting_for") or ""),
                "waiting_for_display": str(step.get("waiting_for_display") or "niemanden"),
                "last_error": last_error,
                "last_result_summary": visible_output or "—",
                "successful": str(step.get("state") or "") == "complete" and not errors,
            }
        )

    return runs


def _build_worker_benchmark_report(
    tasks: list[dict[str, Any]],
    skipped_tasks: list[dict[str, Any]] | None = None,
    *,
    reset_at: datetime | None = None,
    hidden_tasks_before_reset: int = 0,
) -> dict[str, Any]:
    """Aggregate readable worker benchmarks from persisted task details and progress events."""

    skipped = skipped_tasks or []
    runs = [run for task in tasks for run in _worker_run_records(task)]
    recent_runs = sorted(
        runs,
        key=lambda run: _parse_timestamp(run.get("finished_at") or run.get("started_at")) or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    durations = [run["elapsed_seconds"] for run in runs if isinstance(run.get("elapsed_seconds"), float)]
    active_runs = [run for run in runs if run["state"] in {"running", "waiting", "blocked", "queued"}]
    failed_runs = [run for run in runs if run["state"] == "failed"]
    completed_runs = [run for run in runs if run["state"] == "complete"]

    worker_summaries: list[dict[str, Any]] = []
    for step in WORKER_SEQUENCE:
        worker_name = step["worker_name"]
        worker_runs = [run for run in runs if run["worker_name"] == worker_name]
        worker_durations = [run["elapsed_seconds"] for run in worker_runs if isinstance(run.get("elapsed_seconds"), float)]
        input_sizes = [run["visible_input_chars"] for run in worker_runs if run["visible_input_chars"] > 0]
        output_sizes = [run["visible_output_chars"] for run in worker_runs if run["visible_output_chars"] > 0]
        error_counter = Counter(run["last_error"] for run in worker_runs if run["last_error"])
        waiting_counter = Counter(run["waiting_for_display"] for run in worker_runs if run["waiting_for_display"] not in {"", "niemanden"})
        model_counter = Counter(run["model_display"] for run in worker_runs if run["model_display"] != "unbekannt")
        repo_counter = Counter(run["repository"] for run in worker_runs if run["repository"])
        failed_count = sum(1 for run in worker_runs if run["state"] == "failed")
        completed_count = sum(1 for run in worker_runs if run["state"] == "complete")
        active_count = sum(1 for run in worker_runs if run["state"] in {"running", "waiting", "blocked", "queued"})
        warning_total = sum(run["warning_count"] for run in worker_runs)
        error_total = sum(run["error_count"] for run in worker_runs)
        artifact_total = sum(run["artifact_count"] for run in worker_runs)
        risk_flag_total = sum(run["risk_flag_count"] for run in worker_runs)
        success_denominator = completed_count + failed_count
        success_rate = (completed_count / success_denominator) if success_denominator else 0.0
        failure_rate = (failed_count / success_denominator) if success_denominator else 0.0
        worker_runs_sorted = sorted(
            worker_runs,
            key=lambda run: _parse_timestamp(run.get("finished_at") or run.get("started_at")) or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )

        summary = {
            "worker_name": worker_name,
            "label": step["label"],
            "description": step["description"],
            "run_count": len(worker_runs),
            "completed_count": completed_count,
            "failed_count": failed_count,
            "active_count": active_count,
            "warning_total": warning_total,
            "error_total": error_total,
            "artifact_total": artifact_total,
            "risk_flag_total": risk_flag_total,
            "average_duration_seconds": mean(worker_durations) if worker_durations else None,
            "median_duration_seconds": median(worker_durations) if worker_durations else None,
            "max_duration_seconds": max(worker_durations) if worker_durations else None,
            "average_duration_display": _format_duration(mean(worker_durations)) if worker_durations else "noch keine Dauer",
            "median_duration_display": _format_duration(median(worker_durations)) if worker_durations else "noch keine Dauer",
            "max_duration_display": _format_duration(max(worker_durations)) if worker_durations else "noch keine Dauer",
            "average_input_chars": round(mean(input_sizes), 1) if input_sizes else 0.0,
            "average_output_chars": round(mean(output_sizes), 1) if output_sizes else 0.0,
            "average_output_input_ratio_display": _format_ratio(
                mean(output_sizes) if output_sizes else None,
                mean(input_sizes) if input_sizes else None,
            ),
            "success_rate": success_rate,
            "success_rate_display": f"{success_rate * 100:.0f}%" if success_denominator else "n/a",
            "failure_rate": failure_rate,
            "failure_rate_display": f"{failure_rate * 100:.0f}%" if success_denominator else "n/a",
            "primary_model": model_counter.most_common(1)[0][0] if model_counter else "noch keine Modelldaten",
            "top_error": error_counter.most_common(1)[0][0] if error_counter else "kein dominanter Fehler",
            "top_waiting_reason": waiting_counter.most_common(1)[0][0] if waiting_counter else "kein typischer Wartegrund",
            "main_repository": repo_counter.most_common(1)[0][0] if repo_counter else "noch kein Repository",
            "recent_runs": worker_runs_sorted[:5],
            "health_tone": (
                "error"
                if failed_count >= 2 and failure_rate >= 0.34
                else "warning"
                if active_count > 0 or failed_count > 0 or (worker_durations and mean(worker_durations) >= 300)
                else "ok"
                if worker_runs
                else "idle"
            ),
        }
        summary["recommendations"] = _benchmark_recommendations(summary)
        worker_summaries.append(summary)

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "reset_at": reset_at.isoformat() if reset_at else None,
        "benchmark_window_active": reset_at is not None,
        "hidden_tasks_before_reset": hidden_tasks_before_reset,
        "total_tasks": len(tasks),
        "skipped_task_count": len(skipped),
        "skipped_tasks": skipped,
        "total_runs": len(runs),
        "active_runs": len(active_runs),
        "failed_runs": len(failed_runs),
        "completed_runs": len(completed_runs),
        "average_duration_display": _format_duration(mean(durations)) if durations else "noch keine Dauer",
        "median_duration_display": _format_duration(median(durations)) if durations else "noch keine Dauer",
        "worker_summaries": worker_summaries,
        "recent_runs": recent_runs[:40],
    }


def _decorate_worker_probe_registry(payload: dict[str, Any]) -> dict[str, Any]:
    """Turn raw worker-probe API payloads into readable benchmark cards and status facts."""

    runs: list[dict[str, Any]] = []
    for raw_run in _as_list(payload.get("runs")):
        run = _as_mapping(raw_run)
        raw_results = [_as_mapping(item) for item in _as_list(run.get("results"))]
        decorated_results: list[dict[str, Any]] = []
        for raw_result in raw_results:
            response_text = str(raw_result.get("response_text") or "").strip()
            summary = str(raw_result.get("summary") or "Keine Zusammenfassung sichtbar.")
            status = str(raw_result.get("status") or "unknown")
            worker_name = str(raw_result.get("worker_name") or "")
            started_at = raw_result.get("started_at")
            completed_at = raw_result.get("completed_at")
            decorated_results.append(
                {
                    **raw_result,
                    "worker_name": worker_name,
                    "worker_label": str(raw_result.get("worker_label") or WORKER_LABELS.get(worker_name, worker_name)),
                    "status": status,
                    "status_label": (
                        "ok"
                        if status == "ok"
                        else "Fehler"
                        if status == "failed"
                        else "laeuft"
                        if status == "running"
                        else "unbekannt"
                    ),
                    "status_tone": "done" if status == "ok" else "failed" if status == "failed" else "blocked",
                    "summary": summary,
                    "summary_preview": _clip_text(summary, max_length=180),
                    "response_text": response_text or "Noch keine Modellantwort sichtbar.",
                    "response_preview": _clip_text(response_text or summary, max_length=240),
                    "response_line_count": max(1, len((response_text or summary).splitlines())),
                    "started_at_display": _format_timestamp(started_at) if started_at else "nicht gestartet",
                    "completed_at_display": _format_timestamp(completed_at) if completed_at else "noch offen",
                    "elapsed_display": _format_duration(raw_result.get("elapsed_seconds")),
                    "model_display": (
                        f"{raw_result.get('provider')} / {raw_result.get('model_name')}"
                        if raw_result.get("provider") and raw_result.get("model_name")
                        else str(raw_result.get("model_name") or raw_result.get("provider") or "unbekannt")
                    ),
                }
            )

        status = str(run.get("status") or "unknown")
        probe_mode = str(run.get("probe_mode") or WorkerProbeMode.FULL.value)
        selected_workers = [
            worker_name
            for worker_name in _as_list(run.get("selected_workers"))
            if isinstance(worker_name, str) and worker_name.strip()
        ] or list(PROBE_WORKERS)
        selected_worker_labels = [
            PROBE_WORKER_LABELS.get(worker_name, WORKER_LABELS.get(worker_name, worker_name))
            for worker_name in selected_workers
        ]
        focus_paths = [
            path
            for path in _as_list(run.get("focus_paths"))
            if isinstance(path, str) and path.strip()
        ]
        runs.append(
            {
                **run,
                "status": status,
                "probe_mode": probe_mode,
                "selected_workers": selected_workers,
                "focus_paths": focus_paths,
                "selected_worker_labels": selected_worker_labels,
                "selected_worker_count": len(selected_workers),
                "selected_worker_summary": ", ".join(selected_worker_labels),
                "probe_mode_label": {
                    WorkerProbeMode.FULL.value: "Normaler Probelauf",
                    WorkerProbeMode.OK_CONTRACT.value: "OK-Kurztest",
                    WorkerProbeMode.MICRO_FIX.value: "README-Mini-Fix",
                }.get(probe_mode, probe_mode),
                "status_label": {
                    "queued": "wartet",
                    "running": "laeuft",
                    "completed": "abgeschlossen",
                    "failed": "fehlgeschlagen",
                }.get(status, status),
                "status_tone": {
                    "queued": "idle",
                    "running": "blocked",
                    "completed": "done",
                    "failed": "failed",
                }.get(status, "idle"),
                "created_at_display": _format_timestamp(run.get("created_at")) if run.get("created_at") else "unbekannt",
                "started_at_display": _format_timestamp(run.get("started_at")) if run.get("started_at") else "noch nicht",
                "updated_at_display": _format_timestamp(run.get("updated_at")) if run.get("updated_at") else "unbekannt",
                "completed_at_display": _format_timestamp(run.get("completed_at")) if run.get("completed_at") else "noch offen",
                "probe_goal": str(run.get("probe_goal") or DEFAULT_WORKER_PROBE_GOAL),
                "probe_goal_preview": _clip_text(str(run.get("probe_goal") or DEFAULT_WORKER_PROBE_GOAL), max_length=180),
                "active_worker_label": WORKER_LABELS.get(
                    str(run.get("active_worker_name") or ""),
                    str(run.get("active_worker_name") or "niemand"),
                ),
                "results": decorated_results,
                "errors": [str(item) for item in _as_list(run.get("errors")) if str(item).strip()],
            }
        )

    latest = runs[0] if runs else None
    latest_results = latest["results"] if latest else []
    latest_ok = sum(1 for item in latest_results if item["status"] == "ok")
    latest_failed = sum(1 for item in latest_results if item["status"] == "failed")
    return {
        "runs": runs,
        "latest_run": latest,
        "latest_results": latest_results,
        "overview": {
            "run_count": len(runs),
            "active_run_count": sum(1 for run in runs if run["status"] in {"queued", "running"}),
            "latest_completed_workers": latest_ok,
            "latest_failed_workers": latest_failed,
            "latest_total_workers": int(latest.get("total_workers") or 0) if latest else 0,
        },
    }


def _build_worker_test_choices(*, selected_workers: list[str] | None = None) -> list[dict[str, Any]]:
    """Describe the available targeted worker tests for the dedicated UI page."""

    selected = set(selected_workers or ["coding"])
    choices: list[dict[str, Any]] = []
    for worker_name in PROBE_WORKERS:
        definition = PROBE_DEFINITIONS[worker_name]
        provider, _route = resolve_worker_route(settings, worker_name)
        fallback_provider = resolve_fallback_provider(settings, worker_name)
        label = PROBE_WORKER_LABELS.get(worker_name, WORKER_LABELS.get(worker_name, worker_name))
        quick_goal = (
            f"Leerer OK-Kurztest nur fuer den Worker {label} ohne Repository-Aenderungen."
        )
        focused_goal = (
            f"Kurzer Teiltest nur fuer den Worker {label}. "
            "Pruefe den zuletzt geaenderten Bereich ohne Repository-Aenderungen."
        )
        micro_fix_goal = DEFAULT_MICRO_FIX_WORKER_PROBE_GOAL
        choices.append(
            {
                "worker_name": worker_name,
                "label": label,
                "description": WORKER_DESCRIPTIONS.get(worker_name, label),
                "output_contract": definition.output_contract,
                "response_format": definition.response_format,
                "primary_model": f"{provider.name} / {provider.model_name}",
                "fallback_model": (
                    f"{fallback_provider.name} / {fallback_provider.model_name}" if fallback_provider else "kein Fallback"
                ),
                "quick_ok_goal": quick_goal,
                "quick_full_goal": focused_goal,
                "quick_micro_fix_goal": micro_fix_goal,
                "supports_micro_fix": worker_name == "coding",
                "checked": worker_name in selected,
            }
        )
    return choices


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(service="web-ui")


def _split_lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def _default_source_form_values() -> dict[str, Any]:
    return {
        "id": "",
        "name": "",
        "domain": "",
        "category": "official_docs",
        "enabled": False,
        "priority": 100,
        "source_type": "docs",
        "preferred_access": "html",
        "base_url": "",
        "api_description": "",
        "auth_type": "none",
        "auth_env_var": "",
        "rate_limit_notes": "",
        "usage_instructions": "",
        "allowed_paths_text": "",
        "deny_paths_text": "",
        "tags_text": "",
    }


def _default_provider_form_values() -> dict[str, Any]:
    return {
        "id": "",
        "name": "",
        "provider_type": "searxng",
        "enabled": False,
        "priority": 100,
        "base_url": "",
        "search_path": "/search",
        "method": "GET",
        "auth_type": "none",
        "auth_env_var": "",
        "timeout_seconds": 20,
        "max_results": 8,
        "default_language": "auto",
        "default_categories_text": "general",
        "safe_search": 0,
    }


def _default_worker_guidance_form_values() -> dict[str, Any]:
    return {
        "worker_name": "",
        "display_name": "",
        "enabled": False,
        "role_description": "",
        "operator_recommendations_text": "",
        "decision_preferences_text": "",
        "competence_boundary": "",
        "escalate_out_of_scope": True,
        "auto_submit_suggestions": True,
    }


async def _api_request(method: str, path: str, *, json_payload: dict | None = None) -> httpx.Response:
    url = f"{settings.orchestrator_internal_url.rstrip('/')}{path}"
    try:
        async with httpx.AsyncClient(timeout=_orchestrator_timeout()) as client:
            return await client.request(method, url, json=json_payload)
    except httpx.TimeoutException as exc:
        logger.warning("Web UI request to orchestrator timed out for %s %s: %s", method, path, exc)
        return _error_response(
            method,
            url,
            (
                "Der Orchestrator hat nicht rechtzeitig geantwortet. "
                "Das Dashboard zeigt deshalb eine degradierte Ansicht. "
                "Prüfe `docker compose logs -f fmac-orch`."
            ),
            status_code=504,
        )
    except httpx.HTTPError as exc:
        logger.warning("Web UI request to orchestrator failed for %s %s: %s", method, path, exc)
        return _error_response(
            method,
            url,
            (
                "Der Orchestrator ist derzeit nicht erreichbar. "
                "Das Dashboard bleibt nutzbar, zeigt aber nur reduzierte Daten. "
                "Prüfe `docker compose ps` und `docker compose logs -f fmac-orch`."
            ),
        )


def _build_snapshot(
    response: httpx.Response,
    *,
    path: str,
    default_value: Any,
    fallback_message: str,
) -> dict[str, Any]:
    """Wrap backend responses in a stable debug envelope so failed calls are still shareable."""

    payload = _response_json(response, default_value)
    snapshot: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "requested_path": path,
        "ok": response.status_code < 400,
        "backend_status_code": response.status_code,
        "detail": None,
        "payload": payload,
    }
    if response.status_code >= 400:
        snapshot["detail"] = _response_detail(response, fallback_message)
    response_text = response.text.strip()
    if response_text and not isinstance(payload, (dict, list)):
        snapshot["response_text_preview"] = response_text[:2000]
    elif response.status_code >= 400 and response_text:
        snapshot["response_text_preview"] = response_text[:2000]
    return snapshot


async def _api_snapshot(path: str, default_value: Any, fallback_message: str) -> dict[str, Any]:
    """Fetch one orchestrator endpoint and convert it into a downloadable debug snapshot."""

    response = await _api_request("GET", path)
    return _build_snapshot(
        response,
        path=path,
        default_value=default_value,
        fallback_message=fallback_message,
    )


def _task_reports_dir(task_id: str) -> Path:
    """Return the report directory for one task inside the mounted reports volume."""

    return settings.task_report_dir(task_id)


def _task_report_entries(task_id: str) -> list[dict[str, Any]]:
    """Collect downloadable report files for one task in a UI-friendly shape."""

    report_dir = _task_reports_dir(task_id)
    if not report_dir.exists():
        return []

    entries: list[dict[str, Any]] = []
    for report_path in sorted((path for path in report_dir.rglob("*") if path.is_file()), key=lambda item: item.as_posix()):
        relative_path = report_path.relative_to(report_dir).as_posix()
        size = report_path.stat().st_size
        entries.append(
            {
                "relative_path": relative_path,
                "name": report_path.name,
                "size_bytes": size,
                "size_display": _format_bytes(size),
                "href": f"/debug/tasks/{task_id}/reports/{relative_path}",
            }
        )
    return entries


def _resolve_task_report_path(task_id: str, report_path: str) -> Path:
    """Resolve a report download path safely so debug exports cannot escape the report folder."""

    report_dir = _task_reports_dir(task_id).resolve()
    candidate = (report_dir / report_path).resolve()
    if not candidate.is_relative_to(report_dir) or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Die angeforderte Report-Datei wurde nicht gefunden.")
    return candidate


def _data_store_entries() -> list[dict[str, Any]]:
    """List persisted runtime files that can be downloaded directly from the debug center."""

    entries: list[dict[str, Any]] = []
    for file_name, path in _known_data_store_paths().items():
        diagnostics = _path_diagnostics(path)
        entries.append(
            {
                "file_name": file_name,
                "label": file_name,
                "description": DATA_STORE_FILE_DESCRIPTIONS.get(file_name, "Persistierte Runtime-Datei."),
                "exists": diagnostics.get("exists", False),
                "size_display": diagnostics.get("size_display", "unbekannt"),
                "href": f"/debug/system/files/{file_name}",
            }
        )
    return entries


async def _system_snapshot_payload(artifact_key: str) -> tuple[str, Any]:
    """Resolve one named system artifact to filename plus payload."""

    if artifact_key == "orchestrator-health":
        return "orchestrator-health.json", await _api_snapshot(
            "/health",
            {"service": "orchestrator", "status": "unknown"},
            "Der Orchestrator-Healthcheck konnte nicht geladen werden.",
        )
    if artifact_key == "web-ui-health":
        return "web-ui-health.json", {
            "generated_at": datetime.now(UTC).isoformat(),
            "requested_path": "/health",
            "ok": True,
            "backend_status_code": 200,
            "detail": None,
            "payload": HealthResponse(service="web-ui").model_dump(mode="json"),
        }
    if artifact_key == "tasks":
        return "tasks.json", await _api_snapshot(
            "/api/tasks",
            [],
            "Die Aufgabenliste konnte nicht geladen werden.",
        )
    if artifact_key == "repository-access":
        return "repository-access.json", await _api_snapshot(
            "/api/settings/repository-access",
            {"allowed_repositories": []},
            "Die Repository-Allowlist konnte nicht geladen werden.",
        )
    if artifact_key == "trusted-sources":
        return "trusted-sources.json", await _api_snapshot(
            "/api/settings/trusted-sources",
            {"profiles": [], "active_profile_id": None},
            "Die Trusted Sources konnten nicht geladen werden.",
        )
    if artifact_key == "web-search":
        return "web-search.json", await _api_snapshot(
            "/api/settings/web-search",
            {"providers": []},
            "Die Web-Search-Einstellungen konnten nicht geladen werden.",
        )
    if artifact_key == "worker-guidance":
        return "worker-guidance.json", await _api_snapshot(
            "/api/settings/worker-guidance",
            {"workers": []},
            "Die Worker-Guidance konnte nicht geladen werden.",
        )
    if artifact_key == "suggestions-registry":
        return "suggestions-registry.json", await _api_snapshot(
            "/api/suggestions/registry",
            {"suggestions": []},
            "Die Suggestions-Registry konnte nicht geladen werden.",
        )
    if artifact_key == "runtime-summary":
        return "runtime-summary.json", _runtime_summary_snapshot()
    if artifact_key == "reports-manifest":
        return "reports-manifest.json", _reports_root_manifest()
    if artifact_key == "host-log-commands":
        return "host-log-commands.txt", HOST_LOG_COMMANDS
    raise HTTPException(status_code=404, detail=f"Unbekanntes System-Debug-Artefakt `{artifact_key}`.")


async def _task_snapshot_payload(task_id: str, artifact_key: str) -> tuple[str, Any]:
    """Resolve one task-scoped debug artifact to filename plus payload."""

    if artifact_key == "detail":
        return "task-detail.json", await _api_snapshot(
            f"/api/tasks/{task_id}",
            {},
            f"Die Aufgabe `{task_id}` konnte nicht geladen werden.",
        )

    detail_snapshot = await _api_snapshot(
        f"/api/tasks/{task_id}",
        {},
        f"Die Aufgabe `{task_id}` konnte nicht geladen werden.",
    )
    detail_payload = _as_mapping(detail_snapshot.get("payload"))

    if artifact_key == "ui-state":
        snapshot = dict(detail_snapshot)
        snapshot["payload"] = {
            "task_id": task_id,
            "ui_state": _decorate_task(detail_payload) if detail_snapshot["ok"] and detail_payload else None,
        }
        return "task-ui-state.json", snapshot

    if artifact_key == "events":
        snapshot = dict(detail_snapshot)
        snapshot["payload"] = {
            "task_id": task_id,
            "events": _normalize_events(detail_payload.get("events")),
        }
        return "task-events.json", snapshot

    if artifact_key == "worker-results":
        snapshot = dict(detail_snapshot)
        snapshot["payload"] = {
            "task_id": task_id,
            "worker_results": _normalize_worker_results(detail_payload.get("worker_results")),
        }
        return "task-worker-results.json", snapshot

    if artifact_key == "suggestions":
        suggestions_snapshot = await _api_snapshot(
            "/api/suggestions/registry",
            {"suggestions": []},
            "Die Suggestions-Registry konnte nicht geladen werden.",
        )
        registry = _as_mapping(suggestions_snapshot.get("payload"))
        suggestions = _normalize_suggestions(registry.get("suggestions"))
        filtered = [item for item in suggestions if item.get("task_id") == task_id]
        suggestions_snapshot["payload"] = {
            "task_id": task_id,
            "count": len(filtered),
            "suggestions": filtered,
        }
        return "task-suggestions.json", suggestions_snapshot

    if artifact_key == "reports-manifest":
        return "task-reports-manifest.json", {
            "generated_at": datetime.now(UTC).isoformat(),
            "task_id": task_id,
            "reports_dir": str(_task_reports_dir(task_id)),
            "files": _task_report_entries(task_id),
        }

    raise HTTPException(status_code=404, detail=f"Unbekanntes Task-Debug-Artefakt `{artifact_key}`.")


async def _system_bundle_files(prefix: str = "system") -> dict[str, bytes]:
    """Collect all downloadable system snapshots plus persisted config files for one ZIP bundle."""

    bundle: dict[str, bytes] = {}
    for artifact in SYSTEM_SNAPSHOT_ARTIFACTS:
        filename, payload = await _system_snapshot_payload(artifact["key"])
        archive_name = f"{prefix}/{filename}" if prefix else filename
        if filename.endswith(".txt"):
            bundle[archive_name] = _text_bytes(str(payload))
        else:
            bundle[archive_name] = _json_bytes(payload)

    for file_name, path in _known_data_store_paths().items():
        diagnostics = _path_diagnostics(path)
        if not diagnostics.get("exists") or not diagnostics.get("is_file"):
            continue
        try:
            bundle[f"{prefix}/persisted/{file_name}"] = path.read_bytes()
        except OSError as exc:
            bundle[f"{prefix}/persisted/{file_name}.error.json"] = _json_bytes(
                {
                    "generated_at": datetime.now(UTC).isoformat(),
                    "file_name": file_name,
                    "path": str(path),
                    "detail": f"{exc.__class__.__name__}: {exc}",
                }
            )

    return bundle


async def _task_bundle_files(task_id: str, prefix: str | None = None) -> dict[str, bytes]:
    """Collect all task-scoped snapshots and report files for a shareable debug archive."""

    task_prefix = prefix or f"tasks/{task_id}"
    bundle: dict[str, bytes] = {}
    for artifact in TASK_SNAPSHOT_ARTIFACTS:
        filename, payload = await _task_snapshot_payload(task_id, artifact["key"])
        bundle[f"{task_prefix}/{filename}"] = _json_bytes(payload)

    for entry in _task_report_entries(task_id):
        report_path = _resolve_task_report_path(task_id, entry["relative_path"])
        try:
            bundle[f"{task_prefix}/reports/{entry['relative_path']}"] = report_path.read_bytes()
        except OSError as exc:
            bundle[f"{task_prefix}/reports/{entry['relative_path']}.error.json"] = _json_bytes(
                {
                    "generated_at": datetime.now(UTC).isoformat(),
                    "task_id": task_id,
                    "report_path": entry["relative_path"],
                    "detail": f"{exc.__class__.__name__}: {exc}",
                }
            )

    return bundle


async def _load_debug_center_context(
    *,
    task_id: str | None = None,
    error_message: str | None = None,
    success_message: str | None = None,
) -> dict[str, Any]:
    """Build the debug-center page without assuming the orchestrator is fully healthy."""

    tasks_snapshot = await _api_snapshot("/api/tasks", [], "Die Aufgabenliste konnte nicht geladen werden.")
    raw_tasks = _as_list(tasks_snapshot.get("payload"))
    tasks = [_decorate_task(task) for task in raw_tasks if isinstance(task, dict)]

    messages = [error_message] if error_message else []
    if not tasks_snapshot["ok"]:
        messages.append(str(tasks_snapshot.get("detail") or "Die Aufgabenliste konnte nicht geladen werden."))

    selected_task: dict[str, Any] | None = None
    selected_task_error: str | None = None
    if task_id:
        detail_snapshot = await _api_snapshot(
            f"/api/tasks/{task_id}",
            {},
            f"Die Aufgabe `{task_id}` konnte nicht geladen werden.",
        )
        detail_payload = _as_mapping(detail_snapshot.get("payload"))
        if detail_snapshot["ok"] and detail_payload:
            selected_task = _decorate_task(detail_payload)
        else:
            selected_task_error = str(detail_snapshot.get("detail") or f"Die Aufgabe `{task_id}` ist nicht lesbar.")
            messages.append(selected_task_error)

    system_downloads = [
        {
            **artifact,
            "href": f"/debug/system/{artifact['key']}",
        }
        for artifact in SYSTEM_SNAPSHOT_ARTIFACTS
    ]

    task_downloads = []
    if task_id:
        task_downloads = [
            {
                **artifact,
                "href": f"/debug/tasks/{task_id}/{artifact['key']}",
            }
            for artifact in TASK_SNAPSHOT_ARTIFACTS
        ]

    return {
        "tasks": tasks,
        "selected_task_id": task_id or "",
        "selected_task": selected_task,
        "selected_task_error": selected_task_error,
        "selected_task_reports": _task_report_entries(task_id) if task_id else [],
        "system_downloads": system_downloads,
        "task_downloads": task_downloads,
        "data_store_entries": _data_store_entries(),
        "system_bundle_href": "/debug/system/bundle.zip",
        "combined_bundle_href": f"/debug/bundle.zip?task_id={task_id}" if task_id else "/debug/system/bundle.zip",
        "task_bundle_href": f"/debug/tasks/{task_id}/bundle.zip" if task_id else None,
        "host_log_commands": HOST_LOG_COMMANDS,
        "auto_refresh_seconds": selected_task.get("auto_refresh_seconds", 0) if selected_task else 0,
        "error_message": " ".join(item for item in messages if item) or None,
        "success_message": success_message,
    }


async def _load_dashboard_context(
    error_message: str | None = None,
    success_message: str | None = None,
    *,
    show_archived: bool = False,
) -> dict[str, Any]:
    messages = [error_message] if error_message else []
    tasks_response = await _api_request("GET", "/api/tasks")
    archived_tasks_response = await _api_request("GET", "/api/tasks?only_archived=true")
    repo_settings_response = await _api_request("GET", "/api/settings/repository-access")
    suggestions_response = await _api_request("GET", "/api/suggestions")

    tasks = [
        _decorate_task(task)
        for task in _as_list(_response_json(tasks_response, []))
        if isinstance(task, dict) and not _is_task_archived(task)
    ]
    if tasks_response.status_code >= 400:
        messages.append(_response_detail(tasks_response, "Die Aufgabenliste konnte nicht geladen werden."))
    archived_tasks = [
        _decorate_task(task)
        for task in _as_list(_response_json(archived_tasks_response, []))
        if isinstance(task, dict) and _is_task_archived(task)
    ]
    if archived_tasks_response.status_code >= 400:
        messages.append(_response_detail(archived_tasks_response, "Die Archivliste konnte nicht geladen werden."))

    repo_settings = _as_mapping(_response_json(repo_settings_response, {"allowed_repositories": []}))
    if repo_settings_response.status_code >= 400:
        messages.append(
            _response_detail(repo_settings_response, "Die Repository-Allowlist konnte nicht geladen werden.")
        )

    pending_suggestions = _as_list(_response_json(suggestions_response, []))
    if suggestions_response.status_code >= 400:
        messages.append(_response_detail(suggestions_response, "Die Mitarbeiterideen konnten nicht geladen werden."))
    else:
        pending_suggestions = [item for item in pending_suggestions if item.get("status") == "pending"]

    return {
        "tasks": tasks,
        "archived_tasks": archived_tasks,
        "archived_task_count": len(archived_tasks),
        "show_archived": show_archived,
        "repository_access_settings": repo_settings,
        "allowed_repositories_text": "\n".join(repo_settings.get("allowed_repositories", [])),
        "pending_suggestions_count": len(pending_suggestions),
        "error_message": " ".join(item for item in messages if item) or None,
        "success_message": success_message,
    }


async def _load_archive_context(
    error_message: str | None = None,
    success_message: str | None = None,
    *,
    highlight_task_id: str | None = None,
) -> dict[str, Any]:
    """Load only archived tasks so the archive becomes a dedicated, predictable operator view."""

    messages = [error_message] if error_message else []
    archived_tasks_response = await _api_request("GET", "/api/tasks?only_archived=true")
    archived_tasks = [
        _decorate_task(task)
        for task in _as_list(_response_json(archived_tasks_response, []))
        if isinstance(task, dict) and _is_task_archived(task)
    ]
    if archived_tasks_response.status_code >= 400:
        messages.append(_response_detail(archived_tasks_response, "Die Archivliste konnte nicht geladen werden."))

    normalized_highlight = str(highlight_task_id or "").strip()
    for task in archived_tasks:
        task["highlighted"] = bool(normalized_highlight and task.get("id") == normalized_highlight)

    return {
        "archived_tasks": archived_tasks,
        "archived_task_count": len(archived_tasks),
        "highlight_task_id": normalized_highlight or None,
        "error_message": " ".join(item for item in messages if item) or None,
        "success_message": success_message,
    }


async def _load_benchmarks_context(
    error_message: str | None = None,
    success_message: str | None = None,
    selected_workers: list[str] | None = None,
    probe_goal: str | None = None,
    focus_paths_text: str | None = None,
) -> dict[str, Any]:
    """Collect a readable worker benchmark view from persisted task details without crashing on partial backend issues."""

    del selected_workers, probe_goal, focus_paths_text
    messages = [error_message] if error_message else []
    reset_at = _benchmark_reset_at()
    tasks_response = await _api_request("GET", "/api/tasks?include_archived=true")
    worker_probe_response = await _api_request("GET", "/api/benchmarks/model-probe")
    all_tasks = [task for task in _as_list(_response_json(tasks_response, [])) if isinstance(task, dict)]
    archived_hidden_count = sum(1 for task in all_tasks if _is_task_archived(task))
    raw_tasks = [task for task in all_tasks if not _is_task_archived(task)]
    if tasks_response.status_code >= 400:
        messages.append(_response_detail(tasks_response, "Die Aufgabenliste konnte nicht für Benchmarks geladen werden."))
    if worker_probe_response.status_code >= 400:
        messages.append(_response_detail(worker_probe_response, "Die Modell-Probeläufe konnten nicht geladen werden."))
    hidden_tasks_before_reset = 0
    if reset_at is not None and raw_tasks:
        hidden_tasks_before_reset = sum(1 for task in raw_tasks if not _task_matches_benchmark_window(task, reset_at))
        raw_tasks = [task for task in raw_tasks if _task_matches_benchmark_window(task, reset_at)]

    detailed_tasks: list[dict[str, Any]] = []
    skipped_tasks: list[dict[str, Any]] = []
    if raw_tasks:
        detail_responses = await asyncio.gather(
            *[_api_request("GET", f"/api/tasks/{task['id']}") for task in raw_tasks if task.get("id")],
            return_exceptions=True,
        )
        for task, response in zip((task for task in raw_tasks if task.get("id")), detail_responses, strict=False):
            task_id = str(task.get("id") or "unbekannt")
            if not isinstance(response, httpx.Response):
                skipped_tasks.append({"task_id": task_id, "detail": f"{response.__class__.__name__}: {response}"})
                continue
            if response.status_code >= 400:
                skipped_tasks.append(
                    {
                        "task_id": task_id,
                        "detail": _response_detail(response, f"Task `{task_id}` konnte nicht für Benchmarks geladen werden."),
                    }
                )
                continue
            detailed_tasks.append(_decorate_task(_as_mapping(_response_json(response, {}))))

    benchmark_report = _build_worker_benchmark_report(
        detailed_tasks,
        skipped_tasks,
        reset_at=reset_at,
        hidden_tasks_before_reset=hidden_tasks_before_reset,
    )
    worker_probe_report = _decorate_worker_probe_registry(_as_mapping(_response_json(worker_probe_response, {})))
    if skipped_tasks:
        messages.append(
            f"{len(skipped_tasks)} Aufgaben konnten nicht vollständig in die Benchmark-Auswertung aufgenommen werden."
        )
    benchmark_auto_refresh = 30 if benchmark_report["active_runs"] else 0
    probe_auto_refresh = 10 if worker_probe_report["overview"]["active_run_count"] else 0

    return {
        "benchmark_report": benchmark_report,
        "worker_probe_report": worker_probe_report,
        "worker_summaries": benchmark_report["worker_summaries"],
        "recent_runs": benchmark_report["recent_runs"],
        "overview": {
            "generated_at_display": _format_timestamp(benchmark_report["generated_at"]),
            "reset_at_display": _format_timestamp(benchmark_report["reset_at"]) if benchmark_report["reset_at"] else "noch nie",
            "benchmark_window_active": benchmark_report["benchmark_window_active"],
            "archived_hidden_count": archived_hidden_count,
            "hidden_tasks_before_reset": benchmark_report["hidden_tasks_before_reset"],
            "total_tasks": benchmark_report["total_tasks"],
            "skipped_task_count": benchmark_report["skipped_task_count"],
            "total_runs": benchmark_report["total_runs"],
            "active_runs": benchmark_report["active_runs"],
            "failed_runs": benchmark_report["failed_runs"],
            "completed_runs": benchmark_report["completed_runs"],
            "average_duration_display": benchmark_report["average_duration_display"],
            "median_duration_display": benchmark_report["median_duration_display"],
        },
        "worker_probe_overview": worker_probe_report["overview"],
        "auto_refresh_seconds": max(benchmark_auto_refresh, probe_auto_refresh),
        "error_message": " ".join(item for item in messages if item) or None,
        "success_message": success_message,
        "worker_probe_default_goal": DEFAULT_WORKER_PROBE_GOAL,
        "worker_probe_ok_goal": DEFAULT_OK_WORKER_PROBE_GOAL,
    }


async def _load_worker_tests_context(
    *,
    error_message: str | None = None,
    success_message: str | None = None,
    selected_workers: list[str] | None = None,
    probe_goal: str | None = None,
    focus_paths_text: str | None = None,
) -> dict[str, Any]:
    """Render a dedicated operator page for quick single-worker or small subset model probes."""

    messages = [error_message] if error_message else []
    worker_probe_response = await _api_request("GET", "/api/benchmarks/model-probe")
    if worker_probe_response.status_code >= 400:
        messages.append(_response_detail(worker_probe_response, "Die Teiltests konnten nicht geladen werden."))

    worker_probe_report = _decorate_worker_probe_registry(_as_mapping(_response_json(worker_probe_response, {})))
    probe_auto_refresh = 10 if worker_probe_report["overview"]["active_run_count"] else 0
    normalized_selected_workers = [
        worker_name for worker_name in (selected_workers or ["coding"]) if worker_name in PROBE_WORKERS
    ] or ["coding"]
    suggested_focus_paths = _recent_fix_focus_paths()
    normalized_focus_paths = _normalize_focus_paths_text(focus_paths_text) or suggested_focus_paths

    return {
        "worker_probe_report": worker_probe_report,
        "worker_probe_overview": worker_probe_report["overview"],
        "probe_worker_choices": _build_worker_test_choices(selected_workers=normalized_selected_workers),
        "worker_probe_default_goal": probe_goal or DEFAULT_TARGETED_WORKER_PROBE_GOAL,
        "worker_probe_ok_goal": DEFAULT_OK_WORKER_PROBE_GOAL,
        "worker_probe_micro_fix_goal": DEFAULT_MICRO_FIX_WORKER_PROBE_GOAL,
        "selected_workers": normalized_selected_workers,
        "focus_paths": normalized_focus_paths,
        "focus_paths_text": "\n".join(normalized_focus_paths),
        "auto_refresh_seconds": probe_auto_refresh,
        "error_message": " ".join(item for item in messages if item) or None,
        "success_message": success_message,
    }


async def _load_task_detail_context(
    task_id: str,
    *,
    view: str | None = None,
    error_message: str | None = None,
    success_message: str | None = None,
) -> dict[str, Any]:
    """Load and enrich one task detail plus related dashboard data for the detail page."""

    response = await _api_request("GET", f"/api/tasks/{task_id}")
    if response.status_code >= 400:
        raise RuntimeError(_response_detail(response, f"Die Aufgabe `{task_id}` konnte nicht geladen werden."))
    task = _decorate_task(_as_mapping(_response_json(response, {})))
    normalized_view = str(view or "").strip().lower()
    if normalized_view not in {"compact", "full"}:
        normalized_view = "compact" if task.get("is_active") else "full"
    task["layout_mode"] = normalized_view
    task["is_compact_view"] = normalized_view == "compact"
    task["is_full_view"] = normalized_view == "full"
    task["task_detail_href"] = f"/tasks/{task_id}"
    task["compact_view_href"] = f"/tasks/{task_id}?view=compact"
    task["full_view_href"] = f"/tasks/{task_id}?view=full"
    suggestion_context = await _load_suggestions_context(task_id=task_id)
    messages = [error_message, suggestion_context.get("error_message")]
    cleaned_suggestion_context = {
        key: value
        for key, value in suggestion_context.items()
        if key not in {"error_message", "success_message"}
    }
    return {
        "task": task,
        **cleaned_suggestion_context,
        "error_message": " ".join(item for item in messages if item) or None,
        "success_message": success_message or suggestion_context.get("success_message"),
    }


async def _load_trusted_sources_context(
    *,
    edit_source_id: str | None = None,
    error_message: str | None = None,
    success_message: str | None = None,
    dry_run_result: dict | None = None,
    source_test_result: dict | None = None,
    import_payload: str | None = None,
) -> dict[str, Any]:
    registry_response = await _api_request("GET", "/api/settings/trusted-sources")
    registry = _as_mapping(_response_json(registry_response, {"profiles": [], "active_profile_id": None}))
    profiles = [item for item in _as_list(registry.get("profiles")) if isinstance(item, dict)]
    active_profile_id = registry.get("active_profile_id")
    active_profile = next((profile for profile in profiles if profile["id"] == active_profile_id), None)
    sources = [item for item in _as_list(active_profile.get("sources")) if isinstance(item, dict)] if active_profile else []
    edit_source = next((source for source in sources if source["id"] == edit_source_id), None)

    form_values = _default_source_form_values()
    if edit_source:
        form_values.update(edit_source)
        form_values["allowed_paths_text"] = "\n".join(edit_source.get("allowed_paths", []))
        form_values["deny_paths_text"] = "\n".join(edit_source.get("deny_paths", []))
        form_values["tags_text"] = "\n".join(edit_source.get("tags", []))

    messages = [error_message] if error_message else []
    if registry_response.status_code >= 400:
        messages.append(_response_detail(registry_response, "Trusted Sources konnten nicht geladen werden."))

    return {
        "registry": registry,
        "profiles": profiles,
        "active_profile": active_profile,
        "sources": sources,
        "source_form_values": form_values,
        "error_message": " ".join(item for item in messages if item) or None,
        "success_message": success_message,
        "dry_run_result": dry_run_result,
        "source_test_result": source_test_result,
        "import_payload": import_payload or json.dumps(registry, indent=2, ensure_ascii=True),
    }


async def _load_web_search_context(
    *,
    edit_provider_id: str | None = None,
    error_message: str | None = None,
    success_message: str | None = None,
    provider_test_result: dict | None = None,
) -> dict[str, Any]:
    settings_response = await _api_request("GET", "/api/settings/web-search")
    provider_settings = _as_mapping(
        _response_json(
        settings_response,
        {
            "providers": [],
            "primary_web_search_provider": "",
            "fallback_web_search_provider": "",
            "require_trusted_sources_first": True,
            "allow_general_web_search_fallback": True,
            "provider_host_allowlist": [],
        },
    ))
    providers = [item for item in _as_list(provider_settings.get("providers")) if isinstance(item, dict)]
    edit_provider = next((provider for provider in providers if provider["id"] == edit_provider_id), None)

    form_values = _default_provider_form_values()
    if edit_provider:
        form_values.update(edit_provider)
        form_values["default_categories_text"] = "\n".join(edit_provider.get("default_categories", []))

    messages = [error_message] if error_message else []
    if settings_response.status_code >= 400:
        messages.append(_response_detail(settings_response, "Web-Search-Provider konnten nicht geladen werden."))

    return {
        "web_search_settings": provider_settings,
        "providers": providers,
        "provider_form_values": form_values,
        "error_message": " ".join(item for item in messages if item) or None,
        "success_message": success_message,
        "provider_test_result": provider_test_result,
    }


async def _load_worker_guidance_context(
    *,
    edit_worker_name: str | None = None,
    error_message: str | None = None,
    success_message: str | None = None,
) -> dict[str, Any]:
    registry_response = await _api_request("GET", "/api/settings/worker-guidance")
    registry = _as_mapping(_response_json(registry_response, {"workers": []}))
    workers = [item for item in _as_list(registry.get("workers")) if isinstance(item, dict)]
    if edit_worker_name is None and workers:
        edit_worker_name = workers[0]["worker_name"]
    edit_worker = next((worker for worker in workers if worker["worker_name"] == edit_worker_name), None)

    form_values = _default_worker_guidance_form_values()
    if edit_worker:
        form_values.update(edit_worker)
        form_values["role_description"] = str(
            edit_worker.get("role_description") or edit_worker.get("role_summary") or ""
        )
        form_values["escalate_out_of_scope"] = bool(
            edit_worker.get(
                "escalate_out_of_scope",
                edit_worker.get("escalate_beyond_boundary", True),
            )
        )
        form_values["auto_submit_suggestions"] = bool(
            edit_worker.get(
                "auto_submit_suggestions",
                edit_worker.get("auto_submit_improvement_suggestions", True),
            )
        )
        form_values["operator_recommendations_text"] = "\n".join(edit_worker.get("operator_recommendations", []))
        form_values["decision_preferences_text"] = "\n".join(edit_worker.get("decision_preferences", []))

    messages = [error_message] if error_message else []
    if registry_response.status_code >= 400:
        messages.append(_response_detail(registry_response, "Die Worker-Guidance konnte nicht geladen werden."))

    return {
        "worker_guidance_registry": registry,
        "workers": workers,
        "worker_guidance_form_values": form_values,
        "error_message": " ".join(item for item in messages if item) or None,
        "success_message": success_message,
    }


async def _load_suggestions_context(
    *,
    task_id: str | None = None,
    error_message: str | None = None,
    success_message: str | None = None,
) -> dict[str, Any]:
    suggestions_response = await _api_request("GET", "/api/suggestions/registry")
    registry = _as_mapping(_response_json(suggestions_response, {"suggestions": []}))
    suggestions = _normalize_suggestions(registry.get("suggestions", []))
    if task_id is not None:
        suggestions = [item for item in suggestions if item.get("task_id") == task_id]

    pending = [item for item in suggestions if item.get("status") == "pending"]
    approved = [item for item in suggestions if item.get("status") == "approved"]
    implemented = [item for item in suggestions if item.get("status") == "implemented"]
    dismissed = [item for item in suggestions if item.get("status") == "dismissed"]
    suppressed = [item for item in suggestions if item.get("status") == "suppressed_for_repository"]
    repository_resolved = [item for item in suggestions if item.get("is_repository_wide")]

    messages = [error_message] if error_message else []
    if suggestions_response.status_code >= 400:
        messages.append(_response_detail(suggestions_response, "Die Mitarbeiterideen konnten nicht geladen werden."))

    return {
        "suggestion_registry": registry,
        "suggestions": suggestions,
        "pending_suggestions": pending,
        "approved_suggestions": approved,
        "implemented_suggestions": implemented,
        "dismissed_suggestions": dismissed,
        "suppressed_suggestions": suppressed,
        "repository_resolved_suggestions": repository_resolved,
        "rejected_suggestions": dismissed,
        "error_message": " ".join(item for item in messages if item) or None,
        "success_message": success_message,
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, show_archived: bool = Query(default=False)) -> HTMLResponse:
    context = await _load_dashboard_context(show_archived=show_archived)
    return templates.TemplateResponse(request=request, name="index.html", context={"request": request, **context})


def _format_duration_ms(ms: float) -> str:
    """Render a millisecond duration in a human-readable format."""
    if ms < 1000:
        return f"{ms:.0f} ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f} s"
    return f"{ms / 60_000:.1f} min"


def _readiness_worker_groups(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group worker readiness rows so operators can instantly separate healthy, waiting, and broken workers."""

    groups = [
        ("aktiv", "Aktiv / gesund"),
        ("laufend", "Aktuell laufend"),
        ("wartend", "Wartend"),
        ("inaktiv", "Nicht benoetigt / inaktiv"),
        ("fehlerhaft", "Fehlerhaft"),
    ]
    grouped: dict[str, list[dict[str, Any]]] = {key: [] for key, _ in groups}

    for check in checks:
        if check.get("category") != "workers":
            continue
        raw_value = _as_mapping(check.get("raw_value"))
        state_group = str(raw_value.get("state_group") or "")
        state = str(raw_value.get("state") or "")
        group_key = "aktiv"
        if check.get("status") == "fail" or state in {"failed", "blocked"}:
            group_key = "fehlerhaft"
        elif state_group == "aktuell laufend" or state == "running":
            group_key = "laufend"
        elif state_group == "wartend" or state in {"waiting", "queued"}:
            group_key = "wartend"
        elif state_group == "nicht benoetigt / inaktiv" or state in {"idle", "skipped"}:
            group_key = "inaktiv"
        grouped.setdefault(group_key, []).append(check)

    return [
        {"id": group_id, "label": label, "workers": grouped.get(group_id, [])}
        for group_id, label in groups
    ]


def _decorate_readiness_report(raw: dict[str, Any]) -> dict[str, Any]:
    """Add display and grouping fields to the raw readiness report for the dashboard and partial refreshes."""

    report = dict(raw)
    report["duration_display"] = _format_duration_ms(float(raw.get("duration_ms") or 0))
    report["started_at_display"] = _format_timestamp(_parse_timestamp(raw.get("started_at")))
    report["finished_at_display"] = _format_timestamp(_parse_timestamp(raw.get("finished_at")))
    report["overall_status_label"] = READINESS_STATUS_LABELS.get(str(report.get("overall_status")), str(report.get("overall_status", "")))
    report["overall_status_class"] = READINESS_STATUS_CLASSES.get(str(report.get("overall_status")), "status-idle")
    report["mode_label"] = "Tiefencheck" if report.get("mode") == ReadinessMode.DEEP.value else "Schnellcheck"

    checks = [_as_mapping(item) for item in _as_list(report.get("checks"))]
    for check in checks:
        check["duration_display"] = _format_duration_ms(float(check.get("duration_ms") or 0))
        check["started_at_display"] = _format_timestamp(_parse_timestamp(check.get("started_at")))
        check["finished_at_display"] = _format_timestamp(_parse_timestamp(check.get("finished_at")))
        check["status_label"] = READINESS_STATUS_LABELS.get(str(check.get("status")), str(check.get("status", "")))
        check["status_class"] = READINESS_STATUS_CLASSES.get(str(check.get("status")), "status-idle")
        check["severity_label"] = READINESS_SEVERITY_LABELS.get(str(check.get("severity")), str(check.get("severity", "")))
        raw_value = _as_mapping(check.get("raw_value"))
        check["current_action"] = str(raw_value.get("current_action") or raw_value.get("current_instruction") or "")
        check["waiting_for"] = str(raw_value.get("waiting_for") or "")
        check["last_error"] = str(raw_value.get("last_error") or "")
        check["task_id"] = str(raw_value.get("task_id") or "")
        check["goal"] = str(raw_value.get("goal") or "")
        check["updated_at_display"] = _format_timestamp(_parse_timestamp(raw_value.get("updated_at")))
        elapsed_seconds = raw_value.get("elapsed_seconds")
        check["elapsed_display"] = (
            _format_duration(float(elapsed_seconds))
            if isinstance(elapsed_seconds, (int, float))
            else "—"
        )
    report["checks"] = checks

    categories = [_as_mapping(item) for item in _as_list(report.get("categories"))]
    checks_by_category: dict[str, list[dict[str, Any]]] = {}
    for category in categories:
        category_id = str(category.get("id") or "")
        category["status_label"] = READINESS_STATUS_LABELS.get(str(category.get("status")), str(category.get("status", "")))
        category["status_class"] = READINESS_STATUS_CLASSES.get(str(category.get("status")), "status-idle")
        category_checks = [check for check in checks if check.get("category") == category_id]
        category["checks"] = category_checks
        checks_by_category[category_id] = category_checks
    report["categories"] = categories
    report["checks_by_category"] = checks_by_category

    recommendations = [_as_mapping(item) for item in _as_list(report.get("recommendations"))]
    for recommendation in recommendations:
        recommendation["priority_label"] = {
            100: "Sofort",
            90: "Sehr hoch",
            80: "Hoch",
            70: "Wichtig",
        }.get(int(recommendation.get("priority") or 0), "Hinweis")
    report["recommendations"] = recommendations

    environment_overview = _as_mapping(report.get("environment_overview"))
    report["environment_overview"] = environment_overview
    report["environment_rows"] = [
        {"label": "Pruefmodus", "value": report["mode_label"]},
        {"label": "Default-Modell", "value": str(environment_overview.get("default_model_provider") or "unbekannt")},
        {"label": "Orchestrator intern", "value": str(environment_overview.get("orchestrator_internal_url") or "unbekannt")},
        {"label": "Web-UI intern", "value": str(environment_overview.get("web_ui_internal_url") or "unbekannt")},
        {"label": "Workspace", "value": str(environment_overview.get("workspace_root") or "unbekannt")},
        {"label": "Task-Workspaces", "value": str(environment_overview.get("task_workspace_root") or "unbekannt")},
        {"label": "Repo-Pfad", "value": str(environment_overview.get("primary_repo_path") or "unbekannt")},
        {"label": "RUNTIME_HOME_DIR", "value": str(environment_overview.get("runtime_home_dir") or "unbekannt")},
    ]
    report["worker_groups"] = _readiness_worker_groups(checks)

    return report


async def _load_readiness_context(
    *,
    mode: ReadinessMode = ReadinessMode.QUICK,
) -> dict[str, Any]:
    """Load the structured readiness report and always return renderable fallback data."""

    url = f"{settings.orchestrator_internal_url.rstrip('/')}/api/system/readiness"
    timeout = httpx.Timeout(
        connect=min(settings.readiness_http_fast_timeout_seconds, 10.0),
        read=(
            settings.readiness_llm_smoke_timeout_seconds + 30.0
            if mode is ReadinessMode.DEEP
            else settings.readiness_http_deep_timeout_seconds + 15.0
        ),
        write=15.0,
        pool=10.0,
    )
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url, params={"mode": mode.value})
        if response.status_code < 400:
            raw = _response_json(response, {})
            if isinstance(raw, dict):
                report = _decorate_readiness_report(raw)
            else:
                raise ValueError("Bereitschaftsbericht war kein JSON-Objekt.")
        else:
            report = _decorate_readiness_report(
                build_catastrophic_readiness_report(
                    settings,
                    mode=mode,
                    exc=RuntimeError(
                        _response_detail(
                            response,
                            f"Bereitschaftsbericht meldete HTTP {response.status_code}.",
                        )
                    ),
                ).model_dump(mode="json")
            )
    except Exception as exc:
        report = _decorate_readiness_report(
            build_catastrophic_readiness_report(
                settings,
                mode=mode,
                exc=RuntimeError(f"Readiness-Report konnte nicht geladen werden: {exc}"),
            ).model_dump(mode="json")
        )

    return {
        "report": report,
        "report_json": _safe_json(report),
        "mode": mode.value,
        "fast_timeout_display": _format_duration(settings.readiness_http_fast_timeout_seconds),
        "deep_timeout_display": _format_duration(settings.readiness_http_deep_timeout_seconds),
        "llm_smoke_timeout_display": _format_duration(settings.readiness_llm_smoke_timeout_seconds),
        "slow_warning_display": _format_duration(settings.readiness_slow_warning_seconds),
    }


@app.get("/system-check", response_class=HTMLResponse)
async def system_check_page(request: Request, mode: ReadinessMode = READINESS_MODE_QUERY) -> HTMLResponse:
    context = await _load_readiness_context(mode=mode)
    return templates.TemplateResponse(
        request=request,
        name="readiness.html",
        context={"request": request, **context},
    )


@app.get("/system-check/panel", response_class=HTMLResponse)
async def system_check_panel(mode: ReadinessMode = READINESS_MODE_QUERY) -> HTMLResponse:
    context = await _load_readiness_context(mode=mode)
    return HTMLResponse(templates.get_template("readiness_panel.html").render(context))


@app.get("/system-check/report.json")
async def system_check_report_json(mode: ReadinessMode = READINESS_MODE_QUERY) -> Response:
    context = await _load_readiness_context(mode=mode)
    return _download_json_response(f"system-check-{mode.value}.json", context["report"])


@app.post("/system-check", response_class=HTMLResponse)
async def run_system_check(mode: ReadinessMode = READINESS_MODE_FORM) -> Response:
    return RedirectResponse(url=f"/system-check?mode={mode.value}", status_code=303)


@app.get("/benchmarks", response_class=HTMLResponse)
async def benchmarks_page(request: Request) -> HTMLResponse:
    context = await _load_benchmarks_context()
    return templates.TemplateResponse(
        request=request,
        name="benchmarks.html",
        context={"request": request, **context},
    )


@app.get("/worker-tests", response_class=HTMLResponse)
async def worker_tests_page(request: Request) -> HTMLResponse:
    context = await _load_worker_tests_context()
    return templates.TemplateResponse(
        request=request,
        name="worker_tests.html",
        context={"request": request, **context},
    )


@app.get("/benchmarks/export.json")
async def download_benchmarks_export() -> Response:
    context = await _load_benchmarks_context()
    return _download_json_response("worker-benchmarks.json", context["benchmark_report"])


@app.get("/benchmarks/model-probe/export.json")
async def download_worker_probe_export() -> Response:
    response = await _api_request("GET", "/api/benchmarks/model-probe")
    payload = _as_mapping(_response_json(response, {}))
    latest_run = next((item for item in _as_list(payload.get("runs")) if isinstance(item, dict)), {})
    return _download_json_response("worker-model-probe.json", latest_run)


@app.post("/benchmarks/reset", response_class=HTMLResponse)
async def reset_benchmarks(request: Request) -> HTMLResponse:
    reset_at = datetime.now(UTC).replace(microsecond=0)
    _save_benchmark_state(
        {
            "reset_at": reset_at.isoformat(),
            "reset_reason": "operator_reset",
        }
    )
    context = await _load_benchmarks_context(
        success_message=(
            "Die Benchmark-Auswertung wurde zurückgesetzt. Ab jetzt werden nur noch neue oder seit dem Reset "
            "aktualisierte Läufe berücksichtigt."
        )
    )
    return templates.TemplateResponse(
        request=request,
        name="benchmarks.html",
        context={"request": request, **context},
    )


@app.post("/benchmarks/model-probe/start", response_class=HTMLResponse, response_model=None)
async def start_worker_model_probe(
    request: Request,
    probe_goal: str = Form(DEFAULT_WORKER_PROBE_GOAL),
    probe_mode: WorkerProbeMode = WORKER_PROBE_MODE_FORM,
) -> Response:
    return await _submit_worker_probe_form(
        request=request,
        fallback_goal=probe_goal,
        probe_mode=probe_mode,
        redirect_url="/benchmarks",
        on_error_loader=_load_benchmarks_context,
        template_name="benchmarks.html",
        default_selected_workers=None,
    )


@app.post("/worker-tests/start", response_class=HTMLResponse, response_model=None)
async def start_targeted_worker_probe(
    request: Request,
    probe_goal: str = Form(DEFAULT_TARGETED_WORKER_PROBE_GOAL),
    probe_mode: WorkerProbeMode = WORKER_PROBE_MODE_FORM,
) -> Response:
    return await _submit_worker_probe_form(
        request=request,
        fallback_goal=probe_goal,
        probe_mode=probe_mode,
        redirect_url="/worker-tests",
        on_error_loader=_load_worker_tests_context,
        template_name="worker_tests.html",
        default_selected_workers=["coding"],
    )


async def _submit_worker_probe_form(
    *,
    request: Request,
    fallback_goal: str,
    probe_mode: WorkerProbeMode,
    redirect_url: str,
    on_error_loader,
    template_name: str,
    default_selected_workers: list[str] | None,
) -> Response:
    """Parse one worker-probe form once so benchmark and targeted-test pages stay behavior-identical."""

    form = await request.form()
    selected_workers = [
        str(value).strip()
        for value in form.getlist("selected_workers")
        if str(value).strip()
    ]
    focus_paths = _normalize_focus_paths_text(str(form.get("focus_paths_text") or ""))
    if default_selected_workers and not selected_workers:
        selected_workers = list(default_selected_workers)

    normalized_goal = str(form.get("probe_goal") or fallback_goal or "").strip() or {
        WorkerProbeMode.OK_CONTRACT: DEFAULT_OK_WORKER_PROBE_GOAL,
        WorkerProbeMode.MICRO_FIX: DEFAULT_MICRO_FIX_WORKER_PROBE_GOAL,
    }.get(probe_mode, DEFAULT_WORKER_PROBE_GOAL)
    payload: dict[str, Any] = {"probe_goal": normalized_goal, "probe_mode": probe_mode.value}
    if selected_workers:
        payload["selected_workers"] = selected_workers
    if focus_paths:
        payload["focus_paths"] = focus_paths

    response = await _api_request("POST", "/api/benchmarks/model-probe", json_payload=payload)
    if response.status_code >= 400:
        detail = _response_detail(response, "Die Modell-Probe konnte nicht gestartet werden.")
        context = await on_error_loader(
            error_message=detail,
            selected_workers=selected_workers or default_selected_workers,
            probe_goal=normalized_goal,
            focus_paths_text="\n".join(focus_paths),
        )
        return templates.TemplateResponse(
            request=request,
            name=template_name,
            context={"request": request, **context},
        )
    return RedirectResponse(url=redirect_url, status_code=303)


@app.get("/trusted-sources", response_class=HTMLResponse)
async def trusted_sources_page(request: Request, edit: str | None = Query(default=None)) -> HTMLResponse:
    context = await _load_trusted_sources_context(edit_source_id=edit)
    return templates.TemplateResponse(
        request=request,
        name="trusted_sources.html",
        context={"request": request, **context},
    )


@app.get("/web-search", response_class=HTMLResponse)
async def web_search_page(request: Request, edit: str | None = Query(default=None)) -> HTMLResponse:
    context = await _load_web_search_context(edit_provider_id=edit)
    return templates.TemplateResponse(
        request=request,
        name="web_search.html",
        context={"request": request, **context},
    )


@app.get("/worker-guidance", response_class=HTMLResponse)
async def worker_guidance_page(request: Request, edit: str | None = Query(default=None)) -> HTMLResponse:
    context = await _load_worker_guidance_context(edit_worker_name=edit)
    return templates.TemplateResponse(
        request=request,
        name="worker_guidance.html",
        context={"request": request, **context},
    )


@app.get("/suggestions", response_class=HTMLResponse)
async def suggestions_page(request: Request, task_id: str | None = Query(default=None)) -> HTMLResponse:
    context = await _load_suggestions_context(task_id=task_id)
    return templates.TemplateResponse(
        request=request,
        name="suggestions.html",
        context={"request": request, **context},
    )


@app.get("/debug", response_class=HTMLResponse)
async def debug_center_page(request: Request, task_id: str | None = Query(default=None)) -> HTMLResponse:
    context = await _load_debug_center_context(task_id=task_id)
    return templates.TemplateResponse(
        request=request,
        name="debug.html",
        context={"request": request, **context},
    )


@app.get("/debug/system/files/{file_name:path}")
async def download_system_data_store_file(file_name: str) -> Response:
    path = _known_data_store_paths().get(file_name)
    if path is None:
        raise HTTPException(status_code=404, detail=f"Die Runtime-Datei `{file_name}` ist nicht registriert.")
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail=f"Die Runtime-Datei `{file_name}` existiert derzeit nicht.")
    return FileResponse(path, filename=file_name)


@app.get("/debug/system/bundle.zip")
async def download_system_debug_bundle() -> Response:
    bundle = await _system_bundle_files()
    return _zip_response("feberdin-system-debug.zip", bundle)


@app.get("/debug/system/{artifact_key}")
async def download_system_debug_artifact(artifact_key: str) -> Response:
    filename, payload = await _system_snapshot_payload(artifact_key)
    if filename.endswith(".txt"):
        return _download_text_response(filename, str(payload))
    return _download_json_response(filename, payload)


@app.get("/debug/tasks/{task_id}/reports/{report_path:path}")
async def download_task_report(task_id: str, report_path: str) -> Response:
    resolved = _resolve_task_report_path(task_id, report_path)
    download_name = f"{task_id}-{Path(report_path).name}"
    return FileResponse(resolved, filename=download_name)


@app.get("/debug/tasks/{task_id}/bundle.zip")
async def download_task_debug_bundle(task_id: str) -> Response:
    bundle = await _task_bundle_files(task_id)
    return _zip_response(f"{task_id}-debug.zip", bundle)


@app.get("/debug/tasks/{task_id}/{artifact_key}")
async def download_task_debug_artifact(task_id: str, artifact_key: str) -> Response:
    filename, payload = await _task_snapshot_payload(task_id, artifact_key)
    return _download_json_response(f"{task_id}-{filename}", payload)


@app.get("/debug/bundle.zip")
async def download_combined_debug_bundle(task_id: str | None = Query(default=None)) -> Response:
    bundle = await _system_bundle_files(prefix="system")
    if task_id:
        bundle.update(await _task_bundle_files(task_id, prefix=f"tasks/{task_id}"))
        filename = f"feberdin-debug-{task_id}.zip"
    else:
        filename = "feberdin-debug.zip"
    return _zip_response(filename, bundle)


@app.get("/tasks/{task_id}", response_class=HTMLResponse)
async def task_detail(request: Request, task_id: str, view: str | None = Query(default=None)) -> HTMLResponse:
    try:
        context = await _load_task_detail_context(task_id, view=view)
    except RuntimeError as exc:
        context = await _load_dashboard_context(
            error_message=str(exc),
        )
        return templates.TemplateResponse(request=request, name="index.html", context={"request": request, **context})
    except Exception as exc:  # pragma: no cover - defensive fallback for unexpected runtime payloads.
        logger.exception("Task detail page for %s could not be rendered cleanly: %s", task_id, exc)
        detail = f"{exc.__class__.__name__}: {exc}" if str(exc) else exc.__class__.__name__
        context = await _load_dashboard_context(
            error_message=(
                f"Die Detailansicht fuer Aufgabe `{task_id}` konnte nicht vollstaendig aufgebaut werden. "
                "Das Dashboard bleibt verfuegbar. "
                f"Ursache: {detail}. "
                "Pruefe `docker logs --tail=200 fmac-web` oder `docker compose logs --tail=200 web-ui` fuer die genaue Ursache."
            ),
        )
        return templates.TemplateResponse(request=request, name="index.html", context={"request": request, **context})
    return templates.TemplateResponse(
        request=request,
        name="task.html",
        context={"request": request, **context},
    )


# Why this exists:
# FastAPI tries to derive a response model from the return annotation. A union like
# `HTMLResponse | RedirectResponse` is not a valid Pydantic response model and crashes at import time.
# We return concrete response objects directly, so the route must opt out of response-model generation.
@app.post("/tasks", response_class=HTMLResponse, response_model=None)
async def create_task(
    request: Request,
    goal: str = Form(...),
    repository: str = Form(...),
    local_repo_path: str = Form(...),
    enable_web_research: bool = Form(False),
    allow_repository_modifications: bool = Form(False),
) -> Response:
    payload = {
        "goal": goal,
        "repository": repository,
        "local_repo_path": local_repo_path,
        "enable_web_research": enable_web_research,
        "allow_repository_modifications": allow_repository_modifications,
    }
    response = await _api_request("POST", "/api/tasks", json_payload=payload)
    if response.status_code >= 400:
        detail = _response_detail(response, "Die Aufgabe konnte nicht angelegt werden.")
        context = await _load_dashboard_context(error_message=detail)
        return templates.TemplateResponse(request=request, name="index.html", context={"request": request, **context})
    task = _response_json(response, {})
    return RedirectResponse(url=f"/tasks/{task['id']}", status_code=303)


@app.post("/settings/repositories", response_class=HTMLResponse, response_model=None)
async def update_repository_settings(
    request: Request,
    allowed_repositories_text: str = Form(""),
) -> Response:
    repositories = [line.strip() for line in allowed_repositories_text.splitlines() if line.strip()]
    response = await _api_request(
        "PUT",
        "/api/settings/repository-access",
        json_payload={"allowed_repositories": repositories},
    )
    if response.status_code >= 400:
        detail = _response_detail(response, "Die Einstellungen konnten nicht gespeichert werden.")
        context = await _load_dashboard_context(error_message=detail)
        return templates.TemplateResponse(request=request, name="index.html", context={"request": request, **context})
    return RedirectResponse(url="/", status_code=303)


@app.post("/settings/worker-guidance", response_class=HTMLResponse, response_model=None)
async def update_worker_guidance(
    request: Request,
    worker_name: str = Form(...),
    display_name: str = Form(...),
    enabled: bool = Form(False),
    role_description: str = Form(...),
    operator_recommendations_text: str = Form(""),
    decision_preferences_text: str = Form(""),
    competence_boundary: str = Form(...),
    escalate_out_of_scope: bool = Form(False),
    auto_submit_suggestions: bool = Form(False),
) -> Response:
    payload = {
        "worker_name": worker_name,
        "display_name": display_name,
        "enabled": enabled,
        "role_description": role_description,
        "operator_recommendations": _split_lines(operator_recommendations_text),
        "decision_preferences": _split_lines(decision_preferences_text),
        "competence_boundary": competence_boundary,
        "escalate_out_of_scope": escalate_out_of_scope,
        "auto_submit_suggestions": auto_submit_suggestions,
    }
    response = await _api_request("PUT", f"/api/settings/worker-guidance/{worker_name}", json_payload=payload)
    if response.status_code >= 400:
        detail = _response_detail(response, "Die Worker-Guidance konnte nicht gespeichert werden.")
        context = await _load_worker_guidance_context(edit_worker_name=worker_name, error_message=detail)
        context["worker_guidance_form_values"].update(payload)
        context["worker_guidance_form_values"]["operator_recommendations_text"] = operator_recommendations_text
        context["worker_guidance_form_values"]["decision_preferences_text"] = decision_preferences_text
        return templates.TemplateResponse(
            request=request,
            name="worker_guidance.html",
            context={"request": request, **context},
        )
    return RedirectResponse(url="/worker-guidance", status_code=303)


@app.post("/settings/worker-guidance/{worker_name}/reset", response_class=HTMLResponse, response_model=None)
async def reset_worker_guidance(request: Request, worker_name: str) -> Response:
    response = await _api_request("POST", f"/api/settings/worker-guidance/{worker_name}/reset")
    if response.status_code >= 400:
        detail = _response_detail(response, "Die Worker-Guidance konnte nicht auf Standard zurückgesetzt werden.")
        context = await _load_worker_guidance_context(edit_worker_name=worker_name, error_message=detail)
        return templates.TemplateResponse(request=request, name="worker_guidance.html", context={"request": request, **context})
    return RedirectResponse(url=f"/worker-guidance?edit={worker_name}", status_code=303)


@app.post("/settings/trusted-sources/profile", response_class=HTMLResponse, response_model=None)
async def update_trusted_source_profile(
    request: Request,
    profile_id: str = Form(...),
) -> Response:
    response = await _api_request(
        "POST",
        "/api/settings/trusted-sources/active-profile",
        json_payload={"profile_id": profile_id},
    )
    if response.status_code >= 400:
        detail = _response_detail(response, "Das Profil konnte nicht gewechselt werden.")
        context = await _load_trusted_sources_context(error_message=detail)
        return templates.TemplateResponse(
            request=request,
            name="trusted_sources.html",
            context={"request": request, **context},
        )
    return RedirectResponse(url="/trusted-sources", status_code=303)


@app.post("/settings/trusted-sources/source", response_class=HTMLResponse, response_model=None)
async def upsert_trusted_source(
    request: Request,
    source_id: str = Form(""),
    name: str = Form(...),
    domain: str = Form(...),
    category: str = Form(...),
    enabled: bool = Form(False),
    priority: int = Form(100),
    source_type: str = Form(...),
    preferred_access: str = Form(...),
    base_url: str = Form(...),
    api_description: str = Form(""),
    auth_type: str = Form("none"),
    auth_env_var: str = Form(""),
    rate_limit_notes: str = Form(""),
    usage_instructions: str = Form(""),
    allowed_paths_text: str = Form(""),
    deny_paths_text: str = Form(""),
    tags_text: str = Form(""),
) -> Response:
    payload = {
        "id": source_id or name,
        "name": name,
        "domain": domain,
        "category": category,
        "enabled": enabled,
        "priority": priority,
        "source_type": source_type,
        "preferred_access": preferred_access,
        "base_url": base_url,
        "api_description": api_description,
        "auth_type": auth_type,
        "auth_env_var": auth_env_var or None,
        "rate_limit_notes": rate_limit_notes or None,
        "usage_instructions": usage_instructions or None,
        "allowed_paths": _split_lines(allowed_paths_text),
        "deny_paths": _split_lines(deny_paths_text),
        "tags": _split_lines(tags_text),
    }
    method = "PUT" if source_id else "POST"
    path = f"/api/settings/trusted-sources/sources/{source_id}" if source_id else "/api/settings/trusted-sources/sources"
    response = await _api_request(method, path, json_payload=payload)
    if response.status_code >= 400:
        detail = _response_detail(response, "Die Quelle konnte nicht gespeichert werden.")
        context = await _load_trusted_sources_context(
            edit_source_id=source_id or None,
            error_message=detail,
        )
        context["source_form_values"].update(payload)
        context["source_form_values"]["allowed_paths_text"] = allowed_paths_text
        context["source_form_values"]["deny_paths_text"] = deny_paths_text
        context["source_form_values"]["tags_text"] = tags_text
        return templates.TemplateResponse(
            request=request,
            name="trusted_sources.html",
            context={"request": request, **context},
        )
    return RedirectResponse(url="/trusted-sources", status_code=303)


@app.post("/settings/trusted-sources/source/{source_id}/toggle", response_class=HTMLResponse, response_model=None)
async def toggle_trusted_source(request: Request, source_id: str) -> Response:
    context = await _load_trusted_sources_context(edit_source_id=source_id)
    source = next((item for item in context["sources"] if item["id"] == source_id), None)
    if source is None:
        context = await _load_trusted_sources_context(error_message=f"Quelle `{source_id}` wurde nicht gefunden.")
        return templates.TemplateResponse(
            request=request,
            name="trusted_sources.html",
            context={"request": request, **context},
        )
    source["enabled"] = not source.get("enabled", False)
    response = await _api_request("PUT", f"/api/settings/trusted-sources/sources/{source_id}", json_payload=source)
    if response.status_code >= 400:
        detail = _response_detail(response, "Der Status der Quelle konnte nicht geändert werden.")
        context = await _load_trusted_sources_context(edit_source_id=source_id, error_message=detail)
        return templates.TemplateResponse(
            request=request,
            name="trusted_sources.html",
            context={"request": request, **context},
        )
    return RedirectResponse(url="/trusted-sources", status_code=303)


@app.post("/settings/trusted-sources/source/{source_id}/delete", response_class=HTMLResponse, response_model=None)
async def delete_trusted_source(request: Request, source_id: str) -> Response:
    response = await _api_request("DELETE", f"/api/settings/trusted-sources/sources/{source_id}")
    if response.status_code >= 400:
        detail = _response_detail(response, "Die Quelle konnte nicht gelöscht werden.")
        context = await _load_trusted_sources_context(error_message=detail)
        return templates.TemplateResponse(
            request=request,
            name="trusted_sources.html",
            context={"request": request, **context},
        )
    return RedirectResponse(url="/trusted-sources", status_code=303)


@app.post("/settings/trusted-sources/import", response_class=HTMLResponse, response_model=None)
async def import_trusted_sources(
    request: Request,
    payload_json: str = Form(...),
) -> Response:
    response = await _api_request(
        "POST",
        "/api/settings/trusted-sources/import",
        json_payload={"payload_json": payload_json},
    )
    if response.status_code >= 400:
        detail = _response_detail(response, "Der Import konnte nicht verarbeitet werden.")
        context = await _load_trusted_sources_context(error_message=detail, import_payload=payload_json)
        return templates.TemplateResponse(
            request=request,
            name="trusted_sources.html",
            context={"request": request, **context},
        )
    return RedirectResponse(url="/trusted-sources", status_code=303)


@app.post("/settings/trusted-sources/dry-run", response_class=HTMLResponse)
async def dry_run_trusted_sources(
    request: Request,
    query: str = Form(...),
    ecosystem: str = Form(""),
    question_type: str = Form(""),
) -> HTMLResponse:
    payload: dict[str, Any] = {"query": query}
    if ecosystem:
        payload["ecosystem"] = ecosystem
    if question_type:
        payload["question_type"] = question_type
    response = await _api_request("POST", "/api/settings/trusted-sources/dry-run", json_payload=payload)
    if response.status_code >= 400:
        detail = _response_detail(response, "Dry-Run konnte nicht ausgeführt werden.")
        context = await _load_trusted_sources_context(error_message=detail)
        return templates.TemplateResponse(request=request, name="trusted_sources.html", context={"request": request, **context})
    context = await _load_trusted_sources_context(dry_run_result=_response_json(response, {}))
    return templates.TemplateResponse(request=request, name="trusted_sources.html", context={"request": request, **context})


@app.post("/settings/trusted-sources/test", response_class=HTMLResponse)
async def test_trusted_source(
    request: Request,
    source_id: str = Form(...),
    query: str = Form("latest stable release"),
) -> HTMLResponse:
    response = await _api_request(
        "POST",
        "/api/settings/trusted-sources/test",
        json_payload={"source_id": source_id, "query": query},
    )
    if response.status_code >= 400:
        detail = _response_detail(response, "Der Quellentest ist fehlgeschlagen.")
        context = await _load_trusted_sources_context(edit_source_id=source_id, error_message=detail)
        return templates.TemplateResponse(request=request, name="trusted_sources.html", context={"request": request, **context})
    context = await _load_trusted_sources_context(edit_source_id=source_id, source_test_result=_response_json(response, {}))
    return templates.TemplateResponse(request=request, name="trusted_sources.html", context={"request": request, **context})


@app.post("/settings/web-search/core", response_class=HTMLResponse, response_model=None)
async def update_web_search_core_settings(
    request: Request,
    primary_web_search_provider: str = Form(...),
    fallback_web_search_provider: str = Form(...),
    require_trusted_sources_first: bool = Form(False),
    allow_general_web_search_fallback: bool = Form(False),
    provider_host_allowlist_text: str = Form(""),
) -> Response:
    current_response = await _api_request("GET", "/api/settings/web-search")
    if current_response.status_code >= 400:
        detail = _response_detail(current_response, "Die aktuellen Web-Search-Einstellungen konnten nicht geladen werden.")
        context = await _load_web_search_context(error_message=detail)
        return templates.TemplateResponse(request=request, name="web_search.html", context={"request": request, **context})
    current = _response_json(current_response, {})
    current.update(
        {
            "primary_web_search_provider": primary_web_search_provider,
            "fallback_web_search_provider": fallback_web_search_provider,
            "require_trusted_sources_first": require_trusted_sources_first,
            "allow_general_web_search_fallback": allow_general_web_search_fallback,
            "provider_host_allowlist": _split_lines(provider_host_allowlist_text),
        }
    )
    response = await _api_request("PUT", "/api/settings/web-search", json_payload=current)
    if response.status_code >= 400:
        detail = _response_detail(response, "Die Web-Search-Einstellungen konnten nicht gespeichert werden.")
        context = await _load_web_search_context(error_message=detail)
        return templates.TemplateResponse(request=request, name="web_search.html", context={"request": request, **context})
    return RedirectResponse(url="/web-search", status_code=303)


@app.post("/settings/web-search/provider", response_class=HTMLResponse, response_model=None)
async def upsert_web_search_provider(
    request: Request,
    provider_id: str = Form(""),
    name: str = Form(...),
    provider_type: str = Form(...),
    enabled: bool = Form(False),
    priority: int = Form(100),
    base_url: str = Form(...),
    search_path: str = Form("/search"),
    method: str = Form("GET"),
    auth_type: str = Form("none"),
    auth_env_var: str = Form(""),
    timeout_seconds: float = Form(20.0),
    max_results: int = Form(8),
    default_language: str = Form("auto"),
    default_categories_text: str = Form("general"),
    safe_search: int = Form(0),
) -> Response:
    normalized_search_path = "/search" if provider_type == "searxng" else search_path
    normalized_method = "GET" if provider_type == "searxng" else method
    normalized_default_language = (default_language.strip() or ("auto" if provider_type == "searxng" else "en"))
    normalized_safe_search = 0 if provider_type == "searxng" and safe_search is None else safe_search
    normalized_categories_text = default_categories_text.strip() or "general"
    payload = {
        "id": provider_id or name,
        "name": name,
        "provider_type": provider_type,
        "enabled": enabled,
        "priority": priority,
        "base_url": base_url,
        "search_path": normalized_search_path,
        "method": normalized_method,
        "auth_type": auth_type,
        "auth_env_var": auth_env_var or None,
        "timeout_seconds": timeout_seconds,
        "max_results": max_results,
        "default_language": normalized_default_language,
        "default_categories": _split_lines(normalized_categories_text),
        "safe_search": normalized_safe_search,
        "health_status": "unknown",
        "last_checked_at": None,
    }
    method_name = "PUT" if provider_id else "POST"
    path = f"/api/settings/web-search/providers/{provider_id}" if provider_id else "/api/settings/web-search/providers"
    response = await _api_request(method_name, path, json_payload=payload)
    if response.status_code >= 400:
        detail = _response_detail(response, "Der Provider konnte nicht gespeichert werden.")
        context = await _load_web_search_context(edit_provider_id=provider_id or None, error_message=detail)
        context["provider_form_values"].update(payload)
        context["provider_form_values"]["default_categories_text"] = normalized_categories_text
        return templates.TemplateResponse(request=request, name="web_search.html", context={"request": request, **context})
    return RedirectResponse(url="/web-search", status_code=303)


@app.post("/settings/web-search/provider/{provider_id}/delete", response_class=HTMLResponse, response_model=None)
async def delete_web_search_provider(request: Request, provider_id: str) -> Response:
    response = await _api_request("DELETE", f"/api/settings/web-search/providers/{provider_id}")
    if response.status_code >= 400:
        detail = _response_detail(response, "Der Provider konnte nicht gelöscht werden.")
        context = await _load_web_search_context(error_message=detail)
        return templates.TemplateResponse(request=request, name="web_search.html", context={"request": request, **context})
    return RedirectResponse(url="/web-search", status_code=303)


@app.post("/settings/web-search/provider/test", response_class=HTMLResponse)
async def test_web_search_provider(
    request: Request,
    provider_id: str = Form(...),
    query: str = Form("python packaging official docs"),
) -> HTMLResponse:
    response = await _api_request(
        "POST",
        "/api/settings/web-search/test",
        json_payload={"provider_id": provider_id, "query": query},
    )
    if response.status_code >= 400:
        detail = _response_detail(response, "Der Providertest ist fehlgeschlagen.")
        context = await _load_web_search_context(edit_provider_id=provider_id, error_message=detail)
        return templates.TemplateResponse(request=request, name="web_search.html", context={"request": request, **context})
    context = await _load_web_search_context(edit_provider_id=provider_id, provider_test_result=_response_json(response, {}))
    return templates.TemplateResponse(request=request, name="web_search.html", context={"request": request, **context})


@app.post("/settings/web-search/provider/{provider_id}/health", response_class=HTMLResponse)
async def health_check_web_search_provider(request: Request, provider_id: str) -> HTMLResponse:
    response = await _api_request("POST", f"/api/settings/web-search/health/{provider_id}")
    if response.status_code >= 400:
        detail = _response_detail(response, "Der Health-Check ist fehlgeschlagen.")
        context = await _load_web_search_context(edit_provider_id=provider_id, error_message=detail)
        return templates.TemplateResponse(request=request, name="web_search.html", context={"request": request, **context})
    context = await _load_web_search_context(edit_provider_id=provider_id, provider_test_result=_response_json(response, {}))
    return templates.TemplateResponse(request=request, name="web_search.html", context={"request": request, **context})


@app.post("/tasks/{task_id}/run")
async def run_task(request: Request, task_id: str) -> Response:
    response = await _api_request("POST", f"/api/tasks/{task_id}/run")
    if response.status_code >= 400:
        detail = _response_detail(response, "Der Workflow konnte nicht gestartet oder fortgesetzt werden.")
        try:
            context = await _load_task_detail_context(task_id, error_message=detail)
            return templates.TemplateResponse(request=request, name="task.html", context={"request": request, **context})
        except RuntimeError:
            dashboard_context = await _load_dashboard_context(error_message=detail)
            return templates.TemplateResponse(
                request=request,
                name="index.html",
                context={"request": request, **dashboard_context},
            )
    return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)


@app.post("/tasks/{task_id}/archive")
async def archive_task(
    request: Request,
    task_id: str,
    reason: str = Form(""),
) -> Response:
    response = await _api_request(
        "POST",
        f"/api/tasks/{task_id}/archive",
        json_payload={"actor": "dashboard", "reason": reason.strip() or None},
    )
    if response.status_code >= 400:
        detail = _response_detail(response, "Die Aufgabe konnte nicht archiviert werden.")
        try:
            context = await _load_task_detail_context(task_id, error_message=detail)
            return templates.TemplateResponse(request=request, name="task.html", context={"request": request, **context})
        except RuntimeError:
            dashboard_context = await _load_dashboard_context(error_message=detail, show_archived=True)
            return templates.TemplateResponse(
                request=request,
                name="index.html",
                context={"request": request, **dashboard_context},
            )
    return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)


@app.post("/tasks/{task_id}/restore")
async def restore_task(
    request: Request,
    task_id: str,
    reason: str = Form(""),
) -> Response:
    response = await _api_request(
        "POST",
        f"/api/tasks/{task_id}/restore",
        json_payload={"actor": "dashboard", "reason": reason.strip() or None},
    )
    if response.status_code >= 400:
        detail = _response_detail(response, "Die Aufgabe konnte nicht wiederhergestellt werden.")
        try:
            context = await _load_task_detail_context(task_id, error_message=detail)
            return templates.TemplateResponse(request=request, name="task.html", context={"request": request, **context})
        except RuntimeError:
            archive_context = await _load_archive_context(error_message=detail, highlight_task_id=task_id)
            return templates.TemplateResponse(
                request=request,
                name="archive.html",
                context={"request": request, **archive_context},
            )
    return RedirectResponse(url="/archive", status_code=303)


@app.post("/tasks/{task_id}/restart-stage")
async def restart_task_stage(
    request: Request,
    task_id: str,
    worker_name: str = Form(...),
    reason: str = Form(""),
) -> Response:
    response = await _api_request(
        "POST",
        f"/api/tasks/{task_id}/restart-stage",
        json_payload={
            "worker_name": worker_name,
            "actor": "dashboard",
            "reason": reason.strip() or None,
            "run_immediately": True,
        },
    )
    if response.status_code >= 400:
        detail = _response_detail(response, "Der Teilbereich konnte nicht neu gestartet werden.")
        try:
            context = await _load_task_detail_context(task_id, error_message=detail)
            return templates.TemplateResponse(request=request, name="task.html", context={"request": request, **context})
        except RuntimeError:
            dashboard_context = await _load_dashboard_context(error_message=detail)
            return templates.TemplateResponse(
                request=request,
                name="index.html",
                context={"request": request, **dashboard_context},
            )
    return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)


@app.post("/tasks/{task_id}/approve")
async def approve_task(request: Request, task_id: str, gate_name: str = Form("risk-review")) -> Response:
    payload = {"gate_name": gate_name, "decision": "APPROVE", "actor": "dashboard"}
    response = await _api_request("POST", f"/api/tasks/{task_id}/approvals", json_payload=payload)
    if response.status_code >= 400:
        detail = _response_detail(response, "Die Freigabe konnte nicht gespeichert werden.")
        try:
            context = await _load_task_detail_context(task_id, error_message=detail)
            return templates.TemplateResponse(request=request, name="task.html", context={"request": request, **context})
        except RuntimeError:
            dashboard_context = await _load_dashboard_context(error_message=detail)
            return templates.TemplateResponse(
                request=request,
                name="index.html",
                context={"request": request, **dashboard_context},
            )
    return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)


@app.post("/tasks/{task_id}/reject")
async def reject_task(
    request: Request,
    task_id: str,
    gate_name: str = Form("risk-review"),
    reason: str = Form("Rejected from dashboard review."),
) -> Response:
    payload = {"gate_name": gate_name, "decision": "REJECT", "actor": "dashboard", "reason": reason}
    response = await _api_request("POST", f"/api/tasks/{task_id}/approvals", json_payload=payload)
    if response.status_code >= 400:
        detail = _response_detail(response, "Die Ablehnung konnte nicht gespeichert werden.")
        try:
            context = await _load_task_detail_context(task_id, error_message=detail)
            return templates.TemplateResponse(request=request, name="task.html", context={"request": request, **context})
        except RuntimeError:
            dashboard_context = await _load_dashboard_context(error_message=detail)
            return templates.TemplateResponse(
                request=request,
                name="index.html",
                context={"request": request, **dashboard_context},
            )
    return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)


async def _submit_suggestion_decision(
    request: Request,
    *,
    suggestion_id: str,
    task_id: str,
    decision: str,
    note: str,
    error_message: str,
) -> Response:
    """Send a suggestion decision to the orchestrator and keep the operator on a readable screen on failure."""

    response = await _api_request(
        "POST",
        f"/api/suggestions/{suggestion_id}/decision",
        json_payload={"decision": decision, "actor": "ceo-dashboard", "note": note},
    )
    if response.status_code >= 400:
        detail = _response_detail(response, error_message)
        context = await _load_suggestions_context(task_id=task_id or None, error_message=detail)
        return templates.TemplateResponse(
            request=request,
            name="suggestions.html",
            context={"request": request, **context},
        )
    target = f"/tasks/{task_id}" if task_id else "/suggestions"
    return RedirectResponse(url=target, status_code=303)


@app.post("/suggestions/{suggestion_id}/approve", response_class=HTMLResponse, response_model=None)
async def approve_suggestion(
    request: Request,
    suggestion_id: str,
    task_id: str = Form(""),
    note: str = Form("CEO approval granted from dashboard."),
) -> Response:
    return await _submit_suggestion_decision(
        request,
        suggestion_id=suggestion_id,
        task_id=task_id,
        decision="approved",
        note=note,
        error_message="Die Anregung konnte nicht freigegeben werden.",
    )


async def _load_self_improvement_context(
    *,
    error_message: str | None = None,
    success_message: str | None = None,
) -> dict[str, Any]:
    """Load self-improvement status, cycles history, and config from the orchestrator."""

    status_response, cycles_response, sessions_response, config_response, policy_response, incidents_response = await asyncio.gather(
        _api_request("GET", "/api/self-improvement/status"),
        _api_request("GET", "/api/self-improvement/cycles"),
        _api_request("GET", "/api/self-improvement/sessions"),
        _api_request("GET", "/api/settings/self-improvement"),
        _api_request("GET", "/api/settings/self-improvement/policy"),
        _api_request("GET", "/api/self-improvement/incidents"),
        return_exceptions=False,
    )
    messages = [error_message] if error_message else []

    status = _as_mapping(_response_json(status_response, {}))
    if status_response.status_code >= 400:
        messages.append(
            _response_detail(status_response, "Self-Improvement-Status konnte nicht geladen werden.")
        )

    cycles = [item for item in _as_list(_response_json(cycles_response, [])) if isinstance(item, dict)]
    if cycles_response.status_code >= 400:
        messages.append(
            _response_detail(cycles_response, "Zyklus-Historie konnte nicht geladen werden.")
        )

    sessions = [item for item in _as_list(_response_json(sessions_response, [])) if isinstance(item, dict)]
    if sessions_response.status_code >= 400:
        messages.append(
            _response_detail(sessions_response, "Session-Historie konnte nicht geladen werden.")
        )

    config = _as_mapping(_response_json(config_response, {}))
    if config_response.status_code >= 400:
        messages.append(
            _response_detail(config_response, "Self-Improvement-Konfiguration konnte nicht geladen werden.")
        )

    policy = _as_mapping(_response_json(policy_response, {}))
    if policy_response.status_code >= 400:
        messages.append(
            _response_detail(policy_response, "Governance-Policy konnte nicht geladen werden.")
        )

    incidents = [item for item in _as_list(_response_json(incidents_response, [])) if isinstance(item, dict)]
    if incidents_response.status_code >= 400:
        messages.append(
            _response_detail(incidents_response, "Incident-Liste konnte nicht geladen werden.")
        )

    active_cycle = _as_mapping(status.get("active_cycle")) if status.get("active_cycle") else None
    active_session = _as_mapping(status.get("active_session")) if status.get("active_session") else None
    last_cycle = _as_mapping(status.get("last_cycle")) if status.get("last_cycle") else None
    pending_review_cycles = [
        _as_mapping(item) for item in _as_list(status.get("pending_review_cycles", [])) if isinstance(item, dict)
    ]

    task_ids_to_resolve: list[str] = []
    for cycle in cycles:
        if cycle.get("task_id"):
            task_ids_to_resolve.append(str(cycle["task_id"]))
    for cycle in pending_review_cycles:
        if cycle.get("task_id"):
            task_ids_to_resolve.append(str(cycle["task_id"]))
    if active_cycle and active_cycle.get("task_id"):
        task_ids_to_resolve.append(str(active_cycle["task_id"]))
    if last_cycle and last_cycle.get("task_id"):
        task_ids_to_resolve.append(str(last_cycle["task_id"]))
    for incident in incidents:
        if incident.get("task_id"):
            task_ids_to_resolve.append(str(incident["task_id"]))
        if incident.get("rollback_task_id"):
            task_ids_to_resolve.append(str(incident["rollback_task_id"]))

    task_lookup = await _load_task_lookup(task_ids_to_resolve)

    for cycle in cycles:
        cycle["started_at_display"] = _format_timestamp(cycle.get("started_at"))
        cycle["completed_at_display"] = _format_timestamp(cycle.get("completed_at")) if cycle.get("completed_at") else "—"
        _attach_task_reference(cycle, source_key="task_id", target_key="task_ref", task_lookup=task_lookup)
    for cycle in pending_review_cycles:
        cycle["started_at_display"] = _format_timestamp(cycle.get("started_at"))
        cycle["completed_at_display"] = (
            _format_timestamp(cycle.get("completed_at")) if cycle.get("completed_at") else "—"
        )
        _attach_task_reference(cycle, source_key="task_id", target_key="task_ref", task_lookup=task_lookup)
    for incident in incidents:
        incident["created_at_display"] = _format_timestamp(incident.get("created_at"))
        incident["updated_at_display"] = _format_timestamp(incident.get("updated_at"))
        _attach_task_reference(incident, source_key="task_id", target_key="task_ref", task_lookup=task_lookup)
        _attach_task_reference(
            incident,
            source_key="rollback_task_id",
            target_key="rollback_task_ref",
            task_lookup=task_lookup,
        )

    if active_cycle:
        active_cycle["started_at_display"] = _format_timestamp(active_cycle.get("started_at"))
        _attach_task_reference(active_cycle, source_key="task_id", target_key="task_ref", task_lookup=task_lookup)
    if active_session:
        active_session["started_at_display"] = _format_timestamp(active_session.get("started_at"))
        active_session["updated_at_display"] = _format_timestamp(active_session.get("updated_at"))
        active_session["completed_at_display"] = (
            _format_timestamp(active_session.get("completed_at")) if active_session.get("completed_at") else "—"
        )
    if last_cycle and not active_cycle:
        last_cycle["started_at_display"] = _format_timestamp(last_cycle.get("started_at"))
        last_cycle["completed_at_display"] = (
            _format_timestamp(last_cycle.get("completed_at")) if last_cycle.get("completed_at") else "—"
        )
        _attach_task_reference(last_cycle, source_key="task_id", target_key="task_ref", task_lookup=task_lookup)
    for session in sessions:
        session["started_at_display"] = _format_timestamp(session.get("started_at"))
        session["updated_at_display"] = _format_timestamp(session.get("updated_at"))
        session["completed_at_display"] = (
            _format_timestamp(session.get("completed_at")) if session.get("completed_at") else "—"
        )

    return {
        "si_status": status,
        "si_cycles": cycles,
        "si_sessions": sessions,
        "si_config": config,
        "si_policy": policy,
        "si_active_cycle": active_cycle,
        "si_active_session": active_session,
        "si_last_cycle": last_cycle,
        "si_pending_review_cycles": pending_review_cycles,
        "si_incidents": incidents,
        "si_enabled": bool(status.get("enabled")),
        "si_can_start": bool(status.get("can_start")),
        "si_daily_count": int(status.get("daily_cycle_count") or 0),
        "si_max_per_day": int(status.get("max_cycles_per_day") or 0),
        "si_mode": str(status.get("mode") or config.get("mode") or "manual"),
        "si_open_incident_count": int(status.get("open_incident_count") or 0),
        "error_message": " ".join(item for item in messages if item) or None,
        "success_message": success_message,
    }


@app.get("/archive", response_class=HTMLResponse)
async def archive_page(request: Request, task_id: str | None = Query(default=None)) -> HTMLResponse:
    context = await _load_archive_context(highlight_task_id=task_id)
    return templates.TemplateResponse(
        request=request,
        name="archive.html",
        context={"request": request, **context},
    )


@app.get("/self-improvement", response_class=HTMLResponse)
async def self_improvement_page(request: Request) -> HTMLResponse:
    context = await _load_self_improvement_context()
    return templates.TemplateResponse(
        request=request,
        name="self_improvement.html",
        context={"request": request, **context},
    )


@app.post("/self-improvement/start", response_class=HTMLResponse, response_model=None)
async def start_self_improvement(
    request: Request,
    problem_hint: str = Form(""),
    force: bool = Form(False),
) -> Response:
    payload: dict[str, Any] = {"trigger": "manual", "force": force}
    if problem_hint.strip():
        payload["problem_hint"] = problem_hint.strip()
    response = await _api_request("POST", "/api/self-improvement/start", json_payload=payload)
    if response.status_code >= 400:
        detail = _response_detail(response, "Der Zyklus konnte nicht gestartet werden.")
        context = await _load_self_improvement_context(error_message=detail)
        return templates.TemplateResponse(
            request=request,
            name="self_improvement.html",
            context={"request": request, **context},
        )
    return RedirectResponse(url="/self-improvement", status_code=303)


@app.post("/self-improvement/session/start", response_class=HTMLResponse, response_model=None)
async def start_self_improvement_session(
    request: Request,
    problem_hint: str = Form(""),
    force: bool = Form(False),
    max_cycles: int = Form(3),
) -> Response:
    payload: dict[str, Any] = {
        "trigger": "overnight",
        "force": force,
        "max_cycles": max(1, min(int(max_cycles), 10)),
    }
    if problem_hint.strip():
        payload["problem_hint"] = problem_hint.strip()
    response = await _api_request("POST", "/api/self-improvement/session/start", json_payload=payload)
    if response.status_code >= 400:
        detail = _response_detail(response, "Die Selbstreparatur-Session konnte nicht gestartet werden.")
        context = await _load_self_improvement_context(error_message=detail)
        return templates.TemplateResponse(
            request=request,
            name="self_improvement.html",
            context={"request": request, **context},
        )
    return RedirectResponse(url="/self-improvement", status_code=303)


@app.post("/self-improvement/stop", response_class=HTMLResponse, response_model=None)
async def stop_self_improvement(request: Request) -> Response:
    response = await _api_request("POST", "/api/self-improvement/stop")
    if response.status_code >= 400:
        detail = _response_detail(response, "Der Zyklus konnte nicht gestoppt werden.")
        context = await _load_self_improvement_context(error_message=detail)
        return templates.TemplateResponse(
            request=request,
            name="self_improvement.html",
            context={"request": request, **context},
        )
    return RedirectResponse(url="/self-improvement", status_code=303)


@app.post("/self-improvement/session/stop", response_class=HTMLResponse, response_model=None)
async def stop_self_improvement_session(request: Request) -> Response:
    response = await _api_request("POST", "/api/self-improvement/session/stop")
    if response.status_code >= 400:
        detail = _response_detail(response, "Die Selbstreparatur-Session konnte nicht gestoppt werden.")
        context = await _load_self_improvement_context(error_message=detail)
        return templates.TemplateResponse(
            request=request,
            name="self_improvement.html",
            context={"request": request, **context},
        )
    return RedirectResponse(url="/self-improvement", status_code=303)


@app.post(
    "/self-improvement/cycles/{cycle_id}/approve",
    response_class=HTMLResponse,
    response_model=None,
)
async def approve_self_improvement_cycle(
    request: Request,
    cycle_id: str,
    reason: str = Form(""),
) -> Response:
    payload = {"actor": "dashboard-operator", "reason": reason.strip() or None}
    response = await _api_request(
        "POST",
        f"/api/self-improvement/cycles/{cycle_id}/approve",
        json_payload=payload,
    )
    if response.status_code >= 400:
        detail = _response_detail(response, "Die Freigabe konnte nicht gespeichert werden.")
        context = await _load_self_improvement_context(error_message=detail)
        return templates.TemplateResponse(
            request=request,
            name="self_improvement.html",
            context={"request": request, **context},
        )
    return RedirectResponse(url="/self-improvement", status_code=303)


@app.post("/suggestions/{suggestion_id}/reject", response_class=HTMLResponse, response_model=None)
async def reject_suggestion(
    request: Request,
    suggestion_id: str,
    task_id: str = Form(""),
    note: str = Form("Repo-weite Unterdrueckung durch CEO-Dashboard."),
) -> Response:
    return await _submit_suggestion_decision(
        request,
        suggestion_id=suggestion_id,
        task_id=task_id,
        decision="dismissed",
        note=note,
        error_message="Die Anregung konnte nicht verworfen werden.",
    )


@app.post("/suggestions/{suggestion_id}/implement", response_class=HTMLResponse, response_model=None)
async def implement_suggestion(
    request: Request,
    suggestion_id: str,
    task_id: str = Form(""),
    note: str = Form("Repo-weite Umsetzung bestaetigt."),
) -> Response:
    return await _submit_suggestion_decision(
        request,
        suggestion_id=suggestion_id,
        task_id=task_id,
        decision="implemented",
        note=note,
        error_message="Die Umsetzung konnte nicht gespeichert werden.",
    )


@app.post("/suggestions/{suggestion_id}/suppress", response_class=HTMLResponse, response_model=None)
async def suppress_suggestion_for_repository(
    request: Request,
    suggestion_id: str,
    task_id: str = Form(""),
    note: str = Form("Repo-weite Unterdrueckung bestaetigt."),
) -> Response:
    return await _submit_suggestion_decision(
        request,
        suggestion_id=suggestion_id,
        task_id=task_id,
        decision="suppressed_for_repository",
        note=note,
        error_message="Die repo-weite Unterdrueckung konnte nicht gespeichert werden.",
    )
