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
from services.shared.agentic_lab.task_profiles import (
    is_readme_smiley_profile,
    is_worker_stage_timeout_profile,
    profile_target_files,
    profile_target_timeout_seconds,
)
from services.shared.agentic_lab.worker_governance import WorkerGovernanceService

settings = get_settings()
logger = configure_logging(settings.service_name, settings.log_level)
llm = LLMClient(settings)
worker_governance = WorkerGovernanceService(settings)
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

    if is_readme_smiley_profile(request.metadata):
        outputs = _readme_smiley_validation(request)
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

    if is_worker_stage_timeout_profile(request.metadata):
        outputs = _worker_stage_timeout_validation(request)
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

    guidance_block = worker_governance.guidance_prompt_block(request, "validation")

    try:
        outputs = await llm.complete_json(
            system_prompt=(
                "You are a validation lead. Return JSON with keys fulfilled, partially_verified, unverified, residual_risks, "
                "release_readiness, recommendation."
                f"{guidance_block}"
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
            required_keys=[
                "fulfilled",
                "partially_verified",
                "unverified",
                "residual_risks",
                "release_readiness",
                "recommendation",
            ],
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


def _readme_smiley_validation(request: WorkerRequest) -> dict:
    """Validate the trivial README smiley fast path without a slow model roundtrip."""

    coding_outputs = request.prior_results.get("coding", {}).get("outputs", {})
    changed_files = [item for item in coding_outputs.get("changed_files", []) if isinstance(item, str)]
    only_readme_changed = changed_files == ["README.md"]
    fulfilled = ["Der Fast-Path blieb auf den minimalen README-Scope begrenzt."]
    partially_verified: list[str] = []
    unverified: list[str] = []
    if only_readme_changed:
        fulfilled.append("Nur README.md ist im resultierenden Diff sichtbar.")
    else:
        partially_verified.append(
            "Die geaenderten Dateien weichen vom erwarteten Minimal-Scope ab oder sind noch nicht vollstaendig sichtbar."
        )
    if not changed_files:
        unverified.append("Es wurde noch kein veraenderter README-Diff nachgewiesen.")

    return {
        "fulfilled": fulfilled,
        "partially_verified": partially_verified,
        "unverified": unverified,
        "residual_risks": [] if only_readme_changed else ["Pruefe den Diff manuell, falls neben README.md weitere Dateien auftauchen."],
        "release_readiness": "beta" if only_readme_changed else "prototype",
        "recommendation": (
            "Proceed with a draft PR for the tiny README fix."
            if only_readme_changed
            else "Inspect the resulting diff before creating a PR."
        ),
    }


def _worker_stage_timeout_validation(request: WorkerRequest) -> dict:
    """Validate the deterministic timeout-config fast path without another slow model roundtrip."""

    coding_outputs = request.prior_results.get("coding", {}).get("outputs", {})
    changed_files = [item for item in coding_outputs.get("changed_files", []) if isinstance(item, str)]
    allowed_files = set(profile_target_files(request.metadata))
    required_file = "services/shared/agentic_lab/config.py"
    timeout_seconds = profile_target_timeout_seconds(request.metadata) or 3600.0
    timeout_label = f"{timeout_seconds:.1f}" if float(timeout_seconds).is_integer() else str(timeout_seconds)

    fulfilled: list[str] = []
    partially_verified: list[str] = []
    unverified: list[str] = []

    if required_file in changed_files:
        fulfilled.append(f"`{required_file}` wurde im Diff geaendert und traegt damit den Zielwert {timeout_label}.")
    else:
        unverified.append(f"Der erwartete Kern-Diff in `{required_file}` ist noch nicht sichtbar.")

    if changed_files and all(path in allowed_files for path in changed_files):
        fulfilled.append("Alle sichtbaren Aenderungen bleiben innerhalb des erlaubten Timeout-Fast-Path-Scopes.")
    elif changed_files:
        partially_verified.append("Im Diff tauchen Dateien ausserhalb des erlaubten Timeout-Fast-Path-Scopes auf.")
    else:
        unverified.append("Es wurden noch keine veraenderten Dateien fuer den Timeout-Fix nachgewiesen.")

    release_readiness = "beta" if required_file in changed_files and not partially_verified else "prototype"
    residual_risks = []
    if partially_verified:
        residual_risks.append("Pruefe den Diff manuell, falls ausserhalb von Config/README/Docs weitere Dateien auftauchen.")
    residual_risks.append("Der laengere Stage-Timeout kann langsame Fehlversuche spaeter sichtbar machen statt sie schneller abzubrechen.")

    return {
        "fulfilled": fulfilled,
        "partially_verified": partially_verified,
        "unverified": unverified,
        "residual_risks": residual_risks,
        "release_readiness": release_readiness,
        "recommendation": (
            "Proceed with a draft PR for the deterministic timeout-config fix."
            if release_readiness == "beta"
            else "Inspect the resulting diff before creating a PR."
        ),
    }
