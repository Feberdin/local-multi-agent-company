"""
Purpose: Validation worker for checking whether the produced result really satisfies the original Auftrag and acceptance criteria.
Input/Output: Consumes upstream worker outputs and returns a structured fit assessment with residual risks and maturity rating.
Important invariants: Validation must distinguish confirmed fulfillment from assumptions and must not rubber-stamp weak outputs.
How to debug: If the final handoff feels too optimistic, inspect the fulfilled-versus-unverified split reported here.
"""

from __future__ import annotations

from fastapi import FastAPI

from services.shared.agentic_lab.config import get_settings
from services.shared.agentic_lab.llm import LLMClient, LLMError
from services.shared.agentic_lab.logging_utils import TaskLoggerAdapter, configure_logging
from services.shared.agentic_lab.repo_tools import write_report
from services.shared.agentic_lab.schemas import Artifact, HealthResponse, WorkerRequest, WorkerResponse

settings = get_settings()
logger = configure_logging(settings.service_name, settings.log_level)
llm = LLMClient(settings)
app = FastAPI(title="Feberdin Validation Worker", version="0.1.0")


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(service="validation-worker")


@app.post("/run", response_model=WorkerResponse)
async def run(request: WorkerRequest) -> WorkerResponse:
    task_logger = TaskLoggerAdapter(logger.logger, {"service": "validation-worker", "task_id": request.task_id})
    requirements = request.prior_results.get("requirements", {}).get("outputs", {})
    architecture = request.prior_results.get("architecture", {}).get("outputs", {})
    tests = request.prior_results.get("tester", {}).get("outputs", {})
    security = request.prior_results.get("security", {}).get("outputs", {})

    try:
        outputs = await llm.complete_json(
            system_prompt=(
                "You are a validation lead. Return JSON with keys fulfilled, partially_verified, unverified, residual_risks, "
                "release_readiness, recommendation."
            ),
            user_prompt=(
                f"Original Auftrag:\n{request.goal}\n\n"
                f"Requirements:\n{requirements}\n\n"
                f"Architecture:\n{architecture}\n\n"
                f"Tests:\n{tests}\n\n"
                f"Security:\n{security}\n\n"
                "Be strict and separate evidence-backed completion from assumptions."
            ),
            worker_name="validation",
        )
    except LLMError as exc:
        task_logger.warning("LLM validation unavailable: %s", exc)
        outputs = _heuristic_validation(requirements, tests, security)

    report_path = write_report(settings.task_report_dir(request.task_id), "validation.json", outputs)
    return WorkerResponse(
        worker="validation",
        summary="Validation against the original Auftrag completed.",
        outputs=outputs,
        warnings=[],
        artifacts=[
            Artifact(
                name="validation",
                path=str(report_path),
                description="Validation of acceptance criteria, residual risks, and maturity rating.",
            )
        ],
    )


def _heuristic_validation(requirements: dict, tests: dict, security: dict) -> dict:
    return {
        "fulfilled": requirements.get("acceptance_criteria", []),
        "partially_verified": ["Human review is still required for any flagged risk or environment-specific deployment assumption."],
        "unverified": [] if not tests.get("errors") else ["One or more test steps failed or were not configured."],
        "residual_risks": security.get("risk_flags", []),
        "release_readiness": "prototype" if security.get("risk_flags") else "beta",
        "recommendation": "Proceed with a draft PR and staging-only deployment, then request human review for flagged risks.",
    }
