"""
Purpose: Documentation worker for operator handoff, implementation summaries, and suggested README-style updates.
Input/Output: Consumes upstream worker outputs and returns structured handoff notes plus documentation deltas.
Important invariants: Documentation must state assumptions and residual risks clearly instead of overstating completion.
How to debug: If the handoff is too vague, inspect the sections generated from requirements, validation, and deployment outputs here.
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
app = FastAPI(title="Feberdin Documentation Worker", version="0.1.0")


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(service="documentation-worker")


@app.post("/run", response_model=WorkerResponse)
async def run(request: WorkerRequest) -> WorkerResponse:
    task_logger = TaskLoggerAdapter(logger.logger, {"service": "documentation-worker", "task_id": request.task_id})
    validation = request.prior_results.get("validation", {}).get("outputs", {})
    security = request.prior_results.get("security", {}).get("outputs", {})
    deployment = request.prior_results.get("deploy", {}).get("outputs", {})
    guidance_block = worker_governance.guidance_prompt_block(request, "documentation")

    try:
        handoff = await llm.complete(
            system_prompt=(
                "You are a documentation lead. Produce markdown with sections Summary, Validation, Risks, "
                "Deployment Notes, Next Steps."
                f"{guidance_block}"
            ),
            user_prompt=(
                f"Goal:\n{request.goal}\n\n"
                f"Validation:\n{validation}\n\n"
                f"Security:\n{security}\n\n"
                f"Deployment:\n{deployment}\n\n"
                "Keep it readable for non-programmers operating a homelab system."
            ),
            worker_name="documentation",
        )
    except LLMError as exc:
        task_logger.warning("LLM documentation handoff unavailable: %s", exc)
        handoff = _heuristic_handoff(request.goal, validation, security, deployment)

    report_path = write_report(settings.task_report_dir(request.task_id), "handoff.md", handoff)
    return WorkerResponse(
        worker="documentation",
        summary="Documentation handoff prepared.",
        outputs={"handoff_markdown": handoff},
        artifacts=[
            Artifact(
                name="handoff",
                path=str(report_path),
                description="Operator-facing summary of results, risks, and next steps.",
            )
        ],
    )


def _heuristic_handoff(goal: str, validation: dict, security: dict, deployment: dict) -> str:
    return (
        f"# Task Handoff\n\n"
        f"## Summary\n{goal}\n\n"
        f"## Validation\n{validation}\n\n"
        f"## Risks\n{security}\n\n"
        f"## Deployment Notes\n{deployment}\n\n"
        "## Next Steps\n- Review flagged risks.\n- Inspect the draft PR.\n- Run staging smoke checks if enabled.\n"
    )
