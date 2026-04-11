"""
Purpose: Minimal dashboard and operations UI for tasks, trusted sources, and web-search provider settings.
Input/Output: Operators use HTML forms backed by the orchestrator API to manage workflow and research guardrails.
Important invariants: The UI is read-mostly, approval actions remain explicit, and it never bypasses orchestrator state management.
How to debug: If a form stops working, inspect the orchestrator base URL, the called endpoint, and the returned JSON error detail.
"""

from __future__ import annotations

import io
import json
import os
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from services.shared.agentic_lab.config import get_settings
from services.shared.agentic_lab.logging_utils import configure_logging
from services.shared.agentic_lab.schemas import HealthResponse, TaskStatus

settings = get_settings()
logger = configure_logging(settings.service_name, settings.log_level)
app = FastAPI(title="Feberdin Agent Team Dashboard", version="0.1.0")

# Why this exists:
# The UI should import both inside the container (`/app/...`) and in local tests where the
# repository lives in a normal workspace path. Resolving from this file keeps the setup portable.
WEB_UI_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=str(WEB_UI_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(WEB_UI_DIR / "templates"))


def _safe_json(value: Any) -> str:
    """Render debug payloads without crashing templates on unexpected runtime types."""

    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


templates.env.filters["safe_json"] = _safe_json

WORKER_SEQUENCE: tuple[dict[str, str], ...] = (
    {
        "worker_name": "requirements",
        "label": "Requirements",
        "description": "Auftrag, Annahmen und Akzeptanzkriterien werden strukturiert.",
    },
    {
        "worker_name": "cost",
        "label": "Ressourcenplanung",
        "description": "Modell- und Ressourcenbedarf werden eingeschätzt.",
    },
    {
        "worker_name": "human_resources",
        "label": "Worker-Auswahl",
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
        "label": "Coding",
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
        "label": "Security",
        "description": "Sicherheits- und Secret-Risiken werden ueberprueft.",
    },
    {
        "worker_name": "validation",
        "label": "Validierung",
        "description": "Ergebnis und Auftrag werden gegenprueft.",
    },
    {
        "worker_name": "documentation",
        "label": "Dokumentation",
        "description": "Betriebs- und Handover-Hinweise werden verdichtet.",
    },
    {
        "worker_name": "github",
        "label": "GitHub",
        "description": "Commit, Push und Pull Request werden vorbereitet oder erstellt.",
    },
    {
        "worker_name": "deploy",
        "label": "Staging Deploy",
        "description": "Der Staging-Rollout wird ausgefuehrt, falls aktiviert.",
    },
    {
        "worker_name": "qa",
        "label": "QA",
        "description": "Smoke-Checks und Abnahmehinweise werden gesammelt.",
    },
    {
        "worker_name": "memory",
        "label": "Memory",
        "description": "Entscheidungen und Learnings werden dauerhaft gespeichert.",
    },
)
WORKER_SEQUENCE_INDEX = {item["worker_name"]: index for index, item in enumerate(WORKER_SEQUENCE)}
WORKER_LABELS = {item["worker_name"]: item["label"] for item in WORKER_SEQUENCE}
WORKER_DESCRIPTIONS = {item["worker_name"]: item["description"] for item in WORKER_SEQUENCE}
STATUS_TO_WORKER_HINT = {
    TaskStatus.REQUIREMENTS.value: "requirements",
    TaskStatus.RESOURCE_PLANNING.value: "human_resources",
    TaskStatus.RESEARCHING.value: "research",
    TaskStatus.ARCHITECTING.value: "architecture",
    TaskStatus.CODING.value: "coding",
    TaskStatus.REVIEWING.value: "reviewer",
    TaskStatus.TESTING.value: "tester",
    TaskStatus.SECURITY_REVIEW.value: "security",
    TaskStatus.VALIDATING.value: "validation",
    TaskStatus.DOCUMENTING.value: "documentation",
    TaskStatus.PR_CREATED.value: "github",
    TaskStatus.STAGING_DEPLOYED.value: "deploy",
    TaskStatus.QA_PENDING.value: "qa",
    TaskStatus.MEMORY_UPDATING.value: "memory",
}
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
AUTO_REFRESH_SECONDS = 15
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
}
HOST_LOG_COMMANDS = """\
Diese Befehle laufen auf dem Unraid-Host und koennen nicht direkt aus der Web-UI geladen werden.

docker compose ps
docker compose logs --tail=200 web-ui
docker compose logs --tail=200 orchestrator
docker compose logs --tail=200 requirements-worker
docker logs --tail=200 fmac-web
docker logs --tail=200 fmac-orch
docker logs --tail=200 fmac-req
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
        suggestion["created_at_display"] = _format_timestamp(suggestion.get("created_at"))
        suggestion["updated_at_display"] = _format_timestamp(suggestion.get("updated_at"))
        normalized.append(suggestion)
    return normalized


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
        return value
    if value in {None, ""}:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _format_timestamp(value: Any) -> str:
    """Render timestamps consistently so long-running stages remain readable at a glance."""

    parsed = _parse_timestamp(value)
    if parsed is None:
        return str(value or "unbekannt")
    return parsed.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


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

    for event in reversed(_as_list(task.get("events"))):
        if not isinstance(event, dict):
            continue
        details = _as_mapping(event.get("details"))
        worker_name = details.get("worker_name")
        if worker_name:
            return str(worker_name)
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


def _build_worker_timeline(task: dict[str, Any]) -> list[dict[str, Any]]:
    """Build a deterministic per-worker timeline so slow stages remain readable and explainable."""

    current_worker = _current_worker_name(task)
    task_status = str(task.get("status", TaskStatus.NEW.value))
    completed_workers = set(_normalize_worker_results(task.get("worker_results")).keys())
    completed_indices = [WORKER_SEQUENCE_INDEX[name] for name in completed_workers if name in WORKER_SEQUENCE_INDEX]
    last_completed_index = max(completed_indices) if completed_indices else -1
    timeline: list[dict[str, Any]] = []

    for step in WORKER_SEQUENCE:
        worker_name = step["worker_name"]
        latest_event = _find_last_worker_event(task, worker_name)
        state = "waiting"
        if worker_name in completed_workers:
            state = "complete"
        elif task_status == TaskStatus.FAILED.value and worker_name == current_worker:
            state = "failed"
        elif task_status == TaskStatus.APPROVAL_REQUIRED.value and worker_name == current_worker:
            state = "blocked"
        elif task_status in ACTIVE_TASK_STATUSES and worker_name == current_worker:
            state = "running"
        elif last_completed_index > WORKER_SEQUENCE_INDEX[worker_name]:
            state = "skipped"

        timeline.append(
            {
                **step,
                "state": state,
                "last_event_at_display": _format_timestamp(latest_event.get("created_at")) if latest_event else "noch keine Aktivitaet",
                "last_event_message": latest_event.get("message") if latest_event else "Noch kein Ereignis fuer diesen Schritt vorhanden.",
            }
        )
    return timeline


def _decorate_events(task: dict[str, Any]) -> list[dict[str, Any]]:
    """Prepare event timestamps and badges for the template without mutating the API contract."""

    decorated: list[dict[str, Any]] = []
    for event in _normalize_events(task.get("events")):
        details = _as_mapping(event.get("details"))
        decorated.append(
            {
                **event,
                "created_at_display": _format_timestamp(event.get("created_at")),
                "level_lower": str(event.get("level", "info")).lower(),
                "worker_label": WORKER_LABELS.get(str(details.get("worker_name")), str(details.get("worker_name", ""))),
                "is_heartbeat": bool(details.get("heartbeat")),
                "event_kind": str(details.get("event_kind") or "note"),
            }
        )
    return decorated


def _build_worker_cast(task: dict[str, Any]) -> list[dict[str, Any]]:
    """Build a visual worker overview with avatar cards and short thought or speech bubbles."""

    worker_results = _normalize_worker_results(task.get("worker_results"))
    current_worker = _current_worker_name(task)
    cast: list[dict[str, Any]] = []

    for step in _build_worker_timeline(task):
        worker_name = step["worker_name"]
        latest_event = _find_last_worker_event(task, worker_name)
        latest_details = _as_mapping(latest_event.get("details")) if latest_event else {}
        result = worker_results.get(worker_name, {})
        state = str(step["state"])
        bubble_kind = "quiet"
        bubble_text = step["description"]
        directed_to = ""

        if state == "running":
            bubble_kind = "thought"
            bubble_text = str(
                latest_event.get("message")
                if latest_event
                else f"{step['label']} bearbeitet gerade diese Stage."
            )
        elif state == "complete":
            bubble_kind = "speech"
            bubble_text = str(result.get("summary") or step["last_event_message"])
            next_worker = _next_worker_name(worker_name)
            if next_worker:
                directed_to = WORKER_LABELS.get(next_worker, next_worker)
        elif state == "blocked":
            bubble_kind = "speech"
            bubble_text = str(task.get("approval_reason") or step["last_event_message"])
        elif state == "failed":
            bubble_kind = "speech"
            bubble_text = str(task.get("latest_error") or step["last_event_message"])

        cast.append(
            {
                **step,
                "initials": _worker_initials(step["label"]),
                "bubble_kind": bubble_kind,
                "bubble_text": bubble_text,
                "activity_display": step["last_event_at_display"],
                "is_current_worker": worker_name == current_worker,
                "directed_to": directed_to,
                "event_kind": str(latest_details.get("event_kind") or "note"),
            }
        )
    return cast


def _decorate_task(task: dict[str, Any]) -> dict[str, Any]:
    """Enrich a raw task payload with operator-focused progress details for the dashboard and detail page."""

    decorated = dict(task)
    decorated["metadata"] = _as_mapping(decorated.get("metadata"))
    decorated["worker_results"] = _normalize_worker_results(decorated.get("worker_results"))
    decorated["events"] = _normalize_events(decorated.get("events"))
    decorated["risk_flags"] = [str(item) for item in _as_list(decorated.get("risk_flags"))]
    current_worker = _current_worker_name(decorated)
    last_event = decorated["events"][-1] if decorated.get("events") else None
    running_since = _running_since(decorated, current_worker)
    if running_since is not None:
        running_for_seconds = round((datetime.now(UTC) - running_since).total_seconds(), 1)
    else:
        running_for_seconds = None

    decorated["events"] = _decorate_events(decorated)
    decorated["worker_timeline"] = _build_worker_timeline(decorated)
    decorated["worker_cast"] = _build_worker_cast(decorated)
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
    decorated["current_stage_state"] = (
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
    decorated["running_since_display"] = _format_timestamp(running_since) if running_since else "noch nicht sichtbar"
    decorated["running_for_display"] = _format_duration(running_for_seconds)
    decorated["is_active"] = str(decorated.get("status")) in ACTIVE_TASK_STATUSES
    decorated["auto_refresh_seconds"] = AUTO_REFRESH_SECONDS if decorated["is_active"] else 0
    return decorated


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
        "timeout_seconds": 10,
        "max_results": 8,
        "default_language": "en",
        "default_categories_text": "general",
        "safe_search": 1,
    }


def _default_worker_guidance_form_values() -> dict[str, Any]:
    return {
        "worker_name": "",
        "display_name": "",
        "enabled": False,
        "role_summary": "",
        "operator_recommendations_text": "",
        "decision_preferences_text": "",
        "competence_boundary": "",
        "escalate_beyond_boundary": True,
        "auto_submit_improvement_suggestions": True,
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


async def _load_dashboard_context(error_message: str | None = None, success_message: str | None = None) -> dict[str, Any]:
    messages = [error_message] if error_message else []
    tasks_response = await _api_request("GET", "/api/tasks")
    repo_settings_response = await _api_request("GET", "/api/settings/repository-access")
    suggestions_response = await _api_request("GET", "/api/suggestions")

    tasks = [_decorate_task(task) for task in _as_list(_response_json(tasks_response, [])) if isinstance(task, dict)]
    if tasks_response.status_code >= 400:
        messages.append(_response_detail(tasks_response, "Die Aufgabenliste konnte nicht geladen werden."))

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
        "repository_access_settings": repo_settings,
        "allowed_repositories_text": "\n".join(repo_settings.get("allowed_repositories", [])),
        "pending_suggestions_count": len(pending_suggestions),
        "error_message": " ".join(item for item in messages if item) or None,
        "success_message": success_message,
    }


async def _load_task_detail_context(
    task_id: str,
    *,
    error_message: str | None = None,
    success_message: str | None = None,
) -> dict[str, Any]:
    """Load and enrich one task detail plus related dashboard data for the detail page."""

    response = await _api_request("GET", f"/api/tasks/{task_id}")
    if response.status_code >= 400:
        raise RuntimeError(_response_detail(response, f"Die Aufgabe `{task_id}` konnte nicht geladen werden."))
    task = _decorate_task(_as_mapping(_response_json(response, {})))
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
    rejected = [item for item in suggestions if item.get("status") == "rejected"]

    messages = [error_message] if error_message else []
    if suggestions_response.status_code >= 400:
        messages.append(_response_detail(suggestions_response, "Die Mitarbeiterideen konnten nicht geladen werden."))

    return {
        "suggestion_registry": registry,
        "suggestions": suggestions,
        "pending_suggestions": pending,
        "approved_suggestions": approved,
        "rejected_suggestions": rejected,
        "error_message": " ".join(item for item in messages if item) or None,
        "success_message": success_message,
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    context = await _load_dashboard_context()
    return templates.TemplateResponse(request=request, name="index.html", context={"request": request, **context})


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
async def task_detail(request: Request, task_id: str) -> HTMLResponse:
    try:
        context = await _load_task_detail_context(task_id)
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
    role_summary: str = Form(...),
    operator_recommendations_text: str = Form(""),
    decision_preferences_text: str = Form(""),
    competence_boundary: str = Form(...),
    escalate_beyond_boundary: bool = Form(False),
    auto_submit_improvement_suggestions: bool = Form(False),
) -> Response:
    payload = {
        "worker_name": worker_name,
        "display_name": display_name,
        "enabled": enabled,
        "role_summary": role_summary,
        "operator_recommendations": _split_lines(operator_recommendations_text),
        "decision_preferences": _split_lines(decision_preferences_text),
        "competence_boundary": competence_boundary,
        "escalate_beyond_boundary": escalate_beyond_boundary,
        "auto_submit_improvement_suggestions": auto_submit_improvement_suggestions,
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
    timeout_seconds: float = Form(10.0),
    max_results: int = Form(8),
    default_language: str = Form("en"),
    default_categories_text: str = Form("general"),
    safe_search: int = Form(1),
) -> Response:
    payload = {
        "id": provider_id or name,
        "name": name,
        "provider_type": provider_type,
        "enabled": enabled,
        "priority": priority,
        "base_url": base_url,
        "search_path": search_path,
        "method": method,
        "auth_type": auth_type,
        "auth_env_var": auth_env_var or None,
        "timeout_seconds": timeout_seconds,
        "max_results": max_results,
        "default_language": default_language,
        "default_categories": _split_lines(default_categories_text),
        "safe_search": safe_search,
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
        context["provider_form_values"]["default_categories_text"] = default_categories_text
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


@app.post("/suggestions/{suggestion_id}/approve", response_class=HTMLResponse, response_model=None)
async def approve_suggestion(
    request: Request,
    suggestion_id: str,
    task_id: str = Form(""),
    note: str = Form("CEO approval granted from dashboard."),
) -> Response:
    response = await _api_request(
        "POST",
        f"/api/suggestions/{suggestion_id}/decision",
        json_payload={"decision": "approved", "actor": "ceo-dashboard", "note": note},
    )
    if response.status_code >= 400:
        detail = _response_detail(response, "Die Anregung konnte nicht freigegeben werden.")
        context = await _load_suggestions_context(task_id=task_id or None, error_message=detail)
        return templates.TemplateResponse(
            request=request,
            name="suggestions.html",
            context={"request": request, **context},
        )
    target = f"/tasks/{task_id}" if task_id else "/suggestions"
    return RedirectResponse(url=target, status_code=303)


@app.post("/suggestions/{suggestion_id}/reject", response_class=HTMLResponse, response_model=None)
async def reject_suggestion(
    request: Request,
    suggestion_id: str,
    task_id: str = Form(""),
    note: str = Form("CEO rejected the improvement suggestion."),
) -> Response:
    response = await _api_request(
        "POST",
        f"/api/suggestions/{suggestion_id}/decision",
        json_payload={"decision": "rejected", "actor": "ceo-dashboard", "note": note},
    )
    if response.status_code >= 400:
        detail = _response_detail(response, "Die Anregung konnte nicht abgelehnt werden.")
        context = await _load_suggestions_context(task_id=task_id or None, error_message=detail)
        return templates.TemplateResponse(
            request=request,
            name="suggestions.html",
            context={"request": request, **context},
        )
    target = f"/tasks/{task_id}" if task_id else "/suggestions"
    return RedirectResponse(url=target, status_code=303)
