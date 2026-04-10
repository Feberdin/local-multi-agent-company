"""
Purpose: FastAPI entrypoint for the orchestrator API, task lifecycle, and approval endpoints.
Input/Output: Operators, scripts, and the web UI call this service to create tasks, inspect status, and resume workflows.
Important invariants: Only the orchestrator mutates task state, and background runs are scheduled explicitly per task.
How to debug: If the UI cannot create or resume tasks, inspect the request/response payloads exposed by this API.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from services.orchestrator.workflow import WorkflowOrchestrator
from services.shared.agentic_lab.config import get_settings
from services.shared.agentic_lab.db import init_db
from services.shared.agentic_lab.logging_utils import configure_logging
from services.shared.agentic_lab.schemas import (
    ApprovalRequest,
    HealthResponse,
    TaskCreateRequest,
    TaskDetail,
    TaskSummary,
)
from services.shared.agentic_lab.task_service import TaskService

settings = get_settings()
logger = configure_logging(settings.service_name, settings.log_level)
task_service = TaskService()
workflow = WorkflowOrchestrator(settings=settings, task_service=task_service)

@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Initialize persistence once per process using FastAPI's lifespan hook."""
    init_db()
    logger.info("Orchestrator startup completed.")
    yield


app = FastAPI(title="Feberdin Agent Team Orchestrator", version="0.1.0", lifespan=lifespan)
app.state.running_tasks = set()


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(service="orchestrator")


@app.get("/api/tasks", response_model=list[TaskSummary])
async def list_tasks() -> list[TaskSummary]:
    return task_service.list_tasks()


@app.get("/api/tasks/{task_id}", response_model=TaskDetail)
async def get_task(task_id: str) -> TaskDetail:
    try:
        return task_service.get_task(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/tasks", response_model=TaskSummary, status_code=201)
async def create_task(request: TaskCreateRequest) -> TaskSummary:
    summary = task_service.create_task(request)
    return task_service.get_task(summary.id)


@app.post("/api/tasks/{task_id}/run", response_model=TaskDetail)
async def run_task(task_id: str) -> TaskDetail:
    try:
        task_service.get_task(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if task_id not in app.state.running_tasks:
        app.state.running_tasks.add(task_id)
        asyncio.create_task(_run_in_background(task_id))
    return task_service.get_task(task_id)


@app.post("/api/tasks/{task_id}/approvals", response_model=TaskDetail)
async def record_approval(task_id: str, request: ApprovalRequest) -> TaskDetail:
    try:
        updated = task_service.record_approval(task_id, request)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if request.decision.value == "APPROVE" and task_id not in app.state.running_tasks:
        app.state.running_tasks.add(task_id)
        asyncio.create_task(_run_in_background(task_id))
    return updated

async def _run_in_background(task_id: str) -> None:
    """Execute the workflow and always release the in-memory run lock."""

    try:
        await workflow.run_task(task_id)
    finally:
        app.state.running_tasks.discard(task_id)
