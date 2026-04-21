"""
Purpose: Memory worker for persisting decisions, residual risks, and worker learnings as task memory artifacts.
Input/Output: Consumes upstream worker outputs and writes a durable task memory record for later reuse and debugging.
Important invariants: Memory entries must capture decisions and uncertainties without exposing secrets or raw sensitive content.
How to debug: If future tasks miss relevant context, inspect the generated memory file and the decisions it recorded.
"""

from __future__ import annotations

from fastapi import FastAPI

from services.shared.agentic_lab.config import get_settings
from services.shared.agentic_lab.logging_utils import TaskLoggerAdapter, configure_logging
from services.shared.agentic_lab.repo_tools import write_report
from services.shared.agentic_lab.schemas import Artifact, HealthResponse, WorkerRequest, WorkerResponse

settings = get_settings()
logger = configure_logging(settings.service_name, settings.log_level)
app = FastAPI(title="Feberdin Memory Worker", version="0.1.0")


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(service="memory-worker")


def _build_handoff_payload(request: WorkerRequest) -> dict[str, object]:
    """Create a compact operator handoff that explains what changed and what happens next."""

    coding_outputs = request.prior_results.get("coding", {}).get("outputs", {})
    validation_outputs = request.prior_results.get("validation", {}).get("outputs", {})
    github_outputs = request.prior_results.get("github", {}).get("outputs", {})
    deploy_outputs = request.prior_results.get("deploy", {}).get("outputs", {})

    changed_files = list(coding_outputs.get("changed_files") or [])
    residual_risks = list(validation_outputs.get("residual_risks") or [])
    pull_request_url = github_outputs.get("pull_request_url")
    commit_sha = github_outputs.get("commit_sha")
    publish_strategy = github_outputs.get("publish_strategy")
    deployment_target = str(request.metadata.get("deployment_target") or "")
    deployment_performed = bool(deploy_outputs)
    deploy_allowed = bool(request.metadata.get("allow_deploy_after_success") or request.auto_deploy_staging)

    next_steps: list[str] = []
    if pull_request_url:
        next_steps.append(f"Draft-PR pruefen oder mergen: {pull_request_url}")
    if not deployment_performed:
        if deploy_allowed:
            next_steps.append("Deployment war freigegeben, wurde in diesem Lauf aber nicht ausgefuehrt.")
        else:
            next_steps.append(
                "Kein Auto-Deploy in diesem Lauf: der neue Stand liegt auf Branch/PR und wird erst nach bewusstem Update aktiv."
            )
    if deployment_target == "staging" and not deployment_performed:
        next_steps.append("Fuer Livebetrieb den gemergten Stand deployen oder den Ziel-Commit manuell updaten.")
    if residual_risks:
        next_steps.append(f"Rest-Risiko beachten: {residual_risks[0]}")

    return {
        "task_id": request.task_id,
        "goal": request.goal,
        "repository": request.repository,
        "base_branch": request.base_branch,
        "branch_name": request.branch_name,
        "changed_files": changed_files,
        "diff_stat": coding_outputs.get("diff_stat"),
        "commit_sha": commit_sha,
        "pull_request_url": pull_request_url,
        "publish_strategy": publish_strategy,
        "deployment_target": deployment_target or "unknown",
        "deployment_performed": deployment_performed,
        "deploy_allowed": deploy_allowed,
        "validation_fulfilled": list(validation_outputs.get("fulfilled") or []),
        "validation_residual_risks": residual_risks,
        "release_readiness": validation_outputs.get("release_readiness"),
        "next_steps": next_steps,
    }


def _build_handoff_markdown(payload: dict[str, object]) -> str:
    """Render a short handoff that humans can skim before deciding to merge or deploy."""

    changed_files = payload.get("changed_files") or []
    fulfilled = payload.get("validation_fulfilled") or []
    residual_risks = payload.get("validation_residual_risks") or []
    next_steps = payload.get("next_steps") or []
    commit_sha = str(payload.get("commit_sha") or "noch keiner")
    pull_request_url = str(payload.get("pull_request_url") or "noch keine PR")
    publish_strategy = str(payload.get("publish_strategy") or "unbekannt")
    deployment_target = str(payload.get("deployment_target") or "unknown")
    deployment_performed = "ja" if payload.get("deployment_performed") else "nein"
    branch_name = str(payload.get("branch_name") or "noch kein Branch")
    release_readiness = str(payload.get("release_readiness") or "unbekannt")
    diff_stat = str(payload.get("diff_stat") or "keine Diff-Stat verfuegbar")

    changed_files_block = "\n".join(f"- {path}" for path in changed_files) or "- keine"
    fulfilled_block = "\n".join(f"- {item}" for item in fulfilled) or "- keine bestaetigten Punkte"
    risks_block = "\n".join(f"- {item}" for item in residual_risks) or "- keine expliziten Rest-Risiken"
    next_steps_block = "\n".join(f"- {item}" for item in next_steps) or "- keine weiteren Schritte hinterlegt"

    return (
        f"# Task-Handoff\n\n"
        f"## Auftrag\n"
        f"{payload.get('goal')}\n\n"
        f"## Git-Stand\n"
        f"- Repository: {payload.get('repository')}\n"
        f"- Basis-Branch: {payload.get('base_branch')}\n"
        f"- Arbeits-Branch: {branch_name}\n"
        f"- Commit: {commit_sha}\n"
        f"- PR: {pull_request_url}\n"
        f"- Publish-Strategie: {publish_strategy}\n\n"
        f"## Aenderungen\n"
        f"{changed_files_block}\n\n"
        f"```text\n{diff_stat}\n```\n\n"
        f"## Validierung\n"
        f"- Release Readiness: {release_readiness}\n"
        f"- Deployment-Ziel: {deployment_target}\n"
        f"- Deployment in diesem Lauf ausgefuehrt: {deployment_performed}\n\n"
        f"### Bestaetigt\n"
        f"{fulfilled_block}\n\n"
        f"### Rest-Risiken\n"
        f"{risks_block}\n\n"
        f"## Naechste Schritte\n"
        f"{next_steps_block}\n"
    )


@app.post("/run", response_model=WorkerResponse)
async def run(request: WorkerRequest) -> WorkerResponse:
    task_logger = TaskLoggerAdapter(logger.logger, {"service": "memory-worker", "task_id": request.task_id})
    memory_dir = settings.data_dir / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)

    # Persist the raw worker context first so future debugging can reconstruct why a task ended in this state.
    memory_entry = {
        "task_id": request.task_id,
        "goal": request.goal,
        "repository": request.repository,
        "branch_name": request.branch_name,
        "requirements": request.prior_results.get("requirements", {}).get("outputs", {}),
        "architecture": request.prior_results.get("architecture", {}).get("outputs", {}),
        "security": request.prior_results.get("security", {}).get("outputs", {}),
        "validation": request.prior_results.get("validation", {}).get("outputs", {}),
        "coding": request.prior_results.get("coding", {}).get("outputs", {}),
        "github": request.prior_results.get("github", {}).get("outputs", {}),
        "deploy": request.prior_results.get("deploy", {}).get("outputs", {}),
    }
    report_path = write_report(settings.task_report_dir(request.task_id), "memory-entry.json", memory_entry)
    durable_path = write_report(memory_dir, f"{request.task_id}.json", memory_entry)

    # Write a compact handoff alongside the raw memory so operators and future agents can load the important deltas quickly.
    handoff_payload = _build_handoff_payload(request)
    handoff_markdown = _build_handoff_markdown(handoff_payload)
    handoff_report_json = write_report(settings.task_report_dir(request.task_id), "handoff.json", handoff_payload)
    handoff_report_md = write_report(settings.task_report_dir(request.task_id), "handoff.md", handoff_markdown)
    durable_handoff_json = write_report(memory_dir, f"{request.task_id}-handoff.json", handoff_payload)
    durable_handoff_md = write_report(memory_dir, f"{request.task_id}-handoff.md", handoff_markdown)
    task_logger.info("Task memory written to %s", durable_path)

    return WorkerResponse(
        worker="memory",
        summary="Task memory updated.",
        outputs={
            "memory_path": str(durable_path),
            "handoff_json_path": str(durable_handoff_json),
            "handoff_markdown_path": str(durable_handoff_md),
            "next_steps": handoff_payload["next_steps"],
        },
        artifacts=[
            Artifact(
                name="memory-entry",
                path=str(report_path),
                description="Task-specific memory artifact stored with the reports.",
            ),
            Artifact(
                name="durable-memory-entry",
                path=str(durable_path),
                description="Longer-lived task memory stored under the shared data directory.",
            ),
            Artifact(
                name="handoff-json",
                path=str(handoff_report_json),
                description="Compact machine-readable handoff for later agents or operator tooling.",
            ),
            Artifact(
                name="handoff-markdown",
                path=str(handoff_report_md),
                description="Short human-readable handoff with branch, PR, changes, and next steps.",
            ),
            Artifact(
                name="durable-handoff-json",
                path=str(durable_handoff_json),
                description="Longer-lived machine-readable handoff stored under the shared data directory.",
            ),
            Artifact(
                name="durable-handoff-markdown",
                path=str(durable_handoff_md),
                description="Longer-lived human-readable handoff stored under the shared data directory.",
            ),
        ],
    )
