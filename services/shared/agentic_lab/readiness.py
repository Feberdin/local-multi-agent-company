"""
Purpose: System readiness check that verifies all workers, LLM endpoints, and infrastructure
         are practically usable before a workflow is started.
Input/Output: Call run_system_readiness_check(settings) to get a structured ReadinessReport.
Important invariants: Never logs secret values. Every check is individually timed and labelled.
How to debug: Run GET /api/system/readiness and read each section's check details.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field

from services.shared.agentic_lab.config import Settings

# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------

class CheckStatus(StrEnum):
    OK = "ok"
    WARNING = "warning"
    FAILED = "failed"
    SKIPPED = "skipped"


class ReadinessCheckItem(BaseModel):
    name: str
    label: str
    status: CheckStatus
    duration_ms: float = 0.0
    message: str
    hint: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class ReadinessSection(BaseModel):
    name: str
    label: str
    status: CheckStatus
    checks: list[ReadinessCheckItem] = Field(default_factory=list)


class ReadinessSummary(BaseModel):
    checks_total: int = 0
    checks_ok: int = 0
    checks_warning: int = 0
    checks_failed: int = 0
    checks_skipped: int = 0


class ReadinessReport(BaseModel):
    overall_status: CheckStatus
    started_at: datetime
    finished_at: datetime
    duration_ms: float
    summary: ReadinessSummary
    sections: list[ReadinessSection] = Field(default_factory=list)
    workflow_ready: bool
    workflow_message: str


# ---------------------------------------------------------------------------
# Internal timeouts (all in seconds)
# ---------------------------------------------------------------------------

HEALTH_TIMEOUT = 10.0        # Simple /health checks
WORKER_TIMEOUT = 20.0        # Worker checks (slightly more headroom)
LLM_TIMEOUT = 180.0          # LLM self-test: models on slow hardware can be slow
LLM_CONNECT_TIMEOUT = 15.0   # TCP connect to LLM host
GIT_TIMEOUT = 15             # Git subprocess calls


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_ms() -> float:
    return datetime.now(UTC).timestamp() * 1000


def _elapsed_ms(start: float) -> float:
    return round(_now_ms() - start, 1)


def _section_status(checks: list[ReadinessCheckItem]) -> CheckStatus:
    """Aggregate check results: any failure → failed, any warning → warning, else ok."""
    statuses = {c.status for c in checks if c.status != CheckStatus.SKIPPED}
    if CheckStatus.FAILED in statuses:
        return CheckStatus.FAILED
    if CheckStatus.WARNING in statuses:
        return CheckStatus.WARNING
    if not statuses:
        return CheckStatus.SKIPPED
    return CheckStatus.OK


def _overall_status(sections: list[ReadinessSection]) -> CheckStatus:
    statuses = {s.status for s in sections if s.status != CheckStatus.SKIPPED}
    if CheckStatus.FAILED in statuses:
        return CheckStatus.FAILED
    if CheckStatus.WARNING in statuses:
        return CheckStatus.WARNING
    if not statuses:
        return CheckStatus.SKIPPED
    return CheckStatus.OK


def _summarize(sections: list[ReadinessSection]) -> ReadinessSummary:
    total = ok = warning = failed = skipped = 0
    for section in sections:
        for check in section.checks:
            total += 1
            if check.status == CheckStatus.OK:
                ok += 1
            elif check.status == CheckStatus.WARNING:
                warning += 1
            elif check.status == CheckStatus.FAILED:
                failed += 1
            else:
                skipped += 1
    return ReadinessSummary(
        checks_total=total,
        checks_ok=ok,
        checks_warning=warning,
        checks_failed=failed,
        checks_skipped=skipped,
    )


def _workflow_message(overall: CheckStatus, summary: ReadinessSummary) -> tuple[bool, str]:
    if overall == CheckStatus.OK:
        return True, "System ist einsatzbereit. Workflows koennen gestartet werden."
    if overall == CheckStatus.WARNING:
        return True, (
            f"System ist eingeschraenkt nutzbar ({summary.checks_warning} Warnung(en)). "
            "Workflows koennen gestartet werden, aber einige Dienste sind moeglicherweise langsam oder instabil."
        )
    return False, (
        f"System hat kritische Probleme ({summary.checks_failed} Fehler). "
        "Workflows sollten nicht gestartet werden, bis die markierten Dienste repariert sind."
    )


# ---------------------------------------------------------------------------
# A. Orchestrator API checks
# ---------------------------------------------------------------------------

async def _check_orchestrator(settings: Settings) -> ReadinessSection:
    base = settings.orchestrator_internal_url.rstrip("/")
    endpoints = [
        ("/health", "Orchestrator Health"),
        ("/api/tasks", "Task-API"),
        ("/api/settings/repository-access", "Repository-Allowlist"),
    ]
    checks: list[ReadinessCheckItem] = []

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5.0, read=HEALTH_TIMEOUT, write=5.0, pool=5.0)
    ) as client:
        for path, label in endpoints:
            start = _now_ms()
            url = f"{base}{path}"
            try:
                resp = await client.get(url)
                elapsed = _elapsed_ms(start)
                if resp.status_code < 400:
                    checks.append(ReadinessCheckItem(
                        name=path.lstrip("/").replace("/", "-") or "health",
                        label=label,
                        status=CheckStatus.OK,
                        duration_ms=elapsed,
                        message=f"HTTP {resp.status_code} in {elapsed:.0f} ms",
                        details={"url": url, "http_status": resp.status_code},
                    ))
                else:
                    checks.append(ReadinessCheckItem(
                        name=path.lstrip("/").replace("/", "-") or "health",
                        label=label,
                        status=CheckStatus.FAILED,
                        duration_ms=elapsed,
                        message=f"HTTP {resp.status_code}: {resp.text[:200].strip()}",
                        hint="Pruefe `docker compose logs fmac-orch` fuer Details.",
                        details={"url": url, "http_status": resp.status_code},
                    ))
            except httpx.TimeoutException:
                elapsed = _elapsed_ms(start)
                checks.append(ReadinessCheckItem(
                    name=path.lstrip("/").replace("/", "-") or "health",
                    label=label,
                    status=CheckStatus.FAILED,
                    duration_ms=elapsed,
                    message=f"Timeout nach {elapsed:.0f} ms – der Orchestrator hat nicht rechtzeitig geantwortet.",
                    hint="Pruefe `docker compose ps fmac-orch` und ob der Service laeuft.",
                    details={"url": url},
                ))
            except httpx.HTTPError as exc:
                elapsed = _elapsed_ms(start)
                checks.append(ReadinessCheckItem(
                    name=path.lstrip("/").replace("/", "-") or "health",
                    label=label,
                    status=CheckStatus.FAILED,
                    duration_ms=elapsed,
                    message=f"Verbindungsfehler: {exc}",
                    hint="Pruefe, ob der Orchestrator-Container laeuft und erreichbar ist.",
                    details={"url": url},
                ))

    return ReadinessSection(
        name="orchestrator",
        label="Orchestrator-API",
        status=_section_status(checks),
        checks=checks,
    )


# ---------------------------------------------------------------------------
# B. Worker health checks
# ---------------------------------------------------------------------------

WORKERS: tuple[tuple[str, str, str], ...] = (
    ("requirements", "Anforderungen", "requirements_worker_url"),
    ("cost", "Ressourcenplanung", "cost_worker_url"),
    ("human_resources", "Worker-Auswahl", "human_resources_worker_url"),
    ("research", "Recherche", "research_worker_url"),
    ("architecture", "Architektur", "architecture_worker_url"),
    ("data", "Daten", "data_worker_url"),
    ("ux", "UX", "ux_worker_url"),
    ("coding", "Coding", "coding_worker_url"),
    ("reviewer", "Review", "reviewer_worker_url"),
    ("tester", "Tests", "test_worker_url"),
    ("security", "Security", "security_worker_url"),
    ("validation", "Validierung", "validation_worker_url"),
    ("documentation", "Dokumentation", "documentation_worker_url"),
    ("github", "GitHub", "github_worker_url"),
    ("deploy", "Staging-Deploy", "deploy_worker_url"),
    ("qa", "QA", "qa_worker_url"),
    ("memory", "Wissen", "memory_worker_url"),
)


async def _check_one_worker(
    client: httpx.AsyncClient,
    worker_name: str,
    label: str,
    url: str,
) -> ReadinessCheckItem:
    health_url = f"{url.rstrip('/')}/health"
    start = _now_ms()
    try:
        resp = await client.get(health_url)
        elapsed = _elapsed_ms(start)
        if resp.status_code < 400:
            status = CheckStatus.OK
            msg = f"Erreichbar – HTTP {resp.status_code} in {elapsed:.0f} ms"
            if elapsed > 5000:
                status = CheckStatus.WARNING
                msg = f"Erreichbar aber sehr langsam ({elapsed:.0f} ms)"
        else:
            status = CheckStatus.FAILED
            msg = f"HTTP {resp.status_code} – Service meldet Fehler"
        return ReadinessCheckItem(
            name=worker_name,
            label=label,
            status=status,
            duration_ms=elapsed,
            message=msg,
            details={"url": health_url, "http_status": resp.status_code},
        )
    except httpx.TimeoutException:
        elapsed = _elapsed_ms(start)
        return ReadinessCheckItem(
            name=worker_name,
            label=label,
            status=CheckStatus.FAILED,
            duration_ms=elapsed,
            message=f"Timeout nach {elapsed:.0f} ms",
            hint=f"Pruefe `docker compose logs` fuer den {label}-Worker.",
            details={"url": health_url},
        )
    except httpx.HTTPError as exc:
        elapsed = _elapsed_ms(start)
        return ReadinessCheckItem(
            name=worker_name,
            label=label,
            status=CheckStatus.FAILED,
            duration_ms=elapsed,
            message=f"Nicht erreichbar: {exc}",
            hint=f"Pruefe, ob der {label}-Worker-Container laeuft.",
            details={"url": health_url},
        )


async def _check_workers(settings: Settings) -> ReadinessSection:
    timeout = httpx.Timeout(connect=5.0, read=WORKER_TIMEOUT, write=5.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        tasks = []
        for worker_name, label, settings_attr in WORKERS:
            url = getattr(settings, settings_attr, "")
            tasks.append(_check_one_worker(client, worker_name, label, url))
        checks = list(await asyncio.gather(*tasks))

    return ReadinessSection(
        name="workers",
        label="Worker-Erreichbarkeit",
        status=_section_status(checks),
        checks=checks,
    )


# ---------------------------------------------------------------------------
# C. LLM connectivity
# ---------------------------------------------------------------------------

async def _check_one_llm(
    provider_name: str,
    base_url: str,
    model_name: str,
    api_key: str,
) -> ReadinessCheckItem:
    if not base_url or not model_name:
        return ReadinessCheckItem(
            name=f"llm-{provider_name}",
            label=f"LLM {provider_name}",
            status=CheckStatus.SKIPPED,
            message="Nicht konfiguriert (BASE_URL oder MODEL_NAME fehlt).",
            details={"provider": provider_name},
        )

    completions_url = f"{base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": "Respond only with valid JSON."},
            {"role": "user", "content": 'Reply with exactly: {"ok":true}'},
        ],
        "temperature": 0.0,
        "max_tokens": 32,
    }
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key and "replace-me" not in api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    timeout = httpx.Timeout(connect=LLM_CONNECT_TIMEOUT, read=LLM_TIMEOUT, write=10.0, pool=10.0)
    start = _now_ms()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(completions_url, json=payload, headers=headers)
        elapsed = _elapsed_ms(start)
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"].strip()

        json_valid = False
        try:
            parsed = json.loads(content)
            json_valid = isinstance(parsed, dict) and parsed.get("ok") is True
        except json.JSONDecodeError:
            pass

        status = CheckStatus.OK if json_valid else CheckStatus.WARNING
        if elapsed > 60_000:
            status = CheckStatus.WARNING
            message = (
                f"Antwort nach {elapsed / 1000:.0f} s erhalten, aber sehr langsam. "
                "Bei dieser Geschwindigkeit kann es bei komplexen Aufgaben zu Timeouts kommen."
            )
        elif not json_valid:
            message = (
                f"Modell antwortet ({elapsed:.0f} ms), aber JSON-Test fehlgeschlagen. "
                f"Antwort: {content[:120]}"
            )
        else:
            message = f"JSON-Antwort korrekt in {elapsed:.0f} ms"

        return ReadinessCheckItem(
            name=f"llm-{provider_name}",
            label=f"LLM {provider_name} ({model_name})",
            status=status,
            duration_ms=elapsed,
            message=message,
            hint=(
                "Falls sehr langsam: erhoehe LLM_READ_TIMEOUT_SECONDS und WORKER_STAGE_TIMEOUT_SECONDS."
                if elapsed > 60_000 else ""
            ),
            details={
                "provider": provider_name,
                "model": model_name,
                "base_url": base_url,
                "json_valid": json_valid,
                "response_preview": content[:200],
            },
        )

    except httpx.TimeoutException:
        elapsed = _elapsed_ms(start)
        return ReadinessCheckItem(
            name=f"llm-{provider_name}",
            label=f"LLM {provider_name} ({model_name})",
            status=CheckStatus.FAILED,
            duration_ms=elapsed,
            message=(
                f"Das lokale Modell hat innerhalb von {LLM_TIMEOUT:.0f} Sekunden keine "
                "vollstaendige Antwort geliefert. Die Hardware ist moeglicherweise zu langsam "
                "oder das Modell ist noch beim Laden."
            ),
            hint=(
                "Pruefe, ob Ollama laeuft (`docker compose logs fmac-orch`). "
                "Erhoehe LLM_READ_TIMEOUT_SECONDS fuer sehr langsame Hardware."
            ),
            details={"provider": provider_name, "model": model_name, "base_url": base_url},
        )
    except httpx.HTTPStatusError as exc:
        elapsed = _elapsed_ms(start)
        return ReadinessCheckItem(
            name=f"llm-{provider_name}",
            label=f"LLM {provider_name} ({model_name})",
            status=CheckStatus.FAILED,
            duration_ms=elapsed,
            message=f"HTTP {exc.response.status_code} vom LLM-Endpoint: {exc.response.text[:200].strip()}",
            hint="Pruefe, ob das Modell in Ollama geladen ist (`ollama list`).",
            details={"provider": provider_name, "model": model_name, "http_status": exc.response.status_code},
        )
    except httpx.HTTPError as exc:
        elapsed = _elapsed_ms(start)
        return ReadinessCheckItem(
            name=f"llm-{provider_name}",
            label=f"LLM {provider_name} ({model_name})",
            status=CheckStatus.FAILED,
            duration_ms=elapsed,
            message=f"Verbindungsfehler zum LLM-Endpoint: {exc}",
            hint="Pruefe MISTRAL_BASE_URL / QWEN_BASE_URL und ob Ollama erreichbar ist.",
            details={"provider": provider_name, "model": model_name, "base_url": base_url},
        )
    except (KeyError, IndexError, ValueError, json.JSONDecodeError) as exc:
        elapsed = _elapsed_ms(start)
        return ReadinessCheckItem(
            name=f"llm-{provider_name}",
            label=f"LLM {provider_name} ({model_name})",
            status=CheckStatus.WARNING,
            duration_ms=elapsed,
            message=f"Antwort erhalten, aber unerwartetes Format: {exc}",
            details={"provider": provider_name, "model": model_name},
        )


async def _check_llm(settings: Settings) -> ReadinessSection:
    tasks = [
        _check_one_llm("mistral", settings.mistral_base_url, settings.mistral_model_name, settings.mistral_api_key),
        _check_one_llm("qwen", settings.qwen_base_url, settings.qwen_model_name, settings.qwen_api_key),
    ]
    checks = list(await asyncio.gather(*tasks))
    return ReadinessSection(
        name="llm",
        label="LLM-Erreichbarkeit",
        status=_section_status(checks),
        checks=checks,
    )


# ---------------------------------------------------------------------------
# D. Workspace / git readiness
# ---------------------------------------------------------------------------

def _check_path_readable(path: Path, label: str, name: str) -> ReadinessCheckItem:
    start = _now_ms()
    if not path.exists():
        return ReadinessCheckItem(
            name=name, label=label, status=CheckStatus.FAILED,
            duration_ms=_elapsed_ms(start),
            message=f"Verzeichnis existiert nicht: {path}",
            hint="Pruefe, ob die Bind-Mounts in docker-compose.yml korrekt konfiguriert sind.",
            details={"path": str(path)},
        )
    if not os.access(path, os.R_OK):
        return ReadinessCheckItem(
            name=name, label=label, status=CheckStatus.FAILED,
            duration_ms=_elapsed_ms(start),
            message=f"Verzeichnis nicht lesbar: {path}",
            hint="Pruefe PUID/PGID und die Berechtigungen des Host-Verzeichnisses.",
            details={"path": str(path)},
        )
    writable = os.access(path, os.W_OK)
    status = CheckStatus.OK if writable else CheckStatus.WARNING
    msg = f"{'Lesbar und beschreibbar' if writable else 'Nur lesbar (kein Schreibzugriff)'}: {path}"
    hint = "Schreibzugriff fehlt. Pruefe PUID/PGID und Host-Berechtigungen." if not writable else ""
    return ReadinessCheckItem(
        name=name, label=label, status=status,
        duration_ms=_elapsed_ms(start),
        message=msg, hint=hint,
        details={"path": str(path), "writable": writable},
    )


def _check_git_available() -> ReadinessCheckItem:
    import subprocess
    start = _now_ms()
    try:
        result = subprocess.run(
            ["git", "--version"],
            capture_output=True, text=True, timeout=GIT_TIMEOUT, check=False,
        )
        elapsed = _elapsed_ms(start)
        if result.returncode == 0:
            return ReadinessCheckItem(
                name="git-available", label="Git verfuegbar",
                status=CheckStatus.OK, duration_ms=elapsed,
                message=result.stdout.strip(),
                details={"git_version": result.stdout.strip()},
            )
        return ReadinessCheckItem(
            name="git-available", label="Git verfuegbar",
            status=CheckStatus.FAILED, duration_ms=elapsed,
            message=f"git --version fehlgeschlagen: {result.stderr.strip()}",
            hint="Stelle sicher, dass Git im Container installiert ist.",
            details={},
        )
    except FileNotFoundError:
        return ReadinessCheckItem(
            name="git-available", label="Git verfuegbar",
            status=CheckStatus.FAILED, duration_ms=_elapsed_ms(start),
            message="Git-Befehl nicht gefunden. Git ist moeglicherweise nicht installiert.",
            hint="Pruefe das Dockerfile und stelle sicher, dass git installiert ist.",
            details={},
        )
    except Exception as exc:
        return ReadinessCheckItem(
            name="git-available", label="Git verfuegbar",
            status=CheckStatus.WARNING, duration_ms=_elapsed_ms(start),
            message=f"Git-Pruefung fehlgeschlagen: {exc}",
            details={},
        )


def _check_git_home_writable(settings: Settings) -> ReadinessCheckItem:
    start = _now_ms()
    home_path = settings.runtime_home_dir
    try:
        home_path.mkdir(parents=True, exist_ok=True)
        test_file = home_path / ".readiness-check-probe"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink(missing_ok=True)
        return ReadinessCheckItem(
            name="git-home-writable", label="Git HOME beschreibbar",
            status=CheckStatus.OK, duration_ms=_elapsed_ms(start),
            message=f"RUNTIME_HOME_DIR ist beschreibbar: {home_path}",
            details={"path": str(home_path)},
        )
    except PermissionError:
        return ReadinessCheckItem(
            name="git-home-writable", label="Git HOME beschreibbar",
            status=CheckStatus.FAILED, duration_ms=_elapsed_ms(start),
            message=f"RUNTIME_HOME_DIR ist nicht beschreibbar: {home_path}",
            hint=(
                "Git braucht ein beschreibbares HOME-Verzeichnis. "
                "Setze RUNTIME_HOME_DIR auf einen beschreibbaren Pfad und pruefe PUID/PGID."
            ),
            details={"path": str(home_path)},
        )
    except Exception as exc:
        return ReadinessCheckItem(
            name="git-home-writable", label="Git HOME beschreibbar",
            status=CheckStatus.WARNING, duration_ms=_elapsed_ms(start),
            message=f"Pruefung nicht eindeutig: {exc}",
            details={"path": str(home_path)},
        )


def _check_workspace(settings: Settings) -> ReadinessSection:
    checks = [
        _check_path_readable(settings.workspace_root, "Workspace-Verzeichnis", "workspace-root"),
        _check_path_readable(settings.data_dir, "Datenverzeichnis (SQLite)", "data-dir"),
        _check_path_readable(settings.reports_dir, "Berichtsverzeichnis", "reports-dir"),
        _check_git_available(),
        _check_git_home_writable(settings),
    ]
    return ReadinessSection(
        name="workspace",
        label="Workspace und Git",
        status=_section_status(checks),
        checks=checks,
    )


# ---------------------------------------------------------------------------
# E. Secrets and configuration
# ---------------------------------------------------------------------------

def _check_secret_file(path: Path | None, label: str, name: str) -> ReadinessCheckItem:
    start = _now_ms()
    if path is None:
        return ReadinessCheckItem(
            name=name, label=label, status=CheckStatus.SKIPPED,
            duration_ms=_elapsed_ms(start),
            message="Kein Secret-Pfad konfiguriert (optional).",
            details={},
        )
    if not path.exists():
        return ReadinessCheckItem(
            name=name, label=label, status=CheckStatus.WARNING,
            duration_ms=_elapsed_ms(start),
            message=f"Secret-Datei nicht vorhanden: {path}",
            hint="Erstelle die Secret-Datei oder setze den Wert direkt als Umgebungsvariable.",
            details={"path": str(path)},
        )
    readable = os.access(path, os.R_OK)
    if not readable:
        return ReadinessCheckItem(
            name=name, label=label, status=CheckStatus.FAILED,
            duration_ms=_elapsed_ms(start),
            message=f"Secret-Datei vorhanden, aber nicht lesbar: {path}",
            hint="Pruefe die Datei-Berechtigungen und ob PUID/PGID mit dem Datei-Eigentuemer uebereinstimmt.",
            details={"path": str(path)},
        )
    return ReadinessCheckItem(
        name=name, label=label, status=CheckStatus.OK,
        duration_ms=_elapsed_ms(start),
        message="Vorhanden und lesbar.",
        details={"path": str(path)},
    )


def _check_env_var(value: str, var_name: str, label: str, name: str, *, required: bool = True) -> ReadinessCheckItem:
    start = _now_ms()
    is_set = bool(value and "replace-me" not in value.lower())
    if is_set:
        return ReadinessCheckItem(
            name=name, label=label, status=CheckStatus.OK,
            duration_ms=_elapsed_ms(start),
            message=f"{var_name} ist gesetzt.",
            details={"var": var_name},
        )
    status = CheckStatus.FAILED if required else CheckStatus.WARNING
    return ReadinessCheckItem(
        name=name, label=label, status=status,
        duration_ms=_elapsed_ms(start),
        message=f"{var_name} ist nicht gesetzt oder enthaelt noch den Platzhalter.",
        hint=f"Setze {var_name} in der .env-Datei oder als Umgebungsvariable.",
        details={"var": var_name},
    )


def _check_config(settings: Settings) -> ReadinessSection:
    checks = [
        # LLM URLs
        _check_env_var(settings.mistral_base_url, "MISTRAL_BASE_URL", "Mistral Base-URL", "mistral-url"),
        _check_env_var(settings.mistral_model_name, "MISTRAL_MODEL_NAME", "Mistral Modellname", "mistral-model"),
        _check_env_var(settings.qwen_base_url, "QWEN_BASE_URL", "Qwen Base-URL", "qwen-url"),
        _check_env_var(settings.qwen_model_name, "QWEN_MODEL_NAME", "Qwen Modellname", "qwen-model"),
        # GitHub
        _check_env_var(settings.github_token, "GITHUB_TOKEN", "GitHub Token", "github-token", required=False),
        # Secret files
        _check_secret_file(settings.github_token_file, "GitHub Token (Datei)", "github-token-file"),
        _check_secret_file(settings.mistral_api_key_file, "Mistral API-Key (Datei)", "mistral-key-file"),
        _check_secret_file(settings.qwen_api_key_file, "Qwen API-Key (Datei)", "qwen-key-file"),
    ]
    return ReadinessSection(
        name="config",
        label="Konfiguration und Secrets",
        status=_section_status(checks),
        checks=checks,
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def run_system_readiness_check(settings: Settings) -> ReadinessReport:
    """Run all preflight checks and return a structured, human-readable result."""

    started_at = datetime.now(UTC)
    start_ms = _now_ms()

    # Run network checks in parallel, sync checks separately
    orchestrator_section, workers_section, llm_section = await asyncio.gather(
        _check_orchestrator(settings),
        _check_workers(settings),
        _check_llm(settings),
    )
    workspace_section = _check_workspace(settings)
    config_section = _check_config(settings)

    sections = [orchestrator_section, workers_section, llm_section, workspace_section, config_section]
    overall = _overall_status(sections)
    summary = _summarize(sections)
    workflow_ready, workflow_message = _workflow_message(overall, summary)

    finished_at = datetime.now(UTC)
    return ReadinessReport(
        overall_status=overall,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=round(_now_ms() - start_ms, 1),
        summary=summary,
        sections=sections,
        workflow_ready=workflow_ready,
        workflow_message=workflow_message,
    )
