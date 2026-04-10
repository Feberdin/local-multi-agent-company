"""
Purpose: Minimal dashboard and operations UI for tasks, trusted sources, and web-search provider settings.
Input/Output: Operators use HTML forms backed by the orchestrator API to manage workflow and research guardrails.
Important invariants: The UI is read-mostly, approval actions remain explicit, and it never bypasses orchestrator state management.
How to debug: If a form stops working, inspect the orchestrator base URL, the called endpoint, and the returned JSON error detail.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from services.shared.agentic_lab.config import get_settings
from services.shared.agentic_lab.logging_utils import configure_logging
from services.shared.agentic_lab.schemas import HealthResponse

settings = get_settings()
logger = configure_logging(settings.service_name, settings.log_level)
app = FastAPI(title="Feberdin Agent Team Dashboard", version="0.1.0")

# Why this exists:
# The UI should import both inside the container (`/app/...`) and in local tests where the
# repository lives in a normal workspace path. Resolving from this file keeps the setup portable.
WEB_UI_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=str(WEB_UI_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(WEB_UI_DIR / "templates"))


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
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.request(
            method,
            f"{settings.orchestrator_internal_url.rstrip('/')}{path}",
            json=json_payload,
        )
        return response


async def _load_dashboard_context(error_message: str | None = None, success_message: str | None = None) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=20) as client:
        tasks_response = await client.get(f"{settings.orchestrator_internal_url.rstrip('/')}/api/tasks")
        tasks_response.raise_for_status()
        repo_settings_response = await client.get(
            f"{settings.orchestrator_internal_url.rstrip('/')}/api/settings/repository-access"
        )
        repo_settings_response.raise_for_status()
        repo_settings = repo_settings_response.json()
        suggestions_response = await client.get(
            f"{settings.orchestrator_internal_url.rstrip('/')}/api/suggestions",
            params={"status": "pending"},
        )
        suggestions_response.raise_for_status()
        pending_suggestions = suggestions_response.json()

    return {
        "tasks": tasks_response.json(),
        "repository_access_settings": repo_settings,
        "allowed_repositories_text": "\n".join(repo_settings.get("allowed_repositories", [])),
        "pending_suggestions_count": len(pending_suggestions),
        "error_message": error_message,
        "success_message": success_message,
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
    registry_response.raise_for_status()
    registry = registry_response.json()
    profiles = registry.get("profiles", [])
    active_profile_id = registry.get("active_profile_id")
    active_profile = next((profile for profile in profiles if profile["id"] == active_profile_id), None)
    sources = active_profile.get("sources", []) if active_profile else []
    edit_source = next((source for source in sources if source["id"] == edit_source_id), None)

    form_values = _default_source_form_values()
    if edit_source:
        form_values.update(edit_source)
        form_values["allowed_paths_text"] = "\n".join(edit_source.get("allowed_paths", []))
        form_values["deny_paths_text"] = "\n".join(edit_source.get("deny_paths", []))
        form_values["tags_text"] = "\n".join(edit_source.get("tags", []))

    return {
        "registry": registry,
        "profiles": profiles,
        "active_profile": active_profile,
        "sources": sources,
        "source_form_values": form_values,
        "error_message": error_message,
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
    settings_response.raise_for_status()
    provider_settings = settings_response.json()
    providers = provider_settings.get("providers", [])
    edit_provider = next((provider for provider in providers if provider["id"] == edit_provider_id), None)

    form_values = _default_provider_form_values()
    if edit_provider:
        form_values.update(edit_provider)
        form_values["default_categories_text"] = "\n".join(edit_provider.get("default_categories", []))

    return {
        "web_search_settings": provider_settings,
        "providers": providers,
        "provider_form_values": form_values,
        "error_message": error_message,
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
    registry_response.raise_for_status()
    registry = registry_response.json()
    workers = registry.get("workers", [])
    if edit_worker_name is None and workers:
        edit_worker_name = workers[0]["worker_name"]
    edit_worker = next((worker for worker in workers if worker["worker_name"] == edit_worker_name), None)

    form_values = _default_worker_guidance_form_values()
    if edit_worker:
        form_values.update(edit_worker)
        form_values["operator_recommendations_text"] = "\n".join(edit_worker.get("operator_recommendations", []))
        form_values["decision_preferences_text"] = "\n".join(edit_worker.get("decision_preferences", []))

    return {
        "worker_guidance_registry": registry,
        "workers": workers,
        "worker_guidance_form_values": form_values,
        "error_message": error_message,
        "success_message": success_message,
    }


async def _load_suggestions_context(
    *,
    task_id: str | None = None,
    error_message: str | None = None,
    success_message: str | None = None,
) -> dict[str, Any]:
    suggestions_response = await _api_request("GET", "/api/suggestions/registry")
    suggestions_response.raise_for_status()
    registry = suggestions_response.json()
    suggestions = registry.get("suggestions", [])
    if task_id is not None:
        suggestions = [item for item in suggestions if item.get("task_id") == task_id]

    pending = [item for item in suggestions if item.get("status") == "pending"]
    approved = [item for item in suggestions if item.get("status") == "approved"]
    rejected = [item for item in suggestions if item.get("status") == "rejected"]

    return {
        "suggestion_registry": registry,
        "suggestions": suggestions,
        "pending_suggestions": pending,
        "approved_suggestions": approved,
        "rejected_suggestions": rejected,
        "error_message": error_message,
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


@app.get("/tasks/{task_id}", response_class=HTMLResponse)
async def task_detail(request: Request, task_id: str) -> HTMLResponse:
    response = await _api_request("GET", f"/api/tasks/{task_id}")
    response.raise_for_status()
    task = response.json()
    suggestion_context = await _load_suggestions_context(task_id=task_id)
    return templates.TemplateResponse(
        request=request,
        name="task.html",
        context={"request": request, "task": task, **suggestion_context},
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
        detail = response.json().get("detail", "Die Aufgabe konnte nicht angelegt werden.")
        context = await _load_dashboard_context(error_message=detail)
        return templates.TemplateResponse(request=request, name="index.html", context={"request": request, **context})
    task = response.json()
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
        detail = response.json().get("detail", "Die Einstellungen konnten nicht gespeichert werden.")
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
        detail = response.json().get("detail", "Die Worker-Guidance konnte nicht gespeichert werden.")
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
        detail = response.json().get("detail", "Das Profil konnte nicht gewechselt werden.")
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
        detail = response.json().get("detail", "Die Quelle konnte nicht gespeichert werden.")
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
        detail = response.json().get("detail", "Der Status der Quelle konnte nicht geändert werden.")
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
        detail = response.json().get("detail", "Die Quelle konnte nicht gelöscht werden.")
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
        detail = response.json().get("detail", "Der Import konnte nicht verarbeitet werden.")
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
        detail = response.json().get("detail", "Dry-Run konnte nicht ausgeführt werden.")
        context = await _load_trusted_sources_context(error_message=detail)
        return templates.TemplateResponse(request=request, name="trusted_sources.html", context={"request": request, **context})
    context = await _load_trusted_sources_context(dry_run_result=response.json())
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
        detail = response.json().get("detail", "Der Quellentest ist fehlgeschlagen.")
        context = await _load_trusted_sources_context(edit_source_id=source_id, error_message=detail)
        return templates.TemplateResponse(request=request, name="trusted_sources.html", context={"request": request, **context})
    context = await _load_trusted_sources_context(edit_source_id=source_id, source_test_result=response.json())
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
    current_response.raise_for_status()
    current = current_response.json()
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
        detail = response.json().get("detail", "Die Web-Search-Einstellungen konnten nicht gespeichert werden.")
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
        detail = response.json().get("detail", "Der Provider konnte nicht gespeichert werden.")
        context = await _load_web_search_context(edit_provider_id=provider_id or None, error_message=detail)
        context["provider_form_values"].update(payload)
        context["provider_form_values"]["default_categories_text"] = default_categories_text
        return templates.TemplateResponse(request=request, name="web_search.html", context={"request": request, **context})
    return RedirectResponse(url="/web-search", status_code=303)


@app.post("/settings/web-search/provider/{provider_id}/delete", response_class=HTMLResponse, response_model=None)
async def delete_web_search_provider(request: Request, provider_id: str) -> Response:
    response = await _api_request("DELETE", f"/api/settings/web-search/providers/{provider_id}")
    if response.status_code >= 400:
        detail = response.json().get("detail", "Der Provider konnte nicht gelöscht werden.")
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
        detail = response.json().get("detail", "Der Providertest ist fehlgeschlagen.")
        context = await _load_web_search_context(edit_provider_id=provider_id, error_message=detail)
        return templates.TemplateResponse(request=request, name="web_search.html", context={"request": request, **context})
    context = await _load_web_search_context(edit_provider_id=provider_id, provider_test_result=response.json())
    return templates.TemplateResponse(request=request, name="web_search.html", context={"request": request, **context})


@app.post("/settings/web-search/provider/{provider_id}/health", response_class=HTMLResponse)
async def health_check_web_search_provider(request: Request, provider_id: str) -> HTMLResponse:
    response = await _api_request("POST", f"/api/settings/web-search/health/{provider_id}")
    if response.status_code >= 400:
        detail = response.json().get("detail", "Der Health-Check ist fehlgeschlagen.")
        context = await _load_web_search_context(edit_provider_id=provider_id, error_message=detail)
        return templates.TemplateResponse(request=request, name="web_search.html", context={"request": request, **context})
    context = await _load_web_search_context(edit_provider_id=provider_id, provider_test_result=response.json())
    return templates.TemplateResponse(request=request, name="web_search.html", context={"request": request, **context})


@app.post("/tasks/{task_id}/run")
async def run_task(task_id: str) -> RedirectResponse:
    await _api_request("POST", f"/api/tasks/{task_id}/run")
    return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)


@app.post("/tasks/{task_id}/approve")
async def approve_task(task_id: str, gate_name: str = Form("risk-review")) -> RedirectResponse:
    payload = {"gate_name": gate_name, "decision": "APPROVE", "actor": "dashboard"}
    await _api_request("POST", f"/api/tasks/{task_id}/approvals", json_payload=payload)
    return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)


@app.post("/tasks/{task_id}/reject")
async def reject_task(
    task_id: str,
    gate_name: str = Form("risk-review"),
    reason: str = Form("Rejected from dashboard review."),
) -> RedirectResponse:
    payload = {"gate_name": gate_name, "decision": "REJECT", "actor": "dashboard", "reason": reason}
    await _api_request("POST", f"/api/tasks/{task_id}/approvals", json_payload=payload)
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
        detail = response.json().get("detail", "Die Anregung konnte nicht freigegeben werden.")
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
        detail = response.json().get("detail", "Die Anregung konnte nicht abgelehnt werden.")
        context = await _load_suggestions_context(task_id=task_id or None, error_message=detail)
        return templates.TemplateResponse(
            request=request,
            name="suggestions.html",
            context={"request": request, **context},
        )
    target = f"/tasks/{task_id}" if task_id else "/suggestions"
    return RedirectResponse(url=target, status_code=303)
