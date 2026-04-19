"""
Purpose: Security worker for prompt-injection heuristics, secret hygiene, dependency risk hints, and dangerous change review.
Input/Output: Consumes task context and diff-related outputs and returns security findings, risk flags, and approval recommendations.
Important invariants: External content is always treated as untrusted, and risky changes cannot quietly slip past this worker.
How to debug: If a task is blocked by security unexpectedly, inspect the reported injection signals, dependency hints, and risk flags.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from services.shared.agentic_lab.config import get_settings
from services.shared.agentic_lab.guardrails import (
    assess_source_quality,
    detect_prompt_injection_signals,
    detect_risk_flags,
)
from services.shared.agentic_lab.llm import LLMClient, LLMError
from services.shared.agentic_lab.logging_utils import TaskLoggerAdapter, configure_logging
from services.shared.agentic_lab.repo_tools import current_diff, write_report
from services.shared.agentic_lab.schemas import Artifact, HealthResponse, WorkerRequest, WorkerResponse
from services.shared.agentic_lab.worker_governance import WorkerGovernanceService

settings = get_settings()
logger = configure_logging(settings.service_name, settings.log_level)
llm = LLMClient(settings)
worker_governance = WorkerGovernanceService(settings)
app = FastAPI(title="Feberdin Security Worker", version="0.1.0")


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(service="security-worker")


@app.post("/run", response_model=WorkerResponse)
async def run(request: WorkerRequest) -> WorkerResponse:
    task_logger = TaskLoggerAdapter(logger.logger, {"service": "security-worker", "task_id": request.task_id})
    research_outputs = request.prior_results.get("research", {}).get("outputs", {})
    architecture_outputs = request.prior_results.get("architecture", {}).get("outputs", {})
    repo_path = Path(request.local_repo_path)
    diff = current_diff(repo_path, request.base_branch)

    research_text = str(research_outputs)
    architecture_text = str(architecture_outputs)
    injection_signals = sorted(
        set(detect_prompt_injection_signals(research_text) + detect_prompt_injection_signals(architecture_text))
    )
    risk_flags = detect_risk_flags(diff["changed_files"], diff["diff_text"])
    dependency_findings = _dependency_hints(diff["changed_files"])
    source_quality = {
        source: assess_source_quality(source)
        for source in research_outputs.get("sources", {}).get("web_sources", [])
    }
    warnings: list[str] = []
    ai_findings: list[str] = []
    ai_residual_risks: list[str] = []
    ai_requires_human_approval = False
    ai_approval_reason = ""
    guidance_block = worker_governance.guidance_prompt_block(request, "security")

    try:
        ai_summary = await llm.complete_json(
            system_prompt=(
                "You are a security reviewer. Return JSON with keys findings, residual_risks, requires_human_approval, approval_reason."
                f"{guidance_block}"
            ),
            user_prompt=(
                f"Goal:\n{request.goal}\n\n"
                f"Research outputs:\n{research_outputs}\n\n"
                f"Architecture outputs:\n{architecture_outputs}\n\n"
                f"Changed files:\n{diff['changed_files']}\n\n"
                f"Diff stat:\n{diff['diff_stat']}\n"
            ),
            worker_name="security",
            required_keys=["findings", "residual_risks", "requires_human_approval", "approval_reason"],
        )
        ai_findings = ai_summary.get("findings", [])
        ai_residual_risks = [str(item) for item in ai_summary.get("residual_risks", []) if isinstance(item, str)]
        ai_requires_human_approval = bool(ai_summary.get("requires_human_approval"))
        ai_approval_reason = str(ai_summary.get("approval_reason") or "").strip()
        if ai_requires_human_approval:
            risk_flags.append("security_manual_review")
    except LLMError as exc:
        warnings.append(f"LLM security summary unavailable: {exc}")

    if injection_signals:
        risk_flags.append("external_prompt_injection_signal")

    normalized_risk_flags = sorted(set(risk_flags))
    all_findings = dependency_findings + ai_findings
    residual_risks = _build_residual_risks(
        injection_signals=injection_signals,
        risk_flags=normalized_risk_flags,
        ai_residual_risks=ai_residual_risks,
    )
    requires_human_approval = bool(normalized_risk_flags or injection_signals or ai_requires_human_approval)
    approval_reason = ai_approval_reason or None
    if requires_human_approval:
        approval_reason = approval_reason or "Security worker found prompt-injection signals or high-impact change areas."

    outputs = {
        "findings": all_findings,
        "residual_risks": residual_risks,
        "requires_human_approval": requires_human_approval,
        "approval_reason": approval_reason or "",
        "injection_signals": injection_signals,
        "source_quality": source_quality,
        "risk_flags": normalized_risk_flags,
        "diff_stat": diff["diff_stat"],
    }
    report_path = write_report(settings.task_report_dir(request.task_id), "security-report.json", outputs)
    task_logger.info("Security review completed with %s risk flags", len(outputs["risk_flags"]))

    return WorkerResponse(
        worker="security",
        summary="Security review completed.",
        outputs=outputs,
        warnings=warnings,
        risk_flags=outputs["risk_flags"],
        requires_human_approval=requires_human_approval,
        approval_reason=approval_reason,
        artifacts=[
            Artifact(
                name="security-report",
                path=str(report_path),
                description="Prompt-injection signals, dependency hints, and risk assessment.",
            )
        ],
    )


def _dependency_hints(changed_files: list[str]) -> list[str]:
    findings: list[str] = []
    for path in changed_files:
        lowered = path.lower()
        if lowered.endswith(("requirements.txt", "poetry.lock", "package-lock.json", "pnpm-lock.yaml", "uv.lock")):
            findings.append(f"Dependency lock or manifest changed in `{path}`. Review licensing and supply-chain implications.")
    return findings


def _build_residual_risks(
    *,
    injection_signals: list[str],
    risk_flags: list[str],
    ai_residual_risks: list[str],
) -> list[str]:
    """Return one stable residual-risk list so UI, probes, and downstream workers see the same contract keys.

    Example:
      injection_signals = ["ignore-previous-instructions"]
      risk_flags = ["shell_dangerous"]
      ai_residual_risks = ["Check secret handling."]
      ->
      [
        "Untrusted research or architecture text contains prompt-injection markers.",
        "High-impact change areas still require a manual security review.",
        "Check secret handling.",
      ]
    """

    residual_risks: list[str] = []
    if injection_signals:
        residual_risks.append("Untrusted research or architecture text contains prompt-injection markers.")
    if risk_flags:
        residual_risks.append("High-impact change areas still require a manual security review.")
    for item in ai_residual_risks:
        if item and item not in residual_risks:
            residual_risks.append(item)
    return residual_risks
