"""
Purpose: UX worker for readability, flow, and operator guidance recommendations on UI-oriented tasks.
Input/Output: Examines the goal and returns UI/UX guidance for screens, states, and helpful user feedback.
Important invariants: UX suggestions should improve clarity and safety rather than adding decorative complexity.
How to debug: If a UI task remains hard to operate, inspect the flow and feedback recommendations from this worker.
"""

from __future__ import annotations

from fastapi import FastAPI

from services.shared.agentic_lab.config import get_settings
from services.shared.agentic_lab.repo_tools import write_report
from services.shared.agentic_lab.schemas import Artifact, HealthResponse, WorkerRequest, WorkerResponse

settings = get_settings()
app = FastAPI(title="Feberdin UX Worker", version="0.1.0")


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(service="ux-worker")


@app.post("/run", response_model=WorkerResponse)
async def run(request: WorkerRequest) -> WorkerResponse:
    lowered = request.goal.lower()
    is_relevant = any(keyword in lowered for keyword in ("ui", "frontend", "dashboard", "form", "weboberfläche"))
    outputs = {
        "relevant": is_relevant,
        "recommendations": (
            [
                "Keep next steps visible after every major action.",
                "Prefer labels and examples over hidden mental models.",
                "Add explicit error-prevention and staging-vs-production context in the UI.",
            ]
            if is_relevant
            else ["No strong UI/UX focus detected in the current task."]
        ),
    }
    report_path = write_report(settings.task_report_dir(request.task_id), "ux-guidance.json", outputs)
    return WorkerResponse(
        worker="ux",
        summary="UX guidance prepared.",
        outputs=outputs,
        artifacts=[
            Artifact(
                name="ux-guidance",
                path=str(report_path),
                description="UX worker recommendations stored with the task reports.",
            )
        ],
    )
