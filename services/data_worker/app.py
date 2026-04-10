"""
Purpose: Data worker for suggesting parsing, extraction, normalization, and quality checks for data-heavy tasks.
Input/Output: Examines the goal and returns data-handling guidance that other workers can use if relevant.
Important invariants: This worker stays advisory unless the task clearly contains data-processing responsibilities.
How to debug: If a data-heavy task misses normalization or validation steps, inspect this worker's recommendations.
"""

from __future__ import annotations

from fastapi import FastAPI

from services.shared.agentic_lab.config import get_settings
from services.shared.agentic_lab.repo_tools import write_report
from services.shared.agentic_lab.schemas import Artifact, HealthResponse, WorkerRequest, WorkerResponse

settings = get_settings()
app = FastAPI(title="Feberdin Data Worker", version="0.1.0")


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(service="data-worker")


@app.post("/run", response_model=WorkerResponse)
async def run(request: WorkerRequest) -> WorkerResponse:
    lowered = request.goal.lower()
    is_relevant = any(keyword in lowered for keyword in ("mail", "csv", "json", "xml", "parse", "extract", "classify"))
    outputs = {
        "relevant": is_relevant,
        "recommendations": (
            [
                "Define explicit schemas for extracted records.",
                "Validate and normalize untrusted input before downstream processing.",
                "Add negative tests for malformed records and partial payloads.",
            ]
            if is_relevant
            else ["No strong data-processing focus detected in the current task."]
        ),
    }
    report_path = write_report(settings.task_report_dir(request.task_id), "data-guidance.json", outputs)
    return WorkerResponse(
        worker="data",
        summary="Data-processing guidance prepared.",
        outputs=outputs,
        artifacts=[
            Artifact(
                name="data-guidance",
                path=str(report_path),
                description="Data worker recommendations stored with the task reports.",
            )
        ],
    )
