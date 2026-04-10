"""
Purpose: Research worker for repository inspection and optional external-source collection.
Input/Output: Receives a task context and returns structured research notes, sources, and uncertainties.
Important invariants: Repository paths must stay inside the mounted workspace, and external content is treated as untrusted.
How to debug: If research notes look incomplete, inspect the collected repo overview and the list of sampled files first.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from services.shared.agentic_lab.config import get_settings
from services.shared.agentic_lab.guardrails import assess_source_quality, detect_prompt_injection_signals
from services.shared.agentic_lab.llm import LLMClient, LLMError
from services.shared.agentic_lab.logging_utils import TaskLoggerAdapter, configure_logging
from services.shared.agentic_lab.repo_tools import (
    collect_repo_overview,
    ensure_repository_checkout,
    read_text_file,
    write_report,
)
from services.shared.agentic_lab.schemas import Artifact, HealthResponse, WorkerRequest, WorkerResponse

settings = get_settings()
logger = configure_logging(settings.service_name, settings.log_level)
llm = LLMClient(settings)
app = FastAPI(title="Feberdin Research Worker", version="0.1.0")


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(service="research-worker")


@app.post("/run", response_model=WorkerResponse)
async def run(request: WorkerRequest) -> WorkerResponse:
    task_logger = TaskLoggerAdapter(logger.logger, {"service": "research-worker", "task_id": request.task_id})
    repo_path = ensure_repository_checkout(
        repository=request.repository,
        repo_path=Path(request.local_repo_path),
        workspace_root=settings.workspace_root,
        base_branch=request.base_branch,
        repo_url=request.repo_url,
    )
    overview = collect_repo_overview(repo_path)
    sampled_files = overview["important_files"] or overview["sample_files"][:8]
    file_samples = {
        path: read_text_file(repo_path, path)
        for path in sampled_files
        if (repo_path / path).exists() and (repo_path / path).is_file()
    }

    warnings: list[str] = []
    web_sources: list[str] = []
    if request.enable_web_research:
        warnings.append(
            "Web research was requested, but the MVP currently keeps internet lookup disabled unless you attach a dedicated search adapter."
        )

    task_logger.info("Collected repo overview with %s files", overview["file_count"])

    # Why this exists: research notes should be readable even without a working model backend.
    # What happens here: try an LLM summary first, then fall back to a deterministic repo snapshot summary.
    try:
        research_notes = await _summarize_with_llm(request.goal, overview, file_samples)
    except LLMError as exc:
        warnings.append(f"LLM summary unavailable, using heuristic research notes instead: {exc}")
        research_notes = _heuristic_summary(request.goal, overview, file_samples)

    prompt_injection_signals = detect_prompt_injection_signals(research_notes)
    source_quality = {source: assess_source_quality(source) for source in web_sources}

    report_text = _build_report(
        goal=request.goal,
        repository=request.repository,
        notes=research_notes,
        overview=overview,
        sampled_files=sampled_files,
        warnings=warnings,
    )
    report_path = write_report(settings.task_report_dir(request.task_id), "research-notes.md", report_text)

    return WorkerResponse(
        worker="research",
        summary="Repository research completed.",
        outputs={
            "research_notes": research_notes,
            "repo_overview": overview,
            "candidate_files": sampled_files,
            "sources": {
                "repository_files": sampled_files,
                "web_sources": web_sources,
                "source_quality": source_quality,
            },
            "uncertainties": warnings,
            "prompt_injection_signals": prompt_injection_signals,
            "local_repo_path": str(repo_path),
        },
        warnings=warnings,
        risk_flags=(["external_prompt_injection_signal"] if prompt_injection_signals else []),
        artifacts=[
            Artifact(
                name="research-notes",
                path=str(report_path),
                description="Structured repository and architecture research notes.",
            )
        ],
    )


async def _summarize_with_llm(goal: str, overview: dict, file_samples: dict[str, str]) -> str:
    system_prompt = (
        "You are a careful staff engineer performing repository research for an autonomous-but-controlled coding system. "
        "Summarize the architecture, likely change points, risks, and unknowns. "
        "Do not invent files or claim certainty where the repository context is thin."
    )
    user_prompt = (
        f"Goal:\n{goal}\n\n"
        f"Repository overview:\n{overview}\n\n"
        f"Sampled file contents:\n{file_samples}\n\n"
        "Return a concise markdown summary with sections: Architecture, Likely Change Points, Risks, Unknowns."
    )
    return await llm.complete(system_prompt, user_prompt, worker_name="research", max_tokens=1400)


def _heuristic_summary(goal: str, overview: dict, file_samples: dict[str, str]) -> str:
    important_files = ", ".join(overview.get("important_files", [])) or "no key entry files detected"
    sampled_names = ", ".join(file_samples.keys()) or "no sampled files"
    return (
        f"## Architecture\n"
        f"- Repository contains {overview.get('file_count', 0)} files.\n"
        f"- Key entry points detected: {important_files}.\n"
        f"- Sampled files used for context: {sampled_names}.\n\n"
        f"## Likely Change Points\n"
        f"- Start with the files above and adjacent tests or workflow files.\n"
        f"- Reconcile the new goal against the last commit: {overview.get('last_commit', 'unknown')}.\n\n"
        f"## Risks\n"
        f"- Repository context may be incomplete if crucial files were not sampled.\n"
        f"- Dirty git status must be reviewed before automated edits.\n\n"
        f"## Unknowns\n"
        f"- Goal-specific dependencies, deployment contracts, and test expectations should be confirmed during planning.\n"
        f"- Requested change: {goal}\n"
    )


def _build_report(
    *,
    goal: str,
    repository: str,
    notes: str,
    overview: dict,
    sampled_files: list[str],
    warnings: list[str],
) -> str:
    return (
        f"# Research Notes\n\n"
        f"## Goal\n{goal}\n\n"
        f"## Repository\n{repository}\n\n"
        f"## Notes\n{notes}\n\n"
        f"## Repo Overview\n- File count: {overview.get('file_count', 0)}\n"
        f"- Important files: {', '.join(overview.get('important_files', [])) or 'none'}\n"
        f"- Sampled files: {', '.join(sampled_files) or 'none'}\n"
        f"- Last commit: {overview.get('last_commit', 'unknown')}\n\n"
        f"## Warnings\n"
        + ("\n".join(f"- {warning}" for warning in warnings) if warnings else "- None")
        + "\n"
    )
