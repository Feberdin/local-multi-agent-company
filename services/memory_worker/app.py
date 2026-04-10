"""
Purpose: Memory worker for persisting decisions, residual risks, and worker learnings as task memory artifacts.
Input/Output: Consumes upstream worker outputs and writes a durable task memory record for later reuse and debugging.
Important invariants: Memory entries must capture decisions and uncertainties without exposing secrets or raw sensitive content.
How to debug: If future tasks miss relevant context, inspect the generated memory file and the decisions it recorded.
"""

from __future__ import annotations

from fastapi import FastAPI

from services.shared.agentic_lab.config import get_settings
from services.shared.agentic_lab.logging_utils import TaskLoggerAdapter, configure_logging
from services.shared.agentic_lab.repo_tools import write_report
from services.shared.agentic_lab.schemas import Artifact, HealthResponse, WorkerRequest, WorkerResponse

settings = get_settings()
logger = configure_logging(settings.service_name, settings.log_level)
app = FastAPI(title="Feberdin Memory Worker", version="0.1.0")


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(service="memory-worker")


@app.post("/run", response_model=WorkerResponse)
async def run(request: WorkerRequest) -> WorkerResponse:
    task_logger = TaskLoggerAdapter(logger.logger, {"service": "memory-worker", "task_id": request.task_id})
    memory_dir = settings.data_dir / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)

    memory_entry = {
        "task_id": request.task_id,
        "goal": request.goal,
        "repository": request.repository,
        "branch_name": request.branch_name,
        "requirements": request.prior_results.get("requirements", {}).get("outputs", {}),
        "architecture": request.prior_results.get("architecture", {}).get("outputs", {}),
        "security": request.prior_results.get("security", {}).get("outputs", {}),
        "validation": request.prior_results.get("validation", {}).get("outputs", {}),
    }
    report_path = write_report(settings.task_report_dir(request.task_id), "memory-entry.json", memory_entry)
    durable_path = write_report(memory_dir, f"{request.task_id}.json", memory_entry)
    task_logger.info("Task memory written to %s", durable_path)

    return WorkerResponse(
        worker="memory",
        summary="Task memory updated.",
        outputs={"memory_path": str(durable_path)},
        artifacts=[
            Artifact(
                name="memory-entry",
                path=str(report_path),
                description="Task-specific memory artifact stored with the reports.",
            ),
            Artifact(
                name="durable-memory-entry",
                path=str(durable_path),
                description="Longer-lived task memory stored under the shared data directory.",
            ),
        ],
    )
