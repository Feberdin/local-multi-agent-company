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
from services.shared.agentic_lab.policy_service import RepositoryPolicyError, RepositoryPolicyService
from services.shared.agentic_lab.schemas import (
    ApprovalRequest,
    HealthResponse,
    ImprovementSuggestion,
    ImprovementSuggestionDecisionRequest,
    ImprovementSuggestionRegistry,
    ImprovementSuggestionStatus,
    RepositoryAccessSettings,
    SearchProvider,
    SearchProviderSettings,
    SearchProviderTestRequest,
    SearchProviderTestResult,
    SourceRoutingDecision,
    SourceRoutingRequest,
    SourceTestRequest,
    SourceTestResult,
    TaskCreateRequest,
    TaskDetail,
    TaskSummary,
    TrustedSource,
    TrustedSourceImportPayload,
    TrustedSourceProfileSelection,
    TrustedSourceRegistry,
    WorkerGuidancePolicy,
    WorkerGuidanceRegistry,
)
from services.shared.agentic_lab.search_providers import SearchProviderError, SearchProviderService
from services.shared.agentic_lab.source_router import SourceRouter
from services.shared.agentic_lab.task_service import TaskService
from services.shared.agentic_lab.trusted_sources import TrustedSourceError, TrustedSourceService
from services.shared.agentic_lab.worker_governance import WorkerGovernanceError, WorkerGovernanceService

settings = get_settings()
logger = configure_logging(settings.service_name, settings.log_level)
task_service = TaskService()
policy_service = RepositoryPolicyService(settings)
trusted_source_service = TrustedSourceService(settings)
search_provider_service = SearchProviderService(settings)
worker_governance_service = WorkerGovernanceService(settings)
source_router = SourceRouter(trusted_source_service, search_provider_service)
workflow = WorkflowOrchestrator(
    settings=settings,
    task_service=task_service,
    policy_service=policy_service,
    worker_governance_service=worker_governance_service,
)


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
    try:
        policy_service.assert_repository_allowed(request.repository)
    except RepositoryPolicyError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    summary = task_service.create_task(request)
    return task_service.get_task(summary.id)


@app.post("/api/tasks/{task_id}/run", response_model=TaskDetail)
async def run_task(task_id: str) -> TaskDetail:
    try:
        task = task_service.get_task(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    try:
        policy_service.assert_repository_allowed(task.repository)
    except RepositoryPolicyError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

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


@app.get("/api/suggestions", response_model=list[ImprovementSuggestion])
async def list_improvement_suggestions(
    status: ImprovementSuggestionStatus | None = None,
    task_id: str | None = None,
) -> list[ImprovementSuggestion]:
    return worker_governance_service.list_suggestions(status=status, task_id=task_id)


@app.get("/api/suggestions/registry", response_model=ImprovementSuggestionRegistry)
async def get_improvement_suggestion_registry() -> ImprovementSuggestionRegistry:
    return worker_governance_service.load_suggestion_registry()


@app.post("/api/suggestions/{suggestion_id}/decision", response_model=ImprovementSuggestionRegistry)
async def decide_improvement_suggestion(
    suggestion_id: str,
    request: ImprovementSuggestionDecisionRequest,
) -> ImprovementSuggestionRegistry:
    try:
        return worker_governance_service.decide_suggestion(suggestion_id, request)
    except WorkerGovernanceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/settings/repository-access", response_model=RepositoryAccessSettings)
async def get_repository_access_settings() -> RepositoryAccessSettings:
    return policy_service.load()


@app.put("/api/settings/repository-access", response_model=RepositoryAccessSettings)
async def update_repository_access_settings(payload: RepositoryAccessSettings) -> RepositoryAccessSettings:
    try:
        return policy_service.save(payload.allowed_repositories)
    except RepositoryPolicyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/settings/worker-guidance", response_model=WorkerGuidanceRegistry)
async def get_worker_guidance_settings() -> WorkerGuidanceRegistry:
    return worker_governance_service.load_guidance_registry()


@app.put("/api/settings/worker-guidance", response_model=WorkerGuidanceRegistry)
async def update_worker_guidance_settings(payload: WorkerGuidanceRegistry) -> WorkerGuidanceRegistry:
    try:
        return worker_governance_service.save_guidance_registry(payload)
    except WorkerGovernanceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put("/api/settings/worker-guidance/{worker_name}", response_model=WorkerGuidanceRegistry)
async def update_single_worker_guidance(worker_name: str, payload: WorkerGuidancePolicy) -> WorkerGuidanceRegistry:
    try:
        return worker_governance_service.upsert_guidance(payload.model_copy(update={"worker_name": worker_name}))
    except WorkerGovernanceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/settings/trusted-sources", response_model=TrustedSourceRegistry)
async def get_trusted_sources_settings() -> TrustedSourceRegistry:
    return trusted_source_service.load_registry()


@app.put("/api/settings/trusted-sources", response_model=TrustedSourceRegistry)
async def update_trusted_sources_settings(payload: TrustedSourceRegistry) -> TrustedSourceRegistry:
    try:
        return trusted_source_service.save_registry(payload)
    except TrustedSourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/settings/trusted-sources/active-profile", response_model=TrustedSourceRegistry)
async def set_active_trusted_source_profile(payload: TrustedSourceProfileSelection) -> TrustedSourceRegistry:
    try:
        return trusted_source_service.set_active_profile(payload.profile_id)
    except TrustedSourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/settings/trusted-sources/import", response_model=TrustedSourceRegistry)
async def import_trusted_sources(payload: TrustedSourceImportPayload) -> TrustedSourceRegistry:
    try:
        return trusted_source_service.import_payload(payload)
    except TrustedSourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/settings/trusted-sources/export")
async def export_trusted_sources() -> dict[str, str]:
    return {"payload_json": trusted_source_service.export_registry_json()}


@app.post("/api/settings/trusted-sources/sources", response_model=TrustedSourceRegistry)
async def create_trusted_source(payload: TrustedSource) -> TrustedSourceRegistry:
    try:
        trusted_source_service.upsert_source(payload)
        return trusted_source_service.load_registry()
    except TrustedSourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put("/api/settings/trusted-sources/sources/{source_id}", response_model=TrustedSourceRegistry)
async def update_trusted_source(source_id: str, payload: TrustedSource) -> TrustedSourceRegistry:
    try:
        trusted_source_service.upsert_source(payload.model_copy(update={"id": source_id}))
        return trusted_source_service.load_registry()
    except TrustedSourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/settings/trusted-sources/sources/{source_id}", response_model=TrustedSourceRegistry)
async def delete_trusted_source(source_id: str) -> TrustedSourceRegistry:
    try:
        trusted_source_service.delete_source(source_id)
        return trusted_source_service.load_registry()
    except TrustedSourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/settings/trusted-sources/dry-run", response_model=SourceRoutingDecision)
async def dry_run_trusted_sources(payload: SourceRoutingRequest) -> SourceRoutingDecision:
    return source_router.route(payload)


@app.post("/api/settings/trusted-sources/test", response_model=SourceTestResult)
async def test_trusted_source(payload: SourceTestRequest) -> SourceTestResult:
    try:
        return await trusted_source_service.test_source(payload.source_id, payload.query)
    except TrustedSourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/settings/web-search", response_model=SearchProviderSettings)
async def get_web_search_settings() -> SearchProviderSettings:
    return search_provider_service.load_settings()


@app.put("/api/settings/web-search", response_model=SearchProviderSettings)
async def update_web_search_settings(payload: SearchProviderSettings) -> SearchProviderSettings:
    try:
        return search_provider_service.save_settings(payload)
    except SearchProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/settings/web-search/providers", response_model=SearchProviderSettings)
async def create_web_search_provider(payload: SearchProvider) -> SearchProviderSettings:
    try:
        return search_provider_service.upsert_provider(payload)
    except SearchProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put("/api/settings/web-search/providers/{provider_id}", response_model=SearchProviderSettings)
async def update_web_search_provider(provider_id: str, payload: SearchProvider) -> SearchProviderSettings:
    try:
        return search_provider_service.upsert_provider(payload.model_copy(update={"id": provider_id}))
    except SearchProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/settings/web-search/providers/{provider_id}", response_model=SearchProviderSettings)
async def delete_web_search_provider(provider_id: str) -> SearchProviderSettings:
    try:
        return search_provider_service.delete_provider(provider_id)
    except SearchProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/settings/web-search/test", response_model=SearchProviderTestResult)
async def test_web_search_provider(payload: SearchProviderTestRequest) -> SearchProviderTestResult:
    try:
        return await search_provider_service.test_provider(
            payload,
            trusted_source_service,
            trusted_source_service.load_active_profile(),
        )
    except (SearchProviderError, TrustedSourceError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/settings/web-search/health/{provider_id}", response_model=SearchProviderTestResult)
async def health_check_web_search_provider(provider_id: str) -> SearchProviderTestResult:
    try:
        return await search_provider_service.health_check(provider_id)
    except SearchProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _run_in_background(task_id: str) -> None:
    """Execute the workflow and always release the in-memory run lock."""

    try:
        await workflow.run_task(task_id)
    finally:
        app.state.running_tasks.discard(task_id)
