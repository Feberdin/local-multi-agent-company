"""
Purpose: Deploy worker for controlled Unraid staging deployments through an auditable shell wrapper.
Input/Output: Receives deployment settings and returns deployment logs plus the target healthcheck URL.
Important invariants: Only staging deployment is allowed automatically; production deployment is out of scope for this worker.
How to debug: If deployment fails, inspect the invoked script, SSH connectivity, and the returned stderr captured here.
"""

from __future__ import annotations

from pathlib import Path

import httpx
from fastapi import FastAPI
from pydantic import BaseModel

from services.shared.agentic_lab.config import get_settings
from services.shared.agentic_lab.logging_utils import TaskLoggerAdapter, configure_logging
from services.shared.agentic_lab.repo_tools import CommandError, run_command, write_report
from services.shared.agentic_lab.schemas import Artifact, HealthResponse, WorkerRequest, WorkerResponse
from services.shared.agentic_lab.self_update_watchdog import (
    SelfUpdateWatchdogState,
    SelfUpdateWatchdogStatus,
    read_watchdog_state,
    write_watchdog_state,
)

settings = get_settings()
logger = configure_logging(settings.service_name, settings.log_level)
app = FastAPI(title="Feberdin Deploy Worker", version="0.1.0")


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


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(service="deploy-worker")


@app.post("/run", response_model=WorkerResponse)
async def run(request: WorkerRequest) -> WorkerResponse:
    task_logger = TaskLoggerAdapter(logger.logger, {"service": "deploy-worker", "task_id": request.task_id})
    deployment = request.deployment.model_dump() if request.deployment else {}
    project_dir = deployment.get("project_dir") or settings.staging_project_dir
    compose_file = deployment.get("compose_file") or settings.staging_compose_file
    healthcheck_url = deployment.get("healthcheck_url") or settings.staging_healthcheck_url
    branch_name = request.branch_name or settings.staging_git_branch

    if request.metadata.get("deployment_target") == "production":
        return WorkerResponse(
            worker="deploy",
            success=False,
            summary="Production deployment is intentionally blocked.",
            errors=["The deploy worker is restricted to staging targets only."],
            requires_human_approval=True,
            approval_reason="Production deployment requires a separate approved workflow.",
        )

    # Self-update mode: deploy the agent stack itself on the Unraid host.
    if request.metadata.get("deployment_target") == "self":
        return await _run_self_update(request, task_logger, branch_name)

    script_path = Path("/app/scripts/unraid/deploy-staging.sh")
    try:
        completed = run_command(
            [
                "/bin/sh",
                str(script_path),
                request.local_repo_path,
                project_dir,
                compose_file,
                branch_name,
                settings.staging_ssh_user,
                settings.staging_host,
                str(settings.staging_ssh_port),
            ],
            timeout=900,
        )
    except CommandError as exc:
        return WorkerResponse(
            worker="deploy",
            success=False,
            summary="Staging deployment failed.",
            errors=[str(exc)],
        )

    report = {
        "stdout": completed.stdout[-6000:],
        "stderr": completed.stderr[-6000:],
        "healthcheck_url": healthcheck_url,
    }
    report_path = write_report(settings.task_report_dir(request.task_id), "deploy-report.json", report)
    task_logger.info("Staging deployment script completed.")

    return WorkerResponse(
        worker="deploy",
        summary="Staging deployment completed.",
        outputs={"healthcheck_url": healthcheck_url, "project_dir": project_dir, "branch_name": branch_name},
        artifacts=[
            Artifact(
                name="deploy-report",
                path=str(report_path),
                description="Staging deployment stdout, stderr, and healthcheck target.",
            )
        ],
    )


async def _run_self_update(request, task_logger, branch_name: str) -> WorkerResponse:
    """Deploy the agent stack itself on the Unraid host via self-update.sh."""
    if not settings.self_host_ssh_host:
        return WorkerResponse(
            worker="deploy",
            success=False,
            summary="Self-update not configured.",
            errors=["SELF_HOST_SSH_HOST is not set. Configure the self-host settings in .env to enable autonomous self-update."],
        )
    if not settings.self_host_project_dir:
        return WorkerResponse(
            worker="deploy",
            success=False,
            summary="Self-update not configured.",
            errors=["SELF_HOST_PROJECT_DIR is not set. Set it to the feberdin repo path on the Unraid host."],
        )
    if not settings.self_host_health_url:
        return WorkerResponse(
            worker="deploy",
            success=False,
            summary="Self-update not configured.",
            errors=["SELF_HOST_HEALTH_URL is not set. The rollback worker needs a reachable health URL to supervise the rollout."],
        )

    try:
        watchdog_state = await _start_self_update_watchdog(request.task_id, branch_name)
    except httpx.HTTPError as exc:
        return WorkerResponse(
            worker="deploy",
            success=False,
            summary="Self-update watchdog konnte nicht gestartet werden.",
            errors=[f"Rollback-Worker unter {settings.rollback_worker_url} antwortet nicht stabil: {exc}"],
        )

    script_path = Path("/app/scripts/unraid/self-update.sh")
    task_logger.info("Self-update: deploying branch %s to %s", branch_name, settings.self_host_ssh_host)

    try:
        completed = run_command(
            [
                "/bin/sh",
                str(script_path),
                settings.self_host_project_dir,
                settings.self_host_compose_file,
                branch_name,
                settings.self_host_ssh_user,
                settings.self_host_ssh_host,
                str(settings.self_host_ssh_port),
                settings.self_host_health_url,
                settings.self_host_ssh_key_file,
                request.task_id,
            ],
            timeout=120,
        )
    except CommandError as exc:
        _mark_watchdog_dispatch_failed(request.task_id, str(exc))
        return WorkerResponse(
            worker="deploy",
            success=False,
            summary="Self-update deployment failed.",
            errors=[str(exc)],
        )

    report = {
        "stdout": completed.stdout[-6000:],
        "stderr": completed.stderr[-6000:],
        "branch": branch_name,
        "host": settings.self_host_ssh_host,
        "project_dir": settings.self_host_project_dir,
        "watchdog": watchdog_state.model_dump(mode="json"),
    }
    report_path = write_report(settings.task_report_dir(request.task_id), "deploy-report.json", report)
    task_logger.info("Self-update rollout dispatched on %s with rollback watchdog.", settings.self_host_ssh_host)

    return WorkerResponse(
        worker="deploy",
        summary=(
            f"Self-Update fuer {settings.self_host_ssh_host} wurde gestartet. "
            "Der rollback-worker ueberwacht Healthchecks und fuehrt bei Bedarf den Host-Rollback aus."
        ),
        outputs={
            "branch_name": branch_name,
            "host": settings.self_host_ssh_host,
            "project_dir": settings.self_host_project_dir,
            "health_url": settings.self_host_health_url,
            "watchdog_status": watchdog_state.status.value,
            "watchdog_previous_sha": watchdog_state.previous_sha,
            "watchdog_state_path": str(write_watchdog_state(watchdog_state, settings)),
        },
        artifacts=[
            Artifact(
                name="deploy-report",
                path=str(report_path),
                description="Self-update dispatch stdout, stderr, watchdog state, and host details.",
            )
        ],
    )


async def _start_self_update_watchdog(task_id: str, branch_name: str) -> SelfUpdateWatchdogState:
    """Arm the dedicated rollback worker before the self-update restarts the rest of the stack."""

    payload = SelfUpdateWatchdogStartRequest(
        task_id=task_id,
        branch_name=branch_name,
        ssh_user=settings.self_host_ssh_user,
        ssh_host=settings.self_host_ssh_host,
        ssh_port=settings.self_host_ssh_port,
        ssh_key_file=settings.self_host_ssh_key_file,
        project_dir=settings.self_host_project_dir,
        compose_file=settings.self_host_compose_file,
        health_url=settings.self_host_health_url,
        poll_seconds=settings.self_update_watchdog_poll_seconds,
        timeout_seconds=settings.self_update_watchdog_timeout_seconds,
    )
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(
            f"{settings.rollback_worker_url.rstrip('/')}/watchdogs/start",
            json=payload.model_dump(),
        )
        response.raise_for_status()
    return SelfUpdateWatchdogState.model_validate(response.json())


def _mark_watchdog_dispatch_failed(task_id: str, error_text: str) -> None:
    """Persist an explicit watchdog failure if the remote update could not even be dispatched."""

    state = read_watchdog_state(task_id, settings)
    if state is None:
        return
    state.status = SelfUpdateWatchdogStatus.DISPATCH_FAILED
    state.last_error = error_text
    state.notes.append("Self-update dispatch failed before the remote rollout could start.")
    write_watchdog_state(state, settings)
