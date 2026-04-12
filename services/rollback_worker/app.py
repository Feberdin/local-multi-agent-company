"""
Purpose: Dedicated rollback and self-update watchdog worker for autonomous recovery on Unraid.
Input/Output:
  - `/run` executes deterministic repository rollback tasks via `git revert`.
  - `/watchdogs/start` arms a persistent self-update monitor that can trigger a host-side rollback.
  - `/watchdogs/{task_id}` returns the last persisted watchdog state for dashboards and restart recovery.
Important invariants:
  - The watchdog state is persisted under DATA_DIR so it survives orchestrator and container restarts.
  - Self-update rollback always restores a concrete previous commit instead of guessing a branch state.
  - The rollback worker must stay outside the self-update restart set so monitoring can continue.
How to debug:
  - Inspect DATA_DIR/self-update-watchdogs/<task-id>.json for heartbeat, observed SHA changes, and rollback errors.
  - Re-run the referenced shell scripts manually if SSH or compose commands fail on the Unraid host.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from services.shared.agentic_lab.config import get_settings
from services.shared.agentic_lab.guardrails import detect_risk_flags
from services.shared.agentic_lab.logging_utils import TaskLoggerAdapter, configure_logging
from services.shared.agentic_lab.repo_tools import (
    CommandError,
    create_branch_name,
    current_diff,
    ensure_branch,
    ensure_repository_checkout,
    git,
    run_command,
    write_report,
)
from services.shared.agentic_lab.schemas import Artifact, HealthResponse, WorkerRequest, WorkerResponse
from services.shared.agentic_lab.self_update_watchdog import (
    SelfUpdateWatchdogState,
    SelfUpdateWatchdogStatus,
    read_watchdog_state,
    write_watchdog_state,
)

settings = get_settings()
logger = configure_logging(settings.service_name, settings.log_level)


class SelfUpdateWatchdogStartRequest(BaseModel):
    task_id: str
    branch_name: str
    ssh_user: str
    ssh_host: str
    ssh_port: int
    ssh_key_file: str = ""
    project_dir: str
    compose_file: str
    health_url: str
    poll_seconds: float | None = None
    timeout_seconds: float | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.watchdog_tasks = {}
    yield
    for task in app.state.watchdog_tasks.values():
        task.cancel()
    for task in app.state.watchdog_tasks.values():
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="Feberdin Rollback Worker", version="0.1.0", lifespan=lifespan)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(service="rollback-worker")


@app.post("/run", response_model=WorkerResponse)
async def run(request: WorkerRequest) -> WorkerResponse:
    task_logger = TaskLoggerAdapter(logger.logger, {"service": "rollback-worker", "task_id": request.task_id})
    repo_path = Path(request.local_repo_path)
    source_repo_path = Path(str(request.metadata.get("source_local_repo_path") or request.local_repo_path))
    rollback_commit_sha = str(request.metadata.get("rollback_commit_sha") or "").strip()
    if not rollback_commit_sha:
        return WorkerResponse(
            worker="rollback",
            success=False,
            summary="Rollback-Worker wurde ohne Rollback-Commit aufgerufen.",
            errors=["metadata.rollback_commit_sha fehlt fuer den deterministischen Rollback-Pfad."],
        )

    try:
        repo_path = ensure_repository_checkout(
            repository=request.repository,
            repo_path=repo_path,
            workspace_root=settings.workspace_root,
            base_branch=request.base_branch,
            repo_url=request.repo_url,
            task_id=request.task_id,
            source_repo_path=source_repo_path,
        )
        branch_name = request.branch_name or create_branch_name(request.goal, request.task_id)
        ensure_branch(repo_path, branch_name, request.base_branch)
        task_logger.info("Using dedicated rollback worker for commit %s.", rollback_commit_sha)
        return _run_git_revert_backend(request, repo_path, branch_name, rollback_commit_sha)
    except Exception as exc:  # pragma: no cover - defensive runtime guard for operator-visible failures.
        task_logger.exception("Rollback worker failed unexpectedly: %s", exc)
        return WorkerResponse(
            worker="rollback",
            success=False,
            summary="Rollback-Stage konnte nicht sauber vorbereitet oder ausgefuehrt werden.",
            errors=[f"{exc.__class__.__name__}: {exc}"],
            outputs={"local_repo_path": str(repo_path)},
        )


@app.post("/watchdogs/start", response_model=SelfUpdateWatchdogState)
async def start_watchdog(request: SelfUpdateWatchdogStartRequest) -> SelfUpdateWatchdogState:
    existing = read_watchdog_state(request.task_id, settings)
    if existing is not None and existing.status in {
        SelfUpdateWatchdogStatus.MONITORING,
        SelfUpdateWatchdogStatus.HEALTHY,
        SelfUpdateWatchdogStatus.ROLLBACK_RUNNING,
        SelfUpdateWatchdogStatus.ROLLED_BACK,
    }:
        return existing

    previous_sha = _read_remote_head(request)
    state = SelfUpdateWatchdogState(
        task_id=request.task_id,
        status=SelfUpdateWatchdogStatus.ARMED,
        branch_name=request.branch_name,
        health_url=request.health_url,
        project_dir=request.project_dir,
        compose_file=request.compose_file,
        ssh_host=request.ssh_host,
        ssh_user=request.ssh_user,
        ssh_port=request.ssh_port,
        previous_sha=previous_sha,
        notes=["Watchdog armed before self-update dispatch."],
    )
    write_watchdog_state(state, settings)

    existing_task = app.state.watchdog_tasks.get(request.task_id)
    if existing_task is not None:
        existing_task.cancel()
        with suppress(asyncio.CancelledError):
            await existing_task

    app.state.watchdog_tasks[request.task_id] = asyncio.create_task(
        _monitor_self_update(request, state)
    )
    return state


@app.get("/watchdogs/{task_id}", response_model=SelfUpdateWatchdogState)
async def get_watchdog(task_id: str) -> SelfUpdateWatchdogState:
    state = read_watchdog_state(task_id, settings)
    if state is None:
        raise HTTPException(status_code=404, detail=f"Kein Self-Update-Watchdog fuer {task_id} gefunden.")
    return state


def _run_git_revert_backend(
    request: WorkerRequest,
    repo_path: Path,
    branch_name: str,
    rollback_commit_sha: str,
) -> WorkerResponse:
    """Apply one deterministic git revert when a rollback task is scheduled."""

    try:
        git(["revert", "--no-edit", rollback_commit_sha], repo_path=repo_path, timeout=600)
    except Exception as exc:  # noqa: BLE001 - operator-facing rollback errors must stay explicit
        return WorkerResponse(
            worker="rollback",
            success=False,
            summary="Git-Revert fuer den vorbereiteten Rollback ist fehlgeschlagen.",
            errors=[f"{type(exc).__name__}: {exc}"],
            outputs={
                "branch_name": branch_name,
                "rollback_commit_sha": rollback_commit_sha,
                "local_repo_path": str(repo_path),
                "backend": "git_revert",
            },
        )

    diff = current_diff(repo_path, request.base_branch)
    risk_flags = detect_risk_flags(diff["changed_files"], diff["diff_text"])
    report = {
        "summary": f"Git-Revert fuer Commit {rollback_commit_sha} wurde vorbereitet.",
        "branch_name": branch_name,
        "changed_files": diff["changed_files"],
        "diff_stat": diff["diff_stat"],
        "rollback_commit_sha": rollback_commit_sha,
        "backend": "git_revert",
    }
    report_path = write_report(settings.task_report_dir(request.task_id), "rollback-report.json", report)

    if not diff["changed_files"]:
        return WorkerResponse(
            worker="rollback",
            success=False,
            summary="Der Git-Revert hat keine sichtbaren Aenderungen erzeugt.",
            errors=[
                "Der angeforderte Commit ist moeglicherweise bereits revertiert oder fuehrt im aktuellen Branch zu keinem Diff."
            ],
            outputs=report,
            artifacts=[
                Artifact(
                    name="rollback-report",
                    path=str(report_path),
                    description="Deterministischer Git-Revert ohne resultierenden Diff.",
                )
            ],
        )

    return WorkerResponse(
        worker="rollback",
        summary=f"Rollback-Aenderung fuer Commit {rollback_commit_sha} wurde vorbereitet.",
        outputs={**report, "local_repo_path": str(repo_path)},
        risk_flags=risk_flags,
        artifacts=[
            Artifact(
                name="rollback-report",
                path=str(report_path),
                description="Deterministischer Git-Revert fuer Self-Improvement-Rollback.",
            )
        ],
    )


async def _monitor_self_update(
    request: SelfUpdateWatchdogStartRequest,
    initial_state: SelfUpdateWatchdogState,
) -> None:
    """Poll host health and trigger a host-side rollback when the self-update does not recover."""

    poll_seconds = max(3.0, request.poll_seconds or settings.self_update_watchdog_poll_seconds)
    timeout_seconds = max(poll_seconds * 3, request.timeout_seconds or settings.self_update_watchdog_timeout_seconds)
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    success_streak = 0
    state = initial_state.model_copy()
    state.status = SelfUpdateWatchdogStatus.MONITORING
    state.notes.append("Watchdog monitoring started.")
    write_watchdog_state(state, settings)

    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(poll_seconds)
        state.heartbeat_count += 1
        state.updated_at = datetime.now(UTC)
        state.last_heartbeat_at = state.updated_at
        state.current_sha = _read_remote_head(request)
        if state.current_sha and state.previous_sha and state.current_sha != state.previous_sha:
            state.observed_target_change = True

        health_ok = await _healthcheck_ok(request.health_url)
        if health_ok and state.observed_target_change:
            success_streak += 1
            if success_streak >= 2:
                state.status = SelfUpdateWatchdogStatus.HEALTHY
                state.last_error = None
                state.notes.append("Target change observed and healthcheck recovered.")
                write_watchdog_state(state, settings)
                return
        else:
            success_streak = 0

        write_watchdog_state(state, settings)

    state.status = SelfUpdateWatchdogStatus.TIMED_OUT
    state.last_error = (
        f"Self-update blieb laenger als {timeout_seconds:.0f}s ohne bestaetigte gesunde Rueckkehr."
    )
    state.notes.append("Timeout reached; host rollback will be attempted.")
    write_watchdog_state(state, settings)
    await _run_host_rollback(request, state)


async def _run_host_rollback(
    request: SelfUpdateWatchdogStartRequest,
    state: SelfUpdateWatchdogState,
) -> None:
    """Rollback the remote self-host stack to the last known stable commit."""

    if not state.previous_sha:
        state.status = SelfUpdateWatchdogStatus.ROLLBACK_FAILED
        state.last_error = "Kein vorheriger Commit bekannt; automatischer Host-Rollback ist nicht moeglich."
        state.updated_at = datetime.now(UTC)
        state.last_heartbeat_at = state.updated_at
        state.notes.append("Rollback skipped because no previous SHA was available.")
        write_watchdog_state(state, settings)
        return

    state.status = SelfUpdateWatchdogStatus.ROLLBACK_RUNNING
    state.updated_at = datetime.now(UTC)
    state.last_heartbeat_at = state.updated_at
    state.notes.append(f"Host rollback to {state.previous_sha} started.")
    write_watchdog_state(state, settings)

    script_path = Path("/app/scripts/unraid/rollback-self-update.sh")
    command = [
        "/bin/sh",
        str(script_path),
        request.project_dir,
        request.compose_file,
        state.previous_sha,
        request.ssh_user,
        request.ssh_host,
        str(request.ssh_port),
        request.health_url,
        request.ssh_key_file,
    ]

    try:
        await asyncio.to_thread(run_command, command, timeout=int(max(300.0, settings.worker_stage_timeout_seconds)))
    except CommandError as exc:
        state.status = SelfUpdateWatchdogStatus.ROLLBACK_FAILED
        state.last_error = str(exc)
        state.notes.append("Host rollback script failed.")
    else:
        state.status = SelfUpdateWatchdogStatus.ROLLED_BACK
        state.last_error = None
        state.notes.append("Host rollback completed successfully.")

    state.updated_at = datetime.now(UTC)
    state.last_heartbeat_at = state.updated_at
    write_watchdog_state(state, settings)


def _read_remote_head(request: SelfUpdateWatchdogStartRequest) -> str | None:
    """Read the current remote HEAD commit without failing the entire watchdog on transient SSH issues."""

    ssh_command = _ssh_command(
        ssh_user=request.ssh_user,
        ssh_host=request.ssh_host,
        ssh_port=request.ssh_port,
        ssh_key_file=request.ssh_key_file,
        remote_command=f"git -C '{request.project_dir}' rev-parse HEAD",
    )
    try:
        completed = run_command(ssh_command, timeout=60)
    except CommandError:
        return None
    value = completed.stdout.strip()
    return value or None


async def _healthcheck_ok(health_url: str) -> bool:
    """Return True when the configured self-host healthcheck answers with HTTP 200."""

    if not health_url.strip():
        return False
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(health_url)
        return response.status_code == 200
    except httpx.HTTPError:
        return False


def _ssh_command(
    *,
    ssh_user: str,
    ssh_host: str,
    ssh_port: int,
    ssh_key_file: str,
    remote_command: str,
) -> list[str]:
    """Build one deterministic SSH invocation with optional key file."""

    command = [
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "BatchMode=yes",
        "-p",
        str(ssh_port),
    ]
    if ssh_key_file.strip():
        command.extend(["-i", ssh_key_file.strip()])
    command.append(f"{ssh_user}@{ssh_host}")
    command.extend(["/bin/sh", "-lc", remote_command])
    return command
