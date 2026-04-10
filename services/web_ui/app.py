"""
Purpose: Minimal dashboard and approval UI for task submission, status visibility, and manual gates.
Input/Output: Operators use simple HTML forms and views backed by the orchestrator API.
Important invariants: The UI is read-mostly, approval actions are explicit, and it never bypasses orchestrator state management.
How to debug: If buttons stop working, inspect the orchestrator base URL and the API responses fetched by this service.
"""

from __future__ import annotations

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from services.shared.agentic_lab.config import get_settings
from services.shared.agentic_lab.logging_utils import configure_logging
from services.shared.agentic_lab.schemas import HealthResponse

settings = get_settings()
logger = configure_logging(settings.service_name, settings.log_level)
app = FastAPI(title="Feberdin Agent Team Dashboard", version="0.1.0")
app.mount("/static", StaticFiles(directory="/app/services/web_ui/static"), name="static")
templates = Jinja2Templates(directory="/app/services/web_ui/templates")


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(service="web-ui")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(f"{settings.orchestrator_internal_url.rstrip('/')}/api/tasks")
        response.raise_for_status()
        tasks = response.json()
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"request": request, "tasks": tasks},
    )


@app.get("/tasks/{task_id}", response_class=HTMLResponse)
async def task_detail(request: Request, task_id: str) -> HTMLResponse:
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(f"{settings.orchestrator_internal_url.rstrip('/')}/api/tasks/{task_id}")
        response.raise_for_status()
        task = response.json()
    return templates.TemplateResponse(
        request=request,
        name="task.html",
        context={"request": request, "task": task},
    )


@app.post("/tasks", response_class=HTMLResponse)
async def create_task(
    request: Request,
    goal: str = Form(...),
    repository: str = Form(...),
    local_repo_path: str = Form(...),
) -> RedirectResponse:
    payload = {"goal": goal, "repository": repository, "local_repo_path": local_repo_path}
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(f"{settings.orchestrator_internal_url.rstrip('/')}/api/tasks", json=payload)
        response.raise_for_status()
        task = response.json()
    return RedirectResponse(url=f"/tasks/{task['id']}", status_code=303)


@app.post("/tasks/{task_id}/run")
async def run_task(task_id: str) -> RedirectResponse:
    async with httpx.AsyncClient(timeout=20) as client:
        await client.post(f"{settings.orchestrator_internal_url.rstrip('/')}/api/tasks/{task_id}/run")
    return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)


@app.post("/tasks/{task_id}/approve")
async def approve_task(task_id: str, gate_name: str = Form("risk-review")) -> RedirectResponse:
    payload = {"gate_name": gate_name, "decision": "APPROVE", "actor": "dashboard"}
    async with httpx.AsyncClient(timeout=20) as client:
        await client.post(
            f"{settings.orchestrator_internal_url.rstrip('/')}/api/tasks/{task_id}/approvals",
            json=payload,
        )
    return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)


@app.post("/tasks/{task_id}/reject")
async def reject_task(
    task_id: str,
    gate_name: str = Form("risk-review"),
    reason: str = Form("Rejected from dashboard review."),
) -> RedirectResponse:
    payload = {"gate_name": gate_name, "decision": "REJECT", "actor": "dashboard", "reason": reason}
    async with httpx.AsyncClient(timeout=20) as client:
        await client.post(
            f"{settings.orchestrator_internal_url.rstrip('/')}/api/tasks/{task_id}/approvals",
            json=payload,
        )
    return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)
