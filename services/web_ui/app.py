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


async def _load_dashboard_context(error_message: str | None = None) -> dict:
    """Collect tasks and repository settings for the dashboard."""

    async with httpx.AsyncClient(timeout=20) as client:
        tasks_response = await client.get(f"{settings.orchestrator_internal_url.rstrip('/')}/api/tasks")
        tasks_response.raise_for_status()
        repo_settings_response = await client.get(
            f"{settings.orchestrator_internal_url.rstrip('/')}/api/settings/repository-access"
        )
        repo_settings_response.raise_for_status()
        repo_settings = repo_settings_response.json()

    return {
        "tasks": tasks_response.json(),
        "repository_access_settings": repo_settings,
        "allowed_repositories_text": "\n".join(repo_settings.get("allowed_repositories", [])),
        "error_message": error_message,
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    context = await _load_dashboard_context()
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"request": request, **context},
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
    allow_repository_modifications: bool = Form(False),
) -> HTMLResponse | RedirectResponse:
    payload = {
        "goal": goal,
        "repository": repository,
        "local_repo_path": local_repo_path,
        "allow_repository_modifications": allow_repository_modifications,
    }
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(f"{settings.orchestrator_internal_url.rstrip('/')}/api/tasks", json=payload)
        if response.status_code >= 400:
            detail = response.json().get("detail", "Die Aufgabe konnte nicht angelegt werden.")
            context = await _load_dashboard_context(error_message=detail)
            return templates.TemplateResponse(request=request, name="index.html", context={"request": request, **context})
        task = response.json()
    return RedirectResponse(url=f"/tasks/{task['id']}", status_code=303)


@app.post("/settings/repositories", response_class=HTMLResponse)
async def update_repository_settings(
    request: Request,
    allowed_repositories_text: str = Form(""),
) -> HTMLResponse | RedirectResponse:
    repositories = [line.strip() for line in allowed_repositories_text.splitlines() if line.strip()]
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.put(
            f"{settings.orchestrator_internal_url.rstrip('/')}/api/settings/repository-access",
            json={"allowed_repositories": repositories},
        )
        if response.status_code >= 400:
            detail = response.json().get("detail", "Die Einstellungen konnten nicht gespeichert werden.")
            context = await _load_dashboard_context(error_message=detail)
            return templates.TemplateResponse(request=request, name="index.html", context={"request": request, **context})
    return RedirectResponse(url="/", status_code=303)


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
