"""
Purpose: Coding worker for branch-based repository changes using either a local patch backend or an OpenHands adapter.
Input/Output: Receives a plan and repo context, applies a minimal set of file changes, and returns changed-file metadata.
Important invariants: Edits stay inside the target repo, risky paths are flagged, and the worker never commits automatically.
How to debug: If generated changes look wrong, inspect the plan, sampled files, parsed operations, and git diff returned here.
"""

from __future__ import annotations

from pathlib import Path

import httpx
from fastapi import FastAPI

from services.shared.agentic_lab.config import get_settings
from services.shared.agentic_lab.guardrails import detect_risk_flags
from services.shared.agentic_lab.llm import LLMClient, LLMError
from services.shared.agentic_lab.logging_utils import TaskLoggerAdapter, configure_logging
from services.shared.agentic_lab.repo_tools import (
    collect_repo_overview,
    create_branch_name,
    current_diff,
    ensure_branch,
    ensure_repository_checkout,
    read_text_file,
    write_report,
)
from services.shared.agentic_lab.schemas import Artifact, HealthResponse, WorkerRequest, WorkerResponse
from services.shared.agentic_lab.worker_governance import WorkerGovernanceService

settings = get_settings()
logger = configure_logging(settings.service_name, settings.log_level)
llm = LLMClient(settings)
worker_governance = WorkerGovernanceService(settings)
app = FastAPI(title="Feberdin Coding Worker", version="0.1.0")


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(service="coding-worker")


@app.post("/run", response_model=WorkerResponse)
async def run(request: WorkerRequest) -> WorkerResponse:
    task_logger = TaskLoggerAdapter(logger.logger, {"service": "coding-worker", "task_id": request.task_id})
    repo_path = Path(request.local_repo_path)
    source_repo_path = Path(str(request.metadata.get("source_local_repo_path") or request.local_repo_path))
    try:
        repo_path = ensure_repository_checkout(
            repository=request.repository,
            repo_path=repo_path,
            workspace_root=settings.workspace_root,
            base_branch=request.base_branch,
            repo_url=request.repo_url,
            task_id=request.task_id,
            source_repo_path=source_repo_path,
        )
        branch_name = request.branch_name or create_branch_name(request.goal, request.task_id)
        ensure_branch(repo_path, branch_name, request.base_branch)

        if settings.coding_provider == "openhands":
            task_logger.info("Using OpenHands adapter backend for coding.")
            return await _run_openhands_adapter(request, repo_path, branch_name)

        task_logger.info("Using local patch backend for coding.")
        return await _run_local_patch_backend(request, repo_path, branch_name)
    except Exception as exc:  # pragma: no cover - defensive runtime guard for operator-visible failures.
        task_logger.exception("Coding worker failed unexpectedly: %s", exc)
        return WorkerResponse(
            worker="coding",
            success=False,
            summary="Coding-Stage konnte nicht sauber vorbereitet oder ausgefuehrt werden.",
            errors=[f"{exc.__class__.__name__}: {exc}"],
            outputs={"local_repo_path": str(repo_path)},
        )


async def _run_local_patch_backend(
    request: WorkerRequest,
    repo_path: Path,
    branch_name: str,
) -> WorkerResponse:
    if not settings.has_llm_backend():
        return WorkerResponse(
            worker="coding",
            success=False,
            summary="Coding backend is unavailable.",
            errors=["Local patch mode requires a configured OpenAI-compatible model backend."],
        )

    requirements = request.prior_results.get("requirements", {}).get("outputs", {})
    architecture = request.prior_results.get("architecture", {}).get("outputs", {})
    research = request.prior_results.get("research", {}).get("outputs", {})
    overview = collect_repo_overview(repo_path)
    guidance_block = worker_governance.guidance_prompt_block(request, "coding")

    candidate_files = [
        path
        for path in architecture.get("touched_areas", [])
        or architecture.get("module_boundaries", [])
        or research.get("candidate_files", [])
        if (repo_path / path).exists() and (repo_path / path).is_file()
    ][:6]
    file_context = {path: read_text_file(repo_path, path) for path in candidate_files}

    try:
        patch_plan = await llm.complete_json(
            system_prompt=(
                "You are a careful coding agent. Return JSON with keys summary and operations. "
                "Each operation must have action=create_or_update, path, reason, content. "
                "Keep the change set small, coherent, and safe."
                f"{guidance_block}"
            ),
            user_prompt=(
                f"Goal:\n{request.goal}\n\n"
                f"Requirements:\n{requirements}\n\n"
                f"Architecture and implementation plan:\n{architecture}\n\n"
                f"Research:\n{research}\n\n"
                f"Repo overview:\n{overview}\n\n"
                f"Candidate file contents:\n{file_context}\n\n"
                "Generate only the necessary file contents for the requested change."
            ),
            worker_name="coding",
        )
    except LLMError as exc:
        return WorkerResponse(
            worker="coding",
            success=False,
            summary="Coding plan generation failed.",
            errors=[str(exc)],
        )

    operations = patch_plan.get("operations", [])
    if not operations:
        return WorkerResponse(
            worker="coding",
            success=False,
            summary="Coding backend returned no file operations.",
            errors=["The local patch backend did not generate any file operations."],
        )

    changed_paths: list[str] = []
    for operation in operations:
        target = Path(operation["path"])
        if target.is_absolute() or ".." in target.parts:
            return WorkerResponse(
                worker="coding",
                success=False,
                summary="Unsafe file path detected in coding output.",
                errors=[f"Unsafe path requested by model: {target}"],
            )

        resolved_target = (repo_path / target).resolve()
        if repo_path.resolve() not in resolved_target.parents and resolved_target != repo_path.resolve():
            return WorkerResponse(
                worker="coding",
                success=False,
                summary="Coding backend attempted to write outside the repo.",
                errors=[f"Rejected out-of-repo write: {resolved_target}"],
            )

        resolved_target.parent.mkdir(parents=True, exist_ok=True)
        resolved_target.write_text(operation["content"], encoding="utf-8")
        changed_paths.append(str(target))

    diff = current_diff(repo_path, request.base_branch)
    risk_flags = detect_risk_flags(diff["changed_files"], diff["diff_text"])
    report = {
        "summary": patch_plan.get("summary", "Applied local patch operations."),
        "branch_name": branch_name,
        "changed_files": diff["changed_files"],
        "diff_stat": diff["diff_stat"],
        "operations": operations,
    }
    report_path = write_report(settings.task_report_dir(request.task_id), "coding-report.json", report)

    if not diff["changed_files"]:
        return WorkerResponse(
            worker="coding",
            success=False,
            summary="No working tree changes were detected after applying the patch.",
            errors=["Generated operations did not result in any diff against the base branch."],
        )

    return WorkerResponse(
        worker="coding",
        summary=patch_plan.get("summary", "Applied local patch operations."),
        outputs={
            "branch_name": branch_name,
            "local_repo_path": str(repo_path),
            "changed_files": diff["changed_files"],
            "diff_stat": diff["diff_stat"],
            "model_operations": operations,
        },
        risk_flags=risk_flags,
        artifacts=[
            Artifact(
                name="coding-report",
                path=str(report_path),
                description="Applied operations and resulting diff summary.",
            )
        ],
    )


async def _run_openhands_adapter(
    request: WorkerRequest,
    repo_path: Path,
    branch_name: str,
) -> WorkerResponse:
    if not settings.openhands_enabled:
        return WorkerResponse(
            worker="coding",
            success=False,
            summary="OpenHands mode is disabled.",
            errors=["Set OPENHANDS_ENABLED=true and provide an adapter endpoint before using this backend."],
        )

    payload = {
        "task_id": request.task_id,
        "goal": request.goal,
        "repository": request.repository,
        "local_repo_path": str(repo_path),
        "base_branch": request.base_branch,
        "branch_name": branch_name,
        "research": request.prior_results.get("research", {}).get("outputs", {}),
        "requirements": request.prior_results.get("requirements", {}).get("outputs", {}),
        "architecture": request.prior_results.get("architecture", {}).get("outputs", {}),
    }

    try:
        async with httpx.AsyncClient(timeout=600) as client:
            response = await client.post(f"{settings.openhands_base_url.rstrip('/')}/api/run", json=payload)
            response.raise_for_status()
            adapter_result = response.json()
    except httpx.HTTPError as exc:
        return WorkerResponse(
            worker="coding",
            success=False,
            summary="OpenHands adapter call failed.",
            errors=[str(exc)],
        )

    diff = current_diff(repo_path, request.base_branch)
    risk_flags = detect_risk_flags(diff["changed_files"], diff["diff_text"])
    report_path = write_report(
        settings.task_report_dir(request.task_id),
        "coding-report.json",
        {
            "adapter_result": adapter_result,
            "diff_stat": diff["diff_stat"],
            "changed_files": diff["changed_files"],
        },
    )
    return WorkerResponse(
        worker="coding",
        summary=adapter_result.get("summary", "OpenHands adapter completed."),
        outputs={
            "branch_name": branch_name,
            "local_repo_path": str(repo_path),
            "changed_files": diff["changed_files"],
            "diff_stat": diff["diff_stat"],
            "adapter_result": adapter_result,
        },
        risk_flags=risk_flags,
        artifacts=[
            Artifact(
                name="coding-report",
                path=str(report_path),
                description="OpenHands adapter response and resulting diff summary.",
            )
        ],
    )
