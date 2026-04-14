"""
Purpose: Human resources worker for suggesting the best worker mix and flagging missing specialist involvement.
Input/Output: Examines the Auftrag and early worker outputs to recommend worker coverage and escalation points.
Important invariants: Recommendations remain advisory and visible; they do not silently override the orchestrator's final authority.
How to debug: If a useful specialist is missing from a task, inspect the recommended worker list produced here.
"""

from __future__ import annotations

from fastapi import FastAPI

from services.shared.agentic_lab.config import get_settings
from services.shared.agentic_lab.repo_tools import write_report
from services.shared.agentic_lab.schemas import Artifact, HealthResponse, WorkerRequest, WorkerResponse
from services.shared.agentic_lab.task_profiles import is_readme_smiley_profile

settings = get_settings()
app = FastAPI(title="Feberdin Human Resources Worker", version="0.1.0")


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(service="human-resources-worker")


@app.post("/run", response_model=WorkerResponse)
async def run(request: WorkerRequest) -> WorkerResponse:
    lowered_goal = request.goal.lower()
    if is_readme_smiley_profile(request.metadata):
        recommended_workers = ["requirements", "cost", "human_resources", "coding", "validation", "github", "memory"]
        team_notes = [
            "README-Mini-Fix erkannt: ueberspringe breite Recherche und Architektur bewusst.",
            "Keine Daten-, UX-, Test- oder Security-Spezialisten fuer einen dokumentationsnahen Einzeilenfix noetig.",
        ]
    else:
        recommended_workers = ["requirements", "research", "architecture", "coding", "reviewer", "tester", "validation"]
        if any(keyword in lowered_goal for keyword in ("ui", "dashboard", "frontend", "weboberfläche")):
            recommended_workers.append("ux")
        if any(keyword in lowered_goal for keyword in ("mail", "csv", "data", "parse", "extrah")):
            recommended_workers.append("data")
        if any(keyword in lowered_goal for keyword in ("deploy", "docker", "unraid", "staging")):
            recommended_workers.append("deploy")
        team_notes = [
            "Keep review and security separate from coding.",
            "Escalate risky infrastructure or secret changes to manual approval.",
        ]

    outputs = {
        "recommended_workers": recommended_workers,
        "team_notes": team_notes,
    }
    report_path = write_report(settings.task_report_dir(request.task_id), "hr-plan.json", outputs)
    return WorkerResponse(
        worker="human_resources",
        summary="Worker fit and team allocation suggestions prepared.",
        outputs=outputs,
        artifacts=[
            Artifact(
                name="hr-plan",
                path=str(report_path),
                description="Recommended worker mix and escalation hints for the task.",
            )
        ],
    )
