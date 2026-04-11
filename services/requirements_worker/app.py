"""
Purpose: Requirements worker for turning an Auftrag into structured requirements, assumptions, risks, and acceptance criteria.
Input/Output: Receives the original goal and returns a concise but actionable requirements package for the rest of the team.
Important invariants: Assumptions and open questions must stay explicit so later workers do not treat guesses as facts.
How to debug: If downstream workers drift, inspect the extracted requirements and acceptance criteria produced here first.
"""

from __future__ import annotations

from fastapi import FastAPI

from services.shared.agentic_lab.config import get_settings
from services.shared.agentic_lab.llm import LLMClient, LLMError
from services.shared.agentic_lab.logging_utils import TaskLoggerAdapter, configure_logging
from services.shared.agentic_lab.repo_tools import write_report
from services.shared.agentic_lab.schemas import Artifact, HealthResponse, WorkerRequest, WorkerResponse
from services.shared.agentic_lab.worker_governance import WorkerGovernanceService

settings = get_settings()
logger = configure_logging(settings.service_name, settings.log_level)
llm = LLMClient(settings)
worker_governance = WorkerGovernanceService(settings)
app = FastAPI(title="Feberdin Requirements Worker", version="0.1.0")


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(service="requirements-worker")


@app.post("/run", response_model=WorkerResponse)
async def run(request: WorkerRequest) -> WorkerResponse:
    task_logger = TaskLoggerAdapter(logger.logger, {"service": "requirements-worker", "task_id": request.task_id})
    try:
        guidance_block = worker_governance.guidance_prompt_block(request, "requirements")

        try:
            outputs = await llm.complete_json(
                system_prompt=(
                    "You are a requirements engineer. Return JSON with keys summary, requirements, wishes, assumptions, "
                    "risks, acceptance_criteria, open_questions, recommended_workers."
                    f"{guidance_block}"
                ),
                user_prompt=(
                    f"Original Auftrag:\n{request.goal}\n\n"
                    f"Repository: {request.repository}\n"
                    "Separate hard requirements, optional wishes, assumptions, and risks clearly."
                ),
                worker_name="requirements",
            )
        except LLMError as exc:
            task_logger.warning("LLM requirements extraction unavailable: %s", exc)
            outputs = _heuristic_requirements(request.goal, request.repository)

        report_path = write_report(settings.task_report_dir(request.task_id), "requirements.json", outputs)
        return WorkerResponse(
            worker="requirements",
            summary="Requirements package created.",
            outputs=outputs,
            artifacts=[
                Artifact(
                    name="requirements",
                    path=str(report_path),
                    description="Structured requirements, assumptions, risks, and acceptance criteria.",
                )
            ],
        )
    except Exception as exc:  # pragma: no cover - defensive runtime guard for operator-visible failures.
        task_logger.exception("Requirements worker failed unexpectedly: %s", exc)
        return WorkerResponse(
            worker="requirements",
            success=False,
            summary="Requirements extraction failed before the report could be completed.",
            errors=[f"{exc.__class__.__name__}: {exc}"],
            outputs={"repository": request.repository},
        )


def _heuristic_requirements(goal: str, repository: str) -> dict:
    return {
        "summary": f"Structured Auftrag for {repository}: {goal}",
        "requirements": [goal],
        "wishes": [],
        "assumptions": [
            "The target repository can be cloned or is already available in the mounted workspace.",
            "GitHub is the source of truth for branches, commits, issues, and PRs.",
        ],
        "risks": [
            "The Auftrag may omit repo-specific runtime or deployment expectations.",
            "Ambiguous success criteria could cause avoidable rework later in the workflow.",
        ],
        "acceptance_criteria": [
            "The delivered result addresses the original goal in a testable way.",
            "Open assumptions and residual risks are visible to the operator.",
            "No critical action proceeds without the required approval gates.",
        ],
        "open_questions": [
            "Are there target runtime, framework, or compatibility constraints inside the selected repository?",
        ],
        "recommended_workers": ["research", "architecture", "coding", "reviewer", "tester", "validation"],
    }
