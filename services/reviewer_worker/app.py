"""
Purpose: Reviewer worker for diff quality, security, and architecture checks before tests and PR creation.
Input/Output: Receives the working tree diff and returns findings, warnings, risk flags, and optional approval requirements.
Important invariants: High-impact or secret-sensitive changes must never pass silently; they require clear escalation.
How to debug: If a benign change is escalated, inspect the detected risk flags and heuristics in the generated review report.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from services.shared.agentic_lab.config import get_settings
from services.shared.agentic_lab.guardrails import detect_risk_flags
from services.shared.agentic_lab.llm import LLMClient, LLMError
from services.shared.agentic_lab.logging_utils import TaskLoggerAdapter, configure_logging
from services.shared.agentic_lab.repo_tools import current_diff, write_report
from services.shared.agentic_lab.schemas import Artifact, HealthResponse, WorkerRequest, WorkerResponse

settings = get_settings()
logger = configure_logging(settings.service_name, settings.log_level)
llm = LLMClient(settings)
app = FastAPI(title="Feberdin Reviewer Worker", version="0.1.0")


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(service="reviewer-worker")


@app.post("/run", response_model=WorkerResponse)
async def run(request: WorkerRequest) -> WorkerResponse:
    task_logger = TaskLoggerAdapter(logger.logger, {"service": "reviewer-worker", "task_id": request.task_id})
    repo_path = Path(request.local_repo_path)
    diff = current_diff(repo_path, request.base_branch)
    if not diff["changed_files"]:
        return WorkerResponse(
            worker="reviewer",
            success=False,
            summary="Review failed because there is no diff to inspect.",
            errors=["No changed files were detected against the base branch."],
        )

    risk_flags = detect_risk_flags(diff["changed_files"], diff["diff_text"])
    findings = _heuristic_findings(diff["changed_files"])
    warnings: list[str] = []

    try:
        ai_review = await _review_with_llm(request.goal, diff)
        findings.extend(ai_review.get("findings", []))
        warnings.extend(ai_review.get("warnings", []))
    except LLMError as exc:
        warnings.append(f"LLM review skipped: {exc}")

    requires_human_approval = bool(risk_flags)
    approval_reason = None
    if requires_human_approval:
        approval_reason = "Risky changes detected in infrastructure, secrets, or destructive command areas."

    report = {
        "changed_files": diff["changed_files"],
        "diff_stat": diff["diff_stat"],
        "risk_flags": risk_flags,
        "findings": findings,
        "warnings": warnings,
        "requires_human_approval": requires_human_approval,
        "approval_reason": approval_reason,
    }
    report_path = write_report(settings.task_report_dir(request.task_id), "review-report.json", report)
    task_logger.info("Review completed with %s risk flags", len(risk_flags))

    return WorkerResponse(
        worker="reviewer",
        summary="Diff review completed.",
        outputs=report,
        warnings=warnings,
        risk_flags=risk_flags,
        requires_human_approval=requires_human_approval,
        approval_reason=approval_reason,
        artifacts=[
            Artifact(
                name="review-report",
                path=str(report_path),
                description="Reviewer findings, warnings, and risk flags.",
            )
        ],
    )


def _heuristic_findings(changed_files: list[str]) -> list[str]:
    findings: list[str] = []
    code_files = [path for path in changed_files if path.endswith((".py", ".ts", ".js", ".go", ".rs"))]
    test_files = [path for path in changed_files if "test" in path.lower()]
    if code_files and not test_files:
        findings.append("Code files changed without any obvious corresponding test update.")
    if any(path.startswith(".github/workflows") for path in changed_files):
        findings.append("CI or workflow files changed; verify self-hosted runner and secret usage carefully.")
    return findings


async def _review_with_llm(goal: str, diff: dict) -> dict:
    system_prompt = (
        "You are a strict reviewer focused on bugs, regressions, security, and architecture drift. "
        "Return JSON with keys findings and warnings."
    )
    user_prompt = (
        f"Goal:\n{goal}\n\n"
        f"Diff stat:\n{diff['diff_stat']}\n\n"
        f"Changed files:\n{diff['changed_files']}\n\n"
        f"Unified diff:\n{diff['diff_text'][:12000]}\n\n"
        "List only important findings."
    )
    return await llm.complete_json(system_prompt, user_prompt, worker_name="reviewer")
