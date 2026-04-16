"""
Purpose: Coding worker for branch-based repository changes using either a local patch backend or an OpenHands adapter.
Input/Output: Receives a plan and repo context, applies a minimal set of file changes, and returns changed-file metadata.
Important invariants: Edits stay inside the target repo, risky paths are flagged, and the worker never commits automatically.
How to debug: If generated changes look wrong, inspect the plan, sampled files, parsed operations, and git diff returned here.
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI

from services.shared.agentic_lab.code_index import build_index
from services.shared.agentic_lab.config import get_settings
from services.shared.agentic_lab.edit_ops import (
    EditAction,
    EditOperation,
    normalize_raw_operation,
    validate_raw_operation,
)
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
from services.shared.agentic_lab.task_profiles import (
    README_SMILEY_CODING_STRATEGY,
    WORKER_STAGE_TIMEOUT_CODING_STRATEGY,
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

    if is_readme_smiley_profile(request.metadata):
        return _run_readme_smiley_fast_path(request, repo_path, branch_name)
    if is_worker_stage_timeout_profile(request.metadata):
        return _run_worker_stage_timeout_fast_path(request, repo_path, branch_name)

    requirements = request.prior_results.get("requirements", {}).get("outputs", {})
    architecture = request.prior_results.get("architecture", {}).get("outputs", {})
    research = request.prior_results.get("research", {}).get("outputs", {})
    overview = collect_repo_overview(repo_path)
    guidance_block = worker_governance.guidance_prompt_block(request, "coding")

    arch_touched = [
        p for p in architecture.get("touched_areas", []) or architecture.get("module_boundaries", [])
        if isinstance(p, str)
    ]
    candidate_files = _select_candidate_files(
        repo_path=repo_path,
        goal=request.goal,
        requirements=requirements,
        architecture=architecture,
        research=research,
    )

    # Build a lightweight symbol index for Python candidate files so the LLM can
    # use targeted replace_symbol_body operations instead of full file rewrites.
    code_index = build_index(repo_path, candidate_files)
    symbol_index_block = code_index.format_for_prompt()
    file_context = _build_prompt_file_context(
        repo_path=repo_path,
        candidate_files=candidate_files,
        goal=request.goal,
        requirements=requirements,
        code_index=code_index,
    )
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
                candidate_files,
            ),
            worker_name="coding",
            required_keys=["summary", "operations"],
        )
        plan_attempts.append(_patch_plan_attempt_snapshot("initial", patch_plan))
    except LLMError as exc:
        plan_attempts.append(
            {
                "attempt": "initial_contract_failure",
                "error": _short_text(str(exc), limit=900),
            }
        )
        try:
            patch_plan = await llm.complete_json(
                system_prompt=_coding_system_prompt(guidance_block),
                user_prompt=_coding_contract_recovery_user_prompt(
                    goal=request.goal,
                    requirements=requirements,
                    candidate_files=candidate_files,
                    file_context=file_context,
                    symbol_index_block=symbol_index_block,
                    previous_error=str(exc),
                ),
                worker_name="coding",
                required_keys=["summary", "operations"],
            )
            plan_attempts.append(_patch_plan_attempt_snapshot("retry_after_contract_failure", patch_plan))
        except LLMError as retry_exc:
            return _coding_failure_response(
                request=request,
                repo_path=repo_path,
                summary="Coding plan generation failed.",
                errors=[str(exc), str(retry_exc)],
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
                required_keys=["summary", "operations"],
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


def _run_readme_smiley_fast_path(
    request: WorkerRequest,
    repo_path: Path,
    branch_name: str,
) -> WorkerResponse:
    """
    Apply the smallest safe README smiley patch without spending minutes on model planning.

    Example:
      Before: "Probe README"
      After:  ":) Probe README"
    """

    target_path = repo_path / "README.md"
    if not target_path.is_file():
        return WorkerResponse(
            worker="coding",
            success=False,
            summary="README-Mini-Fix konnte nicht ausgefuehrt werden.",
            errors=["README.md wurde im isolierten Task-Workspace nicht gefunden."],
            outputs={"local_repo_path": str(repo_path), "branch_name": branch_name},
        )

    original_content = target_path.read_text(encoding="utf-8", errors="ignore")
    updated_content = _prepend_smiley_to_first_line(original_content)
    if updated_content == original_content:
        report = {
            "summary": "README hatte bereits das gewuenschte Smiley-Praefix.",
            "branch_name": branch_name,
            "changed_files": [],
            "diff_stat": "",
            "deterministic_strategy": README_SMILEY_CODING_STRATEGY,
        }
        report_path = write_report(settings.task_report_dir(request.task_id), "coding-report.json", report)
        return WorkerResponse(
            worker="coding",
            summary="README war bereits im gewuenschten Zustand.",
            outputs={
                "branch_name": branch_name,
                "local_repo_path": str(repo_path),
                "changed_files": [],
                "diff_stat": "",
                "deterministic_strategy": README_SMILEY_CODING_STRATEGY,
            },
            warnings=["README.md enthaelt bereits das gewuenschte `:)`-Praefix in der ersten Zeile."],
            artifacts=[
                Artifact(
                    name="coding-report",
                    path=str(report_path),
                    description="Deterministischer README-Mini-Fix ohne resultierenden Diff.",
                )
            ],
        )

    edit_plan = [
        EditOperation(
            action=EditAction.CREATE_OR_UPDATE,
            file_path="README.md",
            reason="Der Trivial-Fast-Path setzt nur ein ASCII-Smiley an den Anfang der ersten README-Zeile.",
            new_content=updated_content,
        )
    ]
    patch_result = apply_edit_plan(repo_path, edit_plan)
    if not patch_result.success:
        errors = [
            f"Operation {item.operation_index} ({item.action} on {item.file_path}): {item.error}"
            for item in patch_result.failed_operations
        ]
        return WorkerResponse(
            worker="coding",
            success=False,
            summary="README-Mini-Fix konnte nicht angewendet werden.",
            errors=errors or patch_result.errors,
            outputs={"local_repo_path": str(repo_path), "branch_name": branch_name},
        )

    diff = current_diff(repo_path, request.base_branch)
    risk_flags = detect_risk_flags(diff["changed_files"], diff["diff_text"])
    report = {
        "summary": "README-Mini-Fix wurde deterministisch angewendet.",
        "branch_name": branch_name,
        "changed_files": diff["changed_files"],
        "diff_stat": diff["diff_stat"],
        "deterministic_strategy": README_SMILEY_CODING_STRATEGY,
        "patch_summary": patch_result.summary_text(),
    }
    report_path = write_report(settings.task_report_dir(request.task_id), "coding-report.json", report)
    return WorkerResponse(
        worker="coding",
        summary="README-Mini-Fix wurde deterministisch angewendet.",
        outputs={
            "branch_name": branch_name,
            "local_repo_path": str(repo_path),
            "changed_files": diff["changed_files"],
            "diff_stat": diff["diff_stat"],
            "deterministic_strategy": README_SMILEY_CODING_STRATEGY,
            "patch_summary": patch_result.summary_text(),
        },
        risk_flags=risk_flags,
        artifacts=[
            Artifact(
                name="coding-report",
                path=str(report_path),
                description="Deterministischer README-Mini-Fix mit resultierendem Diff.",
            )
        ],
    )


def _run_worker_stage_timeout_fast_path(
    request: WorkerRequest,
    repo_path: Path,
    branch_name: str,
) -> WorkerResponse:
    """
    Apply one deterministic timeout-config fix when self-improvement already named WORKER_STAGE_TIMEOUT_SECONDS.

    Example:
      Before in config.py: default=1800.0
      After in config.py:  default=3600.0
    """

    target_timeout_seconds = profile_target_timeout_seconds(request.metadata) or 3600.0
    integer_timeout = _format_timeout_seconds_for_docs(target_timeout_seconds)
    target_files = profile_target_files(request.metadata) or [
        "services/shared/agentic_lab/config.py",
        "README.md",
        "docs/configuration.md",
        "docs/troubleshooting.md",
    ]

    config_path = repo_path / "services" / "shared" / "agentic_lab" / "config.py"
    if not config_path.is_file():
        return WorkerResponse(
            worker="coding",
            success=False,
            summary="Timeout-Config-Fix konnte nicht ausgefuehrt werden.",
            errors=[
                "Die echte Ziel-Datei `services/shared/agentic_lab/config.py` wurde im isolierten Task-Workspace nicht gefunden."
            ],
            outputs={"local_repo_path": str(repo_path), "branch_name": branch_name},
        )

    edit_plan: list[EditOperation] = []
    changed_targets: list[str] = []

    config_relative_path = "services/shared/agentic_lab/config.py"
    config_original = config_path.read_text(encoding="utf-8", errors="ignore")
    config_updated = _replace_worker_stage_timeout_default(config_original, target_timeout_seconds)
    if config_updated == config_original:
        return WorkerResponse(
            worker="coding",
            success=False,
            summary="Timeout-Config-Fix konnte den echten Konfigurationswert nicht finden.",
            errors=[
                "In `services/shared/agentic_lab/config.py` wurde kein passender Default fuer `worker_stage_timeout_seconds` gefunden."
            ],
            outputs={
                "local_repo_path": str(repo_path),
                "branch_name": branch_name,
                "target_timeout_seconds": target_timeout_seconds,
            },
        )

    edit_plan.append(
        EditOperation(
            action=EditAction.CREATE_OR_UPDATE,
            file_path=config_relative_path,
            reason=(
                "Der deterministische Timeout-Fast-Path setzt den echten Default fuer "
                "`worker_stage_timeout_seconds` auf den Zielwert."
            ),
            new_content=config_updated,
        )
    )
    changed_targets.append(config_relative_path)

    for relative_path in target_files:
        if relative_path == config_relative_path:
            continue
        full_path = repo_path / relative_path
        if not full_path.is_file():
            continue
        original_content = full_path.read_text(encoding="utf-8", errors="ignore")
        updated_content = _replace_worker_stage_timeout_examples(original_content, integer_timeout)
        if updated_content == original_content:
            continue
        edit_plan.append(
            EditOperation(
                action=EditAction.CREATE_OR_UPDATE,
                file_path=relative_path,
                reason=(
                    "Der deterministische Timeout-Fast-Path haelt sichtbare Operator-Beispiele "
                    "fuer `WORKER_STAGE_TIMEOUT_SECONDS` konsistent."
                ),
                new_content=updated_content,
            )
        )
        changed_targets.append(relative_path)

    patch_result = apply_edit_plan(repo_path, edit_plan)
    if not patch_result.success:
        errors = [
            f"Operation {item.operation_index} ({item.action} on {item.file_path}): {item.error}"
            for item in patch_result.failed_operations
        ]
        return WorkerResponse(
            worker="coding",
            success=False,
            summary="Timeout-Config-Fix konnte nicht angewendet werden.",
            errors=errors or patch_result.errors,
            outputs={"local_repo_path": str(repo_path), "branch_name": branch_name},
        )

    diff = current_diff(repo_path, request.base_branch)
    risk_flags = detect_risk_flags(diff["changed_files"], diff["diff_text"])
    report = {
        "summary": "Timeout-Config-Fix wurde deterministisch angewendet.",
        "branch_name": branch_name,
        "changed_files": diff["changed_files"],
        "diff_stat": diff["diff_stat"],
        "deterministic_strategy": WORKER_STAGE_TIMEOUT_CODING_STRATEGY,
        "target_timeout_seconds": target_timeout_seconds,
        "target_files": changed_targets,
        "patch_summary": patch_result.summary_text(),
    }
    report_path = write_report(settings.task_report_dir(request.task_id), "coding-report.json", report)
    return WorkerResponse(
        worker="coding",
        summary="Timeout-Config-Fix wurde deterministisch angewendet.",
        outputs={
            "branch_name": branch_name,
            "local_repo_path": str(repo_path),
            "changed_files": diff["changed_files"],
            "diff_stat": diff["diff_stat"],
            "deterministic_strategy": WORKER_STAGE_TIMEOUT_CODING_STRATEGY,
            "target_timeout_seconds": target_timeout_seconds,
            "target_files": changed_targets,
            "patch_summary": patch_result.summary_text(),
        },
        risk_flags=risk_flags,
        artifacts=[
            Artifact(
                name="coding-report",
                path=str(report_path),
                description="Deterministischer Timeout-Config-Fix mit resultierendem Diff.",
            )
        ],
    )


def _grep_for_candidates(repo_path: Path, goal: str, max_files: int = 6) -> list[str]:
    """Fallback file discovery: grep Python sources for keywords from the goal.

    Used when architecture/research workers returned no valid file paths. Extracts
    short keyword tokens from the goal and searches common code, config, and UI files for matches.
    """
    import re
    import shutil
    import subprocess

    stopwords = {"add", "the", "to", "in", "for", "a", "an", "and", "or", "with", "of", "from", "on", "by"}
    tokens = [t for t in re.split(r"\W+", goal.lower()) if len(t) > 3 and t not in stopwords]
    if not tokens:
        return []

    rg_path = shutil.which("rg")
    candidate_globs = (
        "*.py",
        "*.sh",
        "*.yaml",
        "*.yml",
        "*.json",
        "*.toml",
        "*.md",
        "*.html",
        "*.css",
        "Dockerfile*",
    )
    hits: dict[str, int] = {}
    for token in tokens[:4]:
        try:
            if rg_path:
                command = [rg_path, "-l", "-i"]
                for candidate_glob in candidate_globs:
                    command.extend(["-g", candidate_glob])
                command.extend([token, str(repo_path)])
            else:
                command = ["grep", "-rli", token, str(repo_path)]
            result = subprocess.run(command, capture_output=True, text=True, timeout=10)
            for line in result.stdout.splitlines():
                path = Path(line)
                try:
                    rel = str(path.relative_to(repo_path))
                except ValueError:
                    continue
                hits[rel] = hits.get(rel, 0) + 1
        except Exception:
            continue

    # Sort by hit count descending, prefer non-test files
    ranked = sorted(hits.items(), key=lambda x: (x[0].startswith("tests/"), -x[1]))
    return [path for path, _ in ranked[:max_files]]


def _select_candidate_files(
    *,
    repo_path: Path,
    goal: str,
    requirements: object,
    architecture: object,
    research: object,
    max_files: int = 6,
) -> list[str]:
    """
    Choose the most relevant existing files for coding so concrete source targets win over repo metadata.

    Why this exists:
    Earlier runs trusted architecture.touched_areas too literally. When architecture returned
    existing but generic files like README.md, the real implementation target dropped out of
    file_context and the model blocked on oversized, irrelevant context instead of patching code.
    """

    architecture_outputs = architecture if isinstance(architecture, dict) else {}
    research_outputs = research if isinstance(research, dict) else {}
    arch_candidates = _existing_candidate_paths(
        architecture_outputs.get("touched_areas", []) or architecture_outputs.get("module_boundaries", []),
        repo_path,
    )
    research_candidates = _existing_candidate_paths(research_outputs.get("candidate_files", []), repo_path)
    candidate_pool = _merge_unique_candidate_paths(research_candidates, arch_candidates)

    if not candidate_pool:
        return _grep_for_candidates(repo_path, goal, max_files=max_files)

    ranked = _rank_candidate_files(
        repo_path=repo_path,
        goal=goal,
        requirements=requirements,
        architecture=architecture_outputs,
        research=research_outputs,
        arch_candidates=arch_candidates,
        research_candidates=research_candidates,
        candidate_pool=candidate_pool,
    )
    if ranked:
        return ranked[:max_files]

    return candidate_pool[:max_files]


def _existing_candidate_paths(raw_paths: object, repo_path: Path) -> list[str]:
    """Filter one mixed worker payload down to unique real files in the repo."""

    if not isinstance(raw_paths, list):
        return []
    existing: list[str] = []
    for item in raw_paths:
        if not isinstance(item, str):
            continue
        candidate = item.strip()
        if not candidate:
            continue
        full_path = repo_path / candidate
        if full_path.exists() and full_path.is_file() and candidate not in existing:
            existing.append(candidate)
    return existing


def _merge_unique_candidate_paths(*groups: list[str]) -> list[str]:
    """Preserve order while combining research and architecture candidates."""

    merged: list[str] = []
    for group in groups:
        for path in group:
            if path not in merged:
                merged.append(path)
    return merged


def _rank_candidate_files(
    *,
    repo_path: Path,
    goal: str,
    requirements: object,
    architecture: dict[str, Any],
    research: dict[str, Any],
    arch_candidates: list[str],
    research_candidates: list[str],
    candidate_pool: list[str],
) -> list[str]:
    """Rank file candidates by goal fit, code relevance, and signal quality."""

    keywords = _prompt_focus_keywords(goal, requirements)
    context_text = _normalize_prompt_search_text(
        "\n".join(
            [
                _short_text(architecture.get("summary"), limit=1200),
                _short_text(research.get("research_notes"), limit=1200),
                " ".join(str(item) for item in architecture.get("implementation_plan", []) if isinstance(item, str)),
                " ".join(str(item) for item in research.get("candidate_files", []) if isinstance(item, str)),
            ]
        )
    )
    has_specific_source_candidate = any(_looks_like_source_candidate(path) for path in candidate_pool)
    ranked: list[tuple[int, int, str]] = []
    for index, path in enumerate(candidate_pool):
        preview = read_text_file(repo_path, path, max_bytes=8_000)
        haystack = _normalize_prompt_search_text(f"{path}\n{preview}")
        matched_terms = [keyword for keyword in keywords if keyword in haystack][:12]
        path_terms = _candidate_path_terms(path)
        score = len(matched_terms)

        if path in research_candidates:
            score += 9
        if path in arch_candidates:
            score += 5
        if _looks_like_source_candidate(path):
            score += 4
        if any(term in context_text for term in path_terms):
            score += 4
        if "git" in matched_terms and "clone" in matched_terms:
            score += 6
        if "ensure_repository_checkout" in matched_terms:
            score += 5
        if "_clone_target_from_best_source" in matched_terms:
            score += 4
        if "error" in matched_terms and "backend" in matched_terms:
            score += 2
        if _is_generic_repo_metadata_candidate(path) and has_specific_source_candidate:
            score -= 10
        if path.startswith("tests/") and has_specific_source_candidate:
            score -= 2

        ranked.append((score, -index, path))

    ranked.sort(key=lambda item: (-item[0], item[1], item[2]))
    return [path for _, _, path in ranked]


def _candidate_path_terms(path: str) -> list[str]:
    """Extract the non-generic terms from a candidate path for context matching."""

    ignore = {"services", "shared", "agentic_lab", "tests", "unit", "app", "main", "file"}
    parts = re.split(r"[^a-z0-9_]+", _normalize_prompt_search_text(path))
    terms: list[str] = []
    for part in parts:
        if len(part) < 4 or part in ignore:
            continue
        if part not in terms:
            terms.append(part)
    return terms


def _looks_like_source_candidate(path: str) -> bool:
    """Prefer real implementation files when the goal asks for a code change."""

    return Path(path).suffix.lower() in {".py", ".sh", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs"}


def _is_generic_repo_metadata_candidate(path: str) -> bool:
    """Recognize broad repo files that often drown the real fix in prompt noise."""

    normalized = path.strip().lower()
    return normalized in {
        "readme.md",
        "docker-compose.yml",
        "docker-compose.yaml",
        "pyproject.toml",
        ".github/workflows/ci.yml",
        ".github/workflows/ci.yaml",
    }


def _prepend_smiley_to_first_line(content: str) -> str:
    """Prefix the first line with `:) ` exactly once while preserving the rest of the file verbatim."""

    if content == "":
        return ":)\n"

    lines = content.splitlines(keepends=True)
    if not lines:
        return ":)\n"

    first_line = lines[0]
    line_break = ""
    if first_line.endswith("\r\n"):
        line_break = "\r\n"
    elif first_line.endswith("\n"):
        line_break = "\n"

    stripped_first_line = first_line[:-len(line_break)] if line_break else first_line
    if stripped_first_line.startswith(":) "):
        return content

    lines[0] = f":) {stripped_first_line}{line_break}"
    return "".join(lines)


def _replace_worker_stage_timeout_default(content: str, target_timeout_seconds: float) -> str:
    """Update only the real Settings default for worker_stage_timeout_seconds in config.py."""

    replacement_value = f"{target_timeout_seconds:.1f}" if float(target_timeout_seconds).is_integer() else str(
        target_timeout_seconds
    )
    pattern = re.compile(
        r"(worker_stage_timeout_seconds:\s*float\s*=\s*Field\(\s*default=)([0-9]+(?:\.[0-9]+)?)(,)",
        re.MULTILINE,
    )
    return pattern.sub(rf"\g<1>{replacement_value}\g<3>", content, count=1)


def _replace_worker_stage_timeout_examples(content: str, target_timeout_seconds: int) -> str:
    """Update visible WORKER_STAGE_TIMEOUT_SECONDS examples in README and docs without touching other keys."""

    pattern = re.compile(r"(WORKER_STAGE_TIMEOUT_SECONDS=)([0-9]+(?:\.[0-9]+)?)")
    return pattern.sub(rf"\g<1>{target_timeout_seconds}", content)


def _format_timeout_seconds_for_docs(value: float) -> int:
    """Render timeout values as integer docs examples because the operator-facing env var is shown without decimals."""

    return int(round(value))


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
        "Each item inside `operations` must be one flat operation object. "
        "Do not wrap edits inside shapes like `{\"file\":\"...\",\"changes\":[...]}`. "
        "Do not invent action names such as `validate`, `review`, or `analyze`.\n"
        "If the goal explicitly asks to add, fix, implement, update, change, or handle code, "
        "you must return at least one concrete file operation whenever the provided candidate files make a safe "
        "change possible.\n"
        "If the goal requires NO file changes (e.g. analysis, explanation), return:\n"
        '{"summary": "No code changes needed: <reason>", "operations": []}\n'
        "If you keep operations empty for a concrete coding request, include a short `blocking_reason` that names "
        "one specific candidate file and explains why the change would be unsafe there."
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
        candidate_files,
    )
    target_focus_block = _build_target_focus_block(
        goal=goal,
        requirements=requirements,
        candidate_files=candidate_files,
        file_context=file_context,
    )
    retry_block = [
        "Previous coding attempt returned zero file operations even though this task asked for code changes.",
        f"Previous summary: {previous_plan.get('summary', 'keine Zusammenfassung')}",
        "Approved candidate files you may change:",
        _render_string_list_for_prompt(candidate_files, empty_label="<keine konkreten Kandidaten sichtbar>"),
        "Return at least one concrete file operation when a safe change is possible.",
        "If no safe code change is possible, keep operations empty and add a short "
        "blocking_reason field tied to one specific candidate file.",
        "Do not claim that no target file was provided when candidate files are already listed here.",
        "Do not summarize the repository or offer general help. This is a concrete code-edit task.",
        "Do not answer with prose outside the JSON object.",
    ]
    if target_focus_block:
        retry_block.insert(3, target_focus_block)
    return base_prompt + "\n\n" + "\n".join(retry_block)


def _coding_contract_recovery_user_prompt(
    *,
    goal: str,
    requirements: object,
    candidate_files: list[str],
    file_context: dict[str, str],
    symbol_index_block: str,
    previous_error: str,
) -> str:
    """Create one smaller, highly targeted retry prompt after the shared JSON contract path already failed."""

    compact_requirements = _compact_requirements_for_prompt(requirements)
    target_focus_block = _build_target_focus_block(
        goal=goal,
        requirements=requirements,
        candidate_files=candidate_files,
        file_context=file_context,
    )
    parts = [
        f"Goal:\n{goal}",
        "This is a concrete implementation task with an approved edit scope.",
        f"Requirements:\n{_render_prompt_json(compact_requirements)}",
        "Candidate files you may change:\n"
        + _render_string_list_for_prompt(candidate_files, empty_label="<none>"),
        f"Previous contract failure:\n{_short_text(previous_error, limit=1200)}",
    ]
    if target_focus_block:
        parts.append(target_focus_block)
    if symbol_index_block:
        parts.append(symbol_index_block)
    if file_context:
        parts.append(f"Relevant code excerpts:\n{_render_file_context_for_prompt(file_context)}")
    parts.append(
        "This is a concrete code-edit recovery attempt after a failed structured output. "
        "Choose one of the listed candidate files. "
        "Return at least one concrete operation when a safe change is possible. "
        "If no safe code change is possible, set operations to [] and provide a blocking_reason that names exactly "
        "one listed candidate file and explains why changing it there would be unsafe. "
        "Do not claim that no target file was provided when candidate files are listed above."
    )
    return "\n\n".join(parts)


def _coding_user_prompt(
    goal: str,
    requirements: object,
    architecture: object,
    research: object,
    overview: dict,
    file_context: dict,
    symbol_index_block: str,
    candidate_files: list[str],
) -> str:
    compact_requirements = _compact_requirements_for_prompt(requirements)
    compact_architecture = _compact_architecture_for_prompt(architecture)
    compact_research = _compact_research_for_prompt(research)
    compact_overview = _compact_repo_overview_for_prompt(overview)
    target_focus_block = _build_target_focus_block(
        goal=goal,
        requirements=requirements,
        candidate_files=candidate_files,
        file_context=file_context,
    )
    parts = [
        f"Goal:\n{goal}",
        "Approved change scope:\n"
        "This is a concrete implementation task. The listed candidate files are already approved as the edit scope.",
        f"Requirements:\n{_render_prompt_json(compact_requirements)}",
        f"Architecture and implementation plan:\n{_render_prompt_json(compact_architecture)}",
        f"Research:\n{_render_prompt_json(compact_research)}",
        f"Repo overview:\n{_render_prompt_json(compact_overview)}",
        "Candidate files:\n" + _render_string_list_for_prompt(candidate_files, empty_label="<none>"),
    ]
    if target_focus_block:
        parts.append(target_focus_block)
    if symbol_index_block:
        parts.append(symbol_index_block)
    if file_context:
        parts.append(f"Candidate file contents:\n{_render_file_context_for_prompt(file_context)}")
    parts.append(
        "Important guardrails:\n"
        "- Generate only the minimum changes needed.\n"
        "- Prefer replace_symbol_body for Python functions.\n"
        "- Use create_or_update only if a new file is needed or >50% of the file changes.\n"
        "- This is an implementation task, not a repo summary.\n"
        "- Do not claim that no target file was provided when candidate files are listed above.\n"
        "- Do not claim that no changes are needed when the goal clearly asks for a code change unless you include "
        "a file-specific blocking_reason.\n"
        "- Do not answer with repository praise, project summaries, or generic help offers."
    )
    return "\n\n".join(parts)


def _compact_requirements_for_prompt(requirements: object) -> dict[str, Any] | object:
    """Keep only the requirement fields that help coding make a concrete file change."""

    if not isinstance(requirements, dict):
        return requirements
    return {
        "summary": str(requirements.get("summary") or "").strip(),
        "requirements": _limited_string_list(requirements.get("requirements"), limit=8),
        "acceptance_criteria": _limited_string_list(requirements.get("acceptance_criteria"), limit=6),
        "risks": _limited_string_list(requirements.get("risks"), limit=5),
    }


def _compact_architecture_for_prompt(architecture: object) -> dict[str, Any] | object:
    """Keep architecture context concrete and close to the edit surface."""

    if not isinstance(architecture, dict):
        return architecture
    return {
        "summary": str(architecture.get("summary") or "").strip(),
        "touched_areas": _limited_string_list(architecture.get("touched_areas"), limit=8),
        "implementation_plan": _limited_step_list(architecture.get("implementation_plan"), limit=5),
        "risks": _limited_string_list(architecture.get("risks"), limit=5),
    }


def _compact_research_for_prompt(research: object) -> dict[str, Any] | object:
    """Drop bulky source-routing payloads and keep only actionable research signals for coding."""

    if not isinstance(research, dict):
        return research
    return {
        "candidate_files": _limited_string_list(research.get("candidate_files"), limit=10),
        "research_notes_excerpt": _short_text(research.get("research_notes"), limit=1400),
        "uncertainties": _limited_string_list(research.get("uncertainties"), limit=6),
    }


def _compact_repo_overview_for_prompt(overview: dict[str, Any]) -> dict[str, Any]:
    """Keep repo overview compact so code context stays more important than metadata."""

    return {
        "file_count": overview.get("file_count"),
        "important_files": _limited_string_list(overview.get("important_files"), limit=10),
        "sample_files": _limited_string_list(overview.get("sample_files"), limit=10),
        "git_status": _limited_string_list(overview.get("git_status"), limit=10),
        "last_commit": overview.get("last_commit"),
    }


def _render_prompt_json(value: object) -> str:
    """Render prompt context as readable JSON instead of Python repr strings."""

    if value in (None, "", [], {}):
        return "<none>"
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except TypeError:
        return str(value)


def _render_string_list_for_prompt(items: list[str], *, empty_label: str) -> str:
    """Render one string list as bullet points so models see real paths instead of Python list reprs."""

    if not items:
        return f"- {empty_label}"
    return "\n".join(f"- {item}" for item in items)


def _render_file_context_for_prompt(file_context: dict[str, str]) -> str:
    """Join candidate excerpts into a readable prompt block without escaped Python dict newlines."""

    if not file_context:
        return "<none>"
    ordered_paths = sorted(file_context)
    return "\n\n".join(file_context[path] for path in ordered_paths if file_context[path].strip())


def _build_target_focus_block(
    *,
    goal: str,
    requirements: object,
    candidate_files: list[str],
    file_context: dict[str, str],
) -> str:
    """Highlight the likeliest implementation targets so coding models do not drift into generic blockers."""

    hints = _derive_target_focus_hints(
        goal=goal,
        requirements=requirements,
        candidate_files=candidate_files,
        file_context=file_context,
    )
    if not hints:
        return ""

    lines = ["Likely implementation targets:"]
    for hint in hints:
        matched_terms = ", ".join(hint["matched_terms"]) or "excerpt available"
        symbol_suffix = ""
        if hint["symbols"]:
            symbol_suffix = " Relevant symbols: " + ", ".join(hint["symbols"]) + "."
        lines.append(f"- {hint['file_path']}: {hint['reason']} Matched terms: {matched_terms}.{symbol_suffix}")
    lines.append("Start with the highest-ranked target unless another listed file is clearly safer.")
    return "\n".join(lines)


def _derive_target_focus_hints(
    *,
    goal: str,
    requirements: object,
    candidate_files: list[str],
    file_context: dict[str, str],
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Score candidate files against goal keywords so the prompt can name a concrete primary target."""

    keywords = _prompt_focus_keywords(goal, requirements)
    hints: list[dict[str, Any]] = []
    for index, path in enumerate(candidate_files):
        excerpt = file_context.get(path, "")
        haystack = _normalize_prompt_search_text(f"{path}\n{excerpt}")
        matched_terms = [keyword for keyword in keywords if keyword in haystack][:8]
        score = len(matched_terms)
        if "git" in matched_terms and "clone" in matched_terms:
            score += 4
        if "ensure_repository_checkout" in matched_terms:
            score += 3
        if "_clone_target_from_best_source" in matched_terms:
            score += 3
        if "run_command" in matched_terms:
            score += 2
        if score == 0 and excerpt:
            score = 1
        if score == 0:
            continue
        reason = "This file overlaps strongly with the requested change."
        if "git" in matched_terms and "clone" in matched_terms:
            reason = "This file already contains the clone path that the goal talks about."
        elif "ensure_repository_checkout" in matched_terms:
            reason = "This file contains the checkout helper that likely owns the change."
        elif "_clone_target_from_best_source" in matched_terms:
            reason = "This file contains the helper that directly performs the clone command."
        symbol_candidates = _extract_focus_symbols_from_excerpt(excerpt)
        hints.append(
            {
                "file_path": path,
                "matched_terms": matched_terms,
                "reason": reason,
                "symbols": symbol_candidates[:4],
                "score": score,
                "order": index,
            }
        )

    hints.sort(key=lambda item: (-int(item["score"]), int(item["order"])))
    return hints[:limit]


def _limited_string_list(raw: object, *, limit: int) -> list[str]:
    """Normalize unknown list-like input into a bounded list of short strings."""

    if not isinstance(raw, list):
        return []
    values: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        stripped = item.strip()
        if not stripped:
            continue
        values.append(stripped)
        if len(values) >= limit:
            break
    return values


def _limited_step_list(raw: object, *, limit: int) -> list[dict[str, Any]]:
    """Keep only a few compact implementation-plan steps in the coding prompt."""

    if not isinstance(raw, list):
        return []
    values: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            values.append(
                {
                    "step": item.get("step"),
                    "task": _short_text(item.get("task"), limit=160),
                    "status": item.get("status"),
                }
            )
        elif isinstance(item, str):
            values.append({"task": item.strip()})
        if len(values) >= limit:
            break
    return values


def _short_text(value: object, *, limit: int) -> str:
    """Trim long free-text blocks so the prompt stays focused on editable code."""

    if not isinstance(value, str):
        return ""
    compact = " ".join(value.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def _build_prompt_file_context(
    *,
    repo_path: Path,
    candidate_files: list[str],
    goal: str,
    requirements: object,
    code_index: Any,
) -> dict[str, str]:
    """Build compact file excerpts so coding models see relevant lines instead of whole large files."""

    keywords = _prompt_focus_keywords(goal, requirements)
    excerpts: dict[str, str] = {}
    for path in candidate_files:
        raw_content = read_text_file(repo_path, path, max_bytes=32_000)
        if not raw_content:
            continue
        file_index = code_index.get_file(path) if hasattr(code_index, "get_file") else None
        excerpts[path] = _extract_relevant_file_excerpt(path, raw_content, keywords=keywords, file_index=file_index)
    return excerpts


def _prompt_focus_keywords(goal: str, requirements: object) -> list[str]:
    """Derive stable search terms from the coding goal so prompt excerpts target the real edit location."""

    keywords = {
        "git",
        "clone",
        "checkout",
        "repository",
        "repo",
        "subprocess",
        "error",
        "errors",
        "exception",
        "commanderror",
        "ensure_repository_checkout",
        "run_command",
        "local",
        "patch",
        "backend",
    }

    def _collect(text: str) -> None:
        normalized = _normalize_prompt_search_text(text)
        for token in re.split(r"[^a-z0-9_]+", normalized):
            if token == "git" or len(token) >= 4:
                keywords.add(token)

    _collect(goal)
    if isinstance(requirements, dict):
        for item in requirements.get("requirements", []):
            if isinstance(item, str):
                _collect(item)
    return sorted(keywords)


def _extract_relevant_file_excerpt(
    path: str,
    content: str,
    *,
    keywords: list[str],
    file_index: Any,
    max_excerpt_lines: int = 140,
) -> str:
    """Return line-numbered excerpts around keyword and symbol hits to keep coding prompts focused."""

    lines = content.splitlines()
    if not lines:
        return ""

    normalized_lines = [_normalize_prompt_search_text(line) for line in lines]
    matched_line_numbers = [
        index + 1
        for index, normalized in enumerate(normalized_lines)
        if any(keyword in normalized for keyword in keywords)
    ]

    windows: list[tuple[int, int]] = []
    covered_lines: set[int] = set()

    if file_index is not None and matched_line_numbers:
        for symbol in getattr(file_index, "symbols", []):
            if any(symbol.start_line <= line_no <= symbol.end_line for line_no in matched_line_numbers):
                start = max(1, symbol.start_line - 2)
                end = min(len(lines), symbol.end_line + 2)
                windows.append((start, end))
                covered_lines.update(range(start, end + 1))
                if len(windows) >= 3:
                    break

    for line_no in matched_line_numbers:
        if line_no in covered_lines:
            continue
        start = max(1, line_no - 4)
        end = min(len(lines), line_no + 6)
        windows.append((start, end))
        covered_lines.update(range(start, end + 1))
        if len(windows) >= 6:
            break

    if not windows:
        if len(lines) <= 120:
            windows = [(1, len(lines))]
        else:
            windows = [(1, min(len(lines), 90))]

    merged = _merge_line_windows(windows)
    excerpt_parts = [f"# {path}"]
    consumed_lines = 0
    for start, end in merged:
        excerpt_parts.append(f"[lines {start}-{end}]")
        for line_no in range(start, end + 1):
            excerpt_parts.append(f"{line_no:04d}: {lines[line_no - 1]}")
            consumed_lines += 1
            if consumed_lines >= max_excerpt_lines:
                excerpt_parts.append("... excerpt truncated ...")
                return "\n".join(excerpt_parts)
    return "\n".join(excerpt_parts)


def _merge_line_windows(windows: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping line windows so prompt excerpts stay compact and readable."""

    if not windows:
        return []
    ordered = sorted(windows)
    merged: list[tuple[int, int]] = [ordered[0]]
    for start, end in ordered[1:]:
        current_start, current_end = merged[-1]
        if start <= current_end + 1:
            merged[-1] = (current_start, max(current_end, end))
        else:
            merged.append((start, end))
    return merged


def _extract_focus_symbols_from_excerpt(excerpt: str) -> list[str]:
    """Extract a few likely symbol names from one file excerpt for stronger coding target hints."""

    symbol_matches = re.findall(r"^\d+:\s*(?:async\s+def|def|class)\s+([A-Za-z_][A-Za-z0-9_]*)", excerpt, flags=re.MULTILINE)
    seen: set[str] = set()
    ordered: list[str] = []
    for symbol in symbol_matches:
        if symbol in seen:
            continue
        seen.add(symbol)
        ordered.append(symbol)
    return ordered


def _normalize_prompt_search_text(text: str) -> str:
    """Normalize free text so prompt keyword matching works for English and German snippets alike."""

    normalized = unicodedata.normalize("NFKD", text)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    return ascii_only.lower()


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
        validation_error = validate_raw_operation(raw, index=i)
        if validation_error:
            errors.append(validation_error)
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
