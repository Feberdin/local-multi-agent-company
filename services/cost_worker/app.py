"""
Purpose: Cost and resource worker for estimating model usage, reasoning depth, and rough token budgets per task.
Input/Output: Uses configured model routes and task complexity heuristics to return a simple budget plan for the team.
Important invariants: Estimates must stay explainable and conservative rather than pretending to be exact billing data.
How to debug: If a worker seems routed to an unexpectedly strong or weak model, inspect the route summary produced here.
"""

from __future__ import annotations

from fastapi import FastAPI

from services.shared.agentic_lab.config import get_settings
from services.shared.agentic_lab.model_routing import get_model_routing
from services.shared.agentic_lab.repo_tools import write_report
from services.shared.agentic_lab.schemas import Artifact, HealthResponse, WorkerRequest, WorkerResponse

settings = get_settings()
app = FastAPI(title="Feberdin Cost Worker", version="0.1.0")


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(service="cost-worker")


@app.post("/run", response_model=WorkerResponse)
async def run(request: WorkerRequest) -> WorkerResponse:
    routing = get_model_routing(settings)
    route_summary = {
        worker_name: {
            "primary_provider": route.primary_provider,
            "fallback_provider": route.fallback_provider,
            "temperature": route.temperature,
            "max_tokens": route.max_tokens,
            "budget_tokens": route.budget_tokens,
            "reasoning": route.reasoning,
        }
        for worker_name, route in routing.workers.items()
    }
    recommended = "qwen-heavy" if len(request.goal) > 180 or "architecture" in request.goal.lower() else "mixed-routing"
    outputs = {
        "recommended_strategy": recommended,
        "route_summary": route_summary,
        "notes": [
            "Routine extraction and summarization can stay on Mistral.",
            "Architecture, security, review, and complex coding default to Qwen with Mistral fallback.",
        ],
    }
    report_path = write_report(settings.task_report_dir(request.task_id), "cost-plan.json", outputs)
    return WorkerResponse(
        worker="cost",
        summary="Model and token budget estimate prepared.",
        outputs=outputs,
        artifacts=[
            Artifact(
                name="cost-plan",
                path=str(report_path),
                description="Model routing summary and rough resource budget guidance.",
            )
        ],
    )
