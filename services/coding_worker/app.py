"""
Purpose: Coding worker for branch-based repository changes using either a local patch backend or an OpenHands adapter.
Input/Output: Receives a plan and repo context, applies a minimal set of file changes, and returns changed-file metadata.
Important invariants: Edits stay inside the target repo, risky paths are flagged, and the worker never commits automatically.
How to debug: If generated changes look wrong, inspect the plan, sampled files, parsed operations, and git diff returned here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI

from services.shared.agentic_lab.code_index import build_index
from services.shared.agentic_lab.config import get_settings
from services.shared.agentic_lab.edit_ops import EditOperation, normalize_raw_operation
from services.shared.agentic_lab.guardrails import detect_risk_flags
from services.shared.agentic_lab.llm import LLMClient, LLMError
from services.shared.agentic_lab.logging_utils import TaskLoggerAdapter, configure_logging
from services.shared.agentic_lab.patch_engine import PatchResult, apply_edit_plan
from services.shared.agentic_lab.repo_tools import (
    collect_repo_overview,
    create_branch_name,
    current_diff,
    ensure_branch,
    ensure_repository_checkout,
    git,
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

        rollback_commit_sha = str(request.metadata.get("rollback_commit_sha") or "").strip()
        if rollback_commit_sha:
            task_logger.info("Using deterministic git-revert backend for rollback commit %s.", rollback_commit_sha)
            return _run_git_revert_backend(request, repo_path, branch_name, rollback_commit_sha)

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


def _run_git_revert_backend(
    request: WorkerRequest,
    repo_path: Path,
    branch_name: str,
    rollback_commit_sha: str,
) -> WorkerResponse:
    """Apply one deterministic git revert when a rollback task is scheduled."""

    try:
        git(["revert", "--no-edit", rollback_commit_sha], repo_path=repo_path, timeout=600)
    except Exception as exc:  # noqa: BLE001 - operator-facing rollback errors must stay explicit
        return WorkerResponse(
            worker="coding",
            success=False,
            summary="Git-Revert fuer den vorbereiteten Rollback ist fehlgeschlagen.",
            errors=[f"{type(exc).__name__}: {exc}"],
            outputs={
                "branch_name": branch_name,
                "rollback_commit_sha": rollback_commit_sha,
                "local_repo_path": str(repo_path),
                "backend": "git_revert",
            },
        )

    diff = current_diff(repo_path, request.base_branch)
    risk_flags = detect_risk_flags(diff["changed_files"], diff["diff_text"])
    report = {
        "summary": f"Git-Revert fuer Commit {rollback_commit_sha} wurde vorbereitet.",
        "branch_name": branch_name,
        "changed_files": diff["changed_files"],
        "diff_stat": diff["diff_stat"],
        "rollback_commit_sha": rollback_commit_sha,
        "backend": "git_revert",
    }
    report_path = write_report(settings.task_report_dir(request.task_id), "coding-revert-report.json", report)

    if not diff["changed_files"]:
        return WorkerResponse(
            worker="coding",
            success=False,
            summary="Der Git-Revert hat keine sichtbaren Aenderungen erzeugt.",
            errors=[
                "Der angeforderte Commit ist moeglicherweise bereits revertiert oder fuehrt im aktuellen Branch zu keinem Diff."
            ],
            outputs=report,
            artifacts=[
                Artifact(
                    name="coding-revert-report",
                    path=str(report_path),
                    description="Deterministischer Git-Revert ohne resultierenden Diff.",
                )
            ],
        )

    return WorkerResponse(
        worker="coding",
        summary=f"Rollback-Aenderung fuer Commit {rollback_commit_sha} wurde vorbereitet.",
        outputs=report,
        risk_flags=risk_flags,
        artifacts=[
            Artifact(
                name="coding-revert-report",
                path=str(report_path),
                description="Deterministischer Git-Revert fuer den angeforderten Commit.",
            )
        ],
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

    arch_touched = [
        p for p in architecture.get("touched_areas", []) or architecture.get("module_boundaries", [])
        if isinstance(p, str)
    ]
    candidate_files = [p for p in arch_touched if (repo_path / p).exists() and (repo_path / p).is_file()][:6]

    # When the architecture hallucinated more than half of its touched_areas (non-existent paths),
    # supplement with research candidate_files so the coding LLM gets the real relevant source files.
    if arch_touched and len(candidate_files) < len(arch_touched) / 2:
        for p in research.get("candidate_files", []):
            if isinstance(p, str) and (repo_path / p).exists() and (repo_path / p).is_file() and p not in candidate_files:
                candidate_files.append(p)
                if len(candidate_files) >= 6:
                    break

    # Final fallback: when no usable paths came from architecture or research, grep Python sources.
    if not candidate_files:
        candidate_files = _grep_for_candidates(repo_path, request.goal)

    file_context = {path: read_text_file(repo_path, path) for path in candidate_files}

    # Build a lightweight symbol index for Python candidate files so the LLM can
    # use targeted replace_symbol_body operations instead of full file rewrites.
    code_index = build_index(repo_path, candidate_files)
    symbol_index_block = code_index.format_for_prompt()
    plan_attempts: list[dict[str, Any]] = []

    try:
        patch_plan = await llm.complete_json(
            system_prompt=_coding_system_prompt(guidance_block),
            user_prompt=_coding_user_prompt(
                request.goal,
                requirements,
                architecture,
                research,
                overview,
                file_context,
                symbol_index_block,
            ),
            worker_name="coding",
        )
        plan_attempts.append(_patch_plan_attempt_snapshot("initial", patch_plan))
    except LLMError as exc:
        return _coding_failure_response(
            request=request,
            repo_path=repo_path,
            summary="Coding plan generation failed.",
            errors=[str(exc)],
            candidate_files=candidate_files,
            arch_touched=arch_touched,
            research=research,
            file_context=file_context,
            symbol_index_block=symbol_index_block,
            plan_attempts=plan_attempts,
        )

    raw_operations = _raw_operations_from_plan(patch_plan)
    if not raw_operations:
        try:
            patch_plan = await llm.complete_json(
                system_prompt=_coding_system_prompt(guidance_block),
                user_prompt=_coding_noop_retry_user_prompt(
                    goal=request.goal,
                    requirements=requirements,
                    architecture=architecture,
                    research=research,
                    overview=overview,
                    file_context=file_context,
                    symbol_index_block=symbol_index_block,
                    candidate_files=candidate_files,
                    previous_plan=patch_plan,
                ),
                worker_name="coding",
            )
            plan_attempts.append(_patch_plan_attempt_snapshot("retry_after_no_operations", patch_plan))
        except LLMError as exc:
            return _coding_failure_response(
                request=request,
                repo_path=repo_path,
                summary="Coding plan retry failed after an empty first plan.",
                errors=[str(exc)],
                candidate_files=candidate_files,
                arch_touched=arch_touched,
                research=research,
                file_context=file_context,
                symbol_index_block=symbol_index_block,
                plan_attempts=plan_attempts,
            )
        raw_operations = _raw_operations_from_plan(patch_plan)

    if not raw_operations:
        blocking_reason = str(patch_plan.get("blocking_reason") or "").strip()
        errors = ["The local patch backend did not generate any file operations."]
        if blocking_reason:
            errors.append(f"Blocking reason: {blocking_reason}")
        return _coding_failure_response(
            request=request,
            repo_path=repo_path,
            summary="Coding backend returned no file operations.",
            errors=errors,
            candidate_files=candidate_files,
            arch_touched=arch_touched,
            research=research,
            file_context=file_context,
            symbol_index_block=symbol_index_block,
            plan_attempts=plan_attempts,
            patch_plan=patch_plan,
            warnings=[
                "Das Modell hat zwar ein JSON-Objekt geliefert, aber keine konkreten Datei-Operationen vorgeschlagen."
            ],
        )

    # Parse and normalize operations (supports both new structured edits and legacy create_or_update)
    edit_ops, parse_errors = _parse_operations(raw_operations)
    if not edit_ops:
        return _coding_failure_response(
            request=request,
            repo_path=repo_path,
            summary="Could not parse any valid edit operations from the model response.",
            errors=parse_errors or ["All operations failed to parse."],
            candidate_files=candidate_files,
            arch_touched=arch_touched,
            research=research,
            file_context=file_context,
            symbol_index_block=symbol_index_block,
            plan_attempts=plan_attempts,
            patch_plan=patch_plan,
        )

    # Apply via patch engine (symbol → anchor → line → full-file, with rollback on failure)
    patch_result: PatchResult = apply_edit_plan(repo_path, edit_ops)
    if not patch_result.success:
        op_errors = [
            f"Operation {r.operation_index} ({r.action} on {r.file_path}): {r.error}"
            for r in patch_result.failed_operations
        ]
        return _coding_failure_response(
            request=request,
            repo_path=repo_path,
            summary="Patch engine could not apply the generated edit operations.",
            errors=op_errors or patch_result.errors,
            candidate_files=candidate_files,
            arch_touched=arch_touched,
            research=research,
            file_context=file_context,
            symbol_index_block=symbol_index_block,
            plan_attempts=plan_attempts,
            patch_plan=patch_plan,
            parse_errors=parse_errors,
            patch_result=patch_result,
        )

    diff = current_diff(repo_path, request.base_branch)
    risk_flags = detect_risk_flags(diff["changed_files"], diff["diff_text"])

    op_summaries = [
        {"action": r.action, "file": r.file_path, "strategy": r.strategy_used, "lines": r.lines_changed}
        for r in patch_result.operation_results
    ]
    warnings = [r.syntax_warning for r in patch_result.operation_results if r.syntax_warning]

    report = {
        "summary": patch_plan.get("summary", "Applied structured edit operations."),
        "branch_name": branch_name,
        "changed_files": diff["changed_files"],
        "diff_stat": diff["diff_stat"],
        "operation_results": op_summaries,
        "patch_summary": patch_result.summary_text(),
    }
    report_path = write_report(settings.task_report_dir(request.task_id), "coding-report.json", report)

    if not diff["changed_files"]:
        return _coding_failure_response(
            request=request,
            repo_path=repo_path,
            summary="No working tree changes were detected after applying the patch.",
            errors=["Generated operations did not result in any diff against the base branch."],
            candidate_files=candidate_files,
            arch_touched=arch_touched,
            research=research,
            file_context=file_context,
            symbol_index_block=symbol_index_block,
            plan_attempts=plan_attempts,
            patch_plan=patch_plan,
            parse_errors=parse_errors,
            patch_result=patch_result,
            diff=diff,
        )

    return WorkerResponse(
        worker="coding",
        summary=patch_plan.get("summary", "Applied structured edit operations."),
        outputs={
            "branch_name": branch_name,
            "local_repo_path": str(repo_path),
            "changed_files": diff["changed_files"],
            "diff_stat": diff["diff_stat"],
            "operation_results": op_summaries,
            "patch_summary": patch_result.summary_text(),
        },
        risk_flags=risk_flags,
        warnings=warnings,
        artifacts=[
            Artifact(
                name="coding-report",
                path=str(report_path),
                description="Applied edit operations and resulting diff summary.",
            )
        ],
    )


def _grep_for_candidates(repo_path: Path, goal: str, max_files: int = 6) -> list[str]:
    """Fallback file discovery: grep Python sources for keywords from the goal.

    Used when architecture/research workers returned no valid file paths. Extracts
    short keyword tokens from the goal and searches .py files for matches.
    """
    import re
    import subprocess

    stopwords = {"add", "the", "to", "in", "for", "a", "an", "and", "or", "with", "of", "from", "on", "by"}
    tokens = [t for t in re.split(r"\W+", goal.lower()) if len(t) > 3 and t not in stopwords]
    if not tokens:
        return []

    hits: dict[str, int] = {}
    for token in tokens[:4]:
        try:
            result = subprocess.run(
                ["grep", "-rl", "--include=*.py", token, str(repo_path)],
                capture_output=True,
                text=True,
                timeout=10,
            )
            for line in result.stdout.splitlines():
                rel = str(Path(line).relative_to(repo_path))
                hits[rel] = hits.get(rel, 0) + 1
        except Exception:
            continue

    # Sort by hit count descending, prefer non-test files
    ranked = sorted(hits.items(), key=lambda x: (x[0].startswith("tests/"), -x[1]))
    return [path for path, _ in ranked[:max_files]]


def _coding_system_prompt(guidance_block: str) -> str:
    return (
        "You are a careful coding agent implementing file changes for a software repository.\n"
        "Return a JSON object with EXACTLY this structure — no prose, no markdown:\n"
        '{"summary": "...", "operations": [<list of operations>]}\n'
        "\n"
        "AVAILABLE OPERATIONS — use the most targeted one that applies:\n"
        "1. replace_symbol_body (preferred for Python — avoids full file rewrite):\n"
        '   {"action":"replace_symbol_body","file_path":"p.py","symbol_name":"fn",'
        '"new_content":"def fn(...):\\n    ...","reason":"..."}\n'
        "2. replace_block (replace a code block by anchor text, fuzzy match):\n"
        '   {"action":"replace_block","file_path":"p.py","anchor_text":"first line",'
        '"new_content":"replacement","reason":"..."}\n'
        "3. insert_after_anchor / insert_before_anchor:\n"
        '   {"action":"insert_after_anchor","file_path":"p.py","anchor_text":"line",'
        '"new_content":"new code","reason":"..."}\n'
        "4. replace_lines (use symbol index line numbers):\n"
        '   {"action":"replace_lines","file_path":"p.py","start_line":42,"end_line":67,'
        '"new_content":"replacement","reason":"..."}\n'
        "5. create_file — create a new file (new_content = full content):\n"
        '   {"action":"create_file","file_path":"new.py","new_content":"...","reason":"..."}\n'
        "6. create_or_update — full file rewrite (only when >50% of file changes):\n"
        '   {"action":"create_or_update","file_path":"p.py","new_content":"full file","reason":"..."}\n'
        "RULE: prefer replace_symbol_body for Python changes. "
        "Use create_or_update only as last resort.\n"
        "If the goal requires NO file changes (e.g. analysis, explanation), return:\n"
        '{"summary": "No code changes needed: <reason>", "operations": []}'
        f"{guidance_block}"
    )


def _coding_noop_retry_user_prompt(
    *,
    goal: str,
    requirements: object,
    architecture: object,
    research: object,
    overview: dict[str, Any],
    file_context: dict[str, str],
    symbol_index_block: str,
    candidate_files: list[str],
    previous_plan: dict[str, Any],
) -> str:
    """Ask for one stricter second attempt when the first JSON plan returned no operations."""

    base_prompt = _coding_user_prompt(
        goal,
        requirements,
        architecture,
        research,
        overview,
        file_context,
        symbol_index_block,
    )
    retry_block = [
        "Previous coding attempt returned zero file operations even though this task asked for code changes.",
        f"Previous summary: {previous_plan.get('summary', 'keine Zusammenfassung')}",
        f"Candidate files you may change: {candidate_files or ['keine konkreten Kandidaten sichtbar']}",
        "Return at least one concrete file operation when a safe change is possible.",
        "If no safe code change is possible, keep operations empty and add a short blocking_reason field.",
        "Do not answer with prose outside the JSON object.",
    ]
    return base_prompt + "\n\n" + "\n".join(retry_block)


def _coding_user_prompt(
    goal: str,
    requirements: object,
    architecture: object,
    research: object,
    overview: dict,
    file_context: dict,
    symbol_index_block: str,
) -> str:
    parts = [
        f"Goal:\n{goal}",
        f"Requirements:\n{requirements}",
        f"Architecture and implementation plan:\n{architecture}",
        f"Research:\n{research}",
        f"Repo overview:\n{overview}",
    ]
    if symbol_index_block:
        parts.append(symbol_index_block)
    if file_context:
        parts.append(f"Candidate file contents:\n{file_context}")
    parts.append(
        "Generate only the minimum changes needed. "
        "Prefer replace_symbol_body for Python functions. "
        "Use create_or_update only if a new file is needed or >50% of the file changes."
    )
    return "\n\n".join(parts)


def _raw_operations_from_plan(patch_plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only dict-based operations so diagnostics can distinguish empty from malformed plans."""

    raw_operations = patch_plan.get("operations", [])
    if not isinstance(raw_operations, list):
        return []
    return [item for item in raw_operations if isinstance(item, dict)]


def _patch_plan_attempt_snapshot(attempt: str, patch_plan: dict[str, Any]) -> dict[str, Any]:
    """Capture one compact snapshot of the model plan for later debugging and UI inspection."""

    raw_operations = _raw_operations_from_plan(patch_plan)
    return {
        "attempt": attempt,
        "summary": str(patch_plan.get("summary") or "").strip(),
        "operation_count": len(raw_operations),
        "operation_actions": [str(item.get("action") or "unknown") for item in raw_operations[:12]],
        "blocking_reason": str(patch_plan.get("blocking_reason") or "").strip(),
        "response_keys": sorted(str(key) for key in patch_plan.keys()),
    }


def _coding_failure_response(
    *,
    request: WorkerRequest,
    repo_path: Path,
    summary: str,
    errors: list[str],
    candidate_files: list[str],
    arch_touched: list[str],
    research: object,
    file_context: dict[str, str],
    symbol_index_block: str,
    plan_attempts: list[dict[str, Any]],
    patch_plan: dict[str, Any] | None = None,
    parse_errors: list[str] | None = None,
    patch_result: PatchResult | None = None,
    diff: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
) -> WorkerResponse:
    """Return one operator-friendly failure response plus a persisted JSON report for later diagnosis."""

    research_outputs = research if isinstance(research, dict) else {}
    output_payload: dict[str, Any] = {
        "local_repo_path": str(repo_path),
        "candidate_files": candidate_files,
        "architecture_touched_areas": arch_touched,
        "research_candidate_files": [
            item for item in research_outputs.get("candidate_files", []) if isinstance(item, str)
        ],
        "file_context_paths": sorted(file_context.keys()),
        "symbol_index_available": bool(symbol_index_block),
        "plan_attempts": plan_attempts,
        "parse_errors": parse_errors or [],
    }
    if patch_plan is not None:
        output_payload["patch_plan_summary"] = str(patch_plan.get("summary") or "").strip()
        output_payload["raw_operation_count"] = len(_raw_operations_from_plan(patch_plan))
        if patch_plan.get("blocking_reason"):
            output_payload["blocking_reason"] = str(patch_plan.get("blocking_reason"))
    if patch_result is not None:
        output_payload["patch_summary"] = patch_result.summary_text()
    if diff is not None:
        output_payload["changed_files"] = diff.get("changed_files", [])
        output_payload["diff_stat"] = diff.get("diff_stat", "")

    report_path = write_report(
        settings.task_report_dir(request.task_id),
        "coding-failure.json",
        {
            "summary": summary,
            "goal": request.goal,
            "repository": request.repository,
            "outputs": output_payload,
            "errors": errors,
            "warnings": warnings or [],
        },
    )

    return WorkerResponse(
        worker="coding",
        success=False,
        summary=summary,
        errors=errors,
        warnings=warnings or [],
        outputs=output_payload,
        artifacts=[
            Artifact(
                name="coding-failure",
                path=str(report_path),
                description="Diagnosebericht zum fehlgeschlagenen Coding-Plan oder Patch-Lauf.",
            )
        ],
    )


def _parse_operations(raw_ops: list) -> tuple[list[EditOperation], list[str]]:
    """Parse raw LLM operation dicts into EditOperation models. Returns (ops, parse_errors)."""
    ops: list[EditOperation] = []
    errors: list[str] = []
    for i, raw in enumerate(raw_ops):
        if not isinstance(raw, dict):
            errors.append(f"Operation {i} is not a dict: {type(raw).__name__}")
            continue
        try:
            normalized = normalize_raw_operation(raw)
            ops.append(EditOperation(**normalized))
        except Exception as exc:
            errors.append(f"Operation {i} parse error: {exc}")
    return ops, errors


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
