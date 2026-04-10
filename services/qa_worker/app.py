"""
Purpose: QA worker for post-deployment smoke, URL, and API health checks.
Input/Output: Receives a list of smoke checks and returns per-endpoint results plus short response snippets.
Important invariants: QA stays read-only, uses explicit expected statuses, and never performs destructive browser actions.
How to debug: If smoke tests fail, inspect the captured status codes and response snippets in the QA report.
"""

from __future__ import annotations

import httpx
from fastapi import FastAPI

from services.shared.agentic_lab.config import get_settings
from services.shared.agentic_lab.logging_utils import TaskLoggerAdapter, configure_logging
from services.shared.agentic_lab.repo_tools import write_report
from services.shared.agentic_lab.schemas import Artifact, HealthResponse, SmokeCheck, WorkerRequest, WorkerResponse

settings = get_settings()
logger = configure_logging(settings.service_name, settings.log_level)
app = FastAPI(title="Feberdin QA Worker", version="0.1.0")


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(service="qa-worker")


@app.post("/run", response_model=WorkerResponse)
async def run(request: WorkerRequest) -> WorkerResponse:
    task_logger = TaskLoggerAdapter(logger.logger, {"service": "qa-worker", "task_id": request.task_id})
    checks = request.smoke_checks or _default_checks(request)
    results: list[dict] = []
    errors: list[str] = []

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        for check in checks:
            smoke_check = SmokeCheck.model_validate(check)
            try:
                response = await client.get(smoke_check.url)
                body = response.text[:300]
                passed = response.status_code == smoke_check.expected_status
                if smoke_check.expected_substring:
                    passed = passed and smoke_check.expected_substring in body
                if not passed:
                    errors.append(
                        f"{smoke_check.name} failed: expected {smoke_check.expected_status}, got {response.status_code}."
                    )
                results.append(
                    {
                        "name": smoke_check.name,
                        "url": smoke_check.url,
                        "status_code": response.status_code,
                        "passed": passed,
                        "body_snippet": body,
                    }
                )
            except httpx.HTTPError as exc:
                errors.append(f"{smoke_check.name} request failed: {exc}")
                results.append({"name": smoke_check.name, "url": smoke_check.url, "passed": False, "error": str(exc)})

    report_path = write_report(
        settings.task_report_dir(request.task_id),
        "qa-report.json",
        {"results": results, "errors": errors},
    )
    task_logger.info("Completed %s smoke checks", len(results))

    return WorkerResponse(
        worker="qa",
        success=not errors,
        summary="QA smoke checks completed." if not errors else "One or more smoke checks failed.",
        outputs={"results": results},
        errors=errors,
        artifacts=[
            Artifact(
                name="qa-report",
                path=str(report_path),
                description="Post-deployment smoke test results.",
            )
        ],
    )


def _default_checks(request: WorkerRequest) -> list[dict]:
    if request.deployment and request.deployment.healthcheck_url:
        return [{"name": "default-healthcheck", "url": request.deployment.healthcheck_url, "expected_status": 200}]
    return [
        {
            "name": "default-healthcheck",
            "url": settings.staging_healthcheck_url,
            "expected_status": 200,
        }
    ]
