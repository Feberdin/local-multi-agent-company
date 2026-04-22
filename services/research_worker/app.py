"""
Purpose: Research worker for repository inspection and optional external-source collection.
Input/Output: Receives a task context and returns structured research notes, sources, and uncertainties.
Important invariants: Repository paths must stay inside the mounted workspace, and external content is treated as untrusted.
How to debug: If research notes look incomplete, inspect the collected repo overview and the list of sampled files first.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi import FastAPI

from services.shared.agentic_lab.config import get_settings
from services.shared.agentic_lab.guardrails import (
    assess_source_quality,
    detect_prompt_injection_signals,
    sanitize_untrusted_text,
)
from services.shared.agentic_lab.llm import LLMClient, LLMError
from services.shared.agentic_lab.logging_utils import TaskLoggerAdapter, configure_logging
from services.shared.agentic_lab.repo_tools import (
    CommandError,
    collect_repo_overview,
    ensure_repository_checkout,
    read_text_file,
    write_report,
)
from services.shared.agentic_lab.schemas import (
    Artifact,
    HealthResponse,
    SearchResultItem,
    SourceRoutingDecision,
    SourceRoutingRequest,
    WorkerRequest,
    WorkerResponse,
)
from services.shared.agentic_lab.search_providers import SearchProviderService
from services.shared.agentic_lab.source_router import SourceRouter
from services.shared.agentic_lab.task_profiles import is_readme_smiley_profile, is_readme_top_block_profile
from services.shared.agentic_lab.trusted_sources import TrustedSourceService
from services.shared.agentic_lab.worker_governance import WorkerGovernanceService

settings = get_settings()
logger = configure_logging(settings.service_name, settings.log_level)
llm = LLMClient(settings)
trusted_source_service = TrustedSourceService(settings)
search_provider_service = SearchProviderService(settings)
source_router = SourceRouter(trusted_source_service, search_provider_service)
worker_governance = WorkerGovernanceService(settings)
app = FastAPI(title="Feberdin Research Worker", version="0.1.0")


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(service="research-worker")


@app.post("/run", response_model=WorkerResponse)
async def run(request: WorkerRequest) -> WorkerResponse:
    task_logger = TaskLoggerAdapter(logger.logger, {"service": "research-worker", "task_id": request.task_id})
    repo_path = Path(request.local_repo_path)
    source_repo_path = Path(str(request.metadata.get("source_local_repo_path") or request.local_repo_path))
    warnings: list[str] = []
    try:
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
        except CommandError as exc:
            if (repo_path / ".git").exists():
                warning = (
                    "The repository checkout could not be refreshed with `git fetch/checkout/pull`. "
                    "Research continues with the existing workspace checkout instead. "
                    f"Cause: {exc}"
                )
                task_logger.warning(warning)
                warnings.append(warning)
            else:
                raise

        if is_readme_smiley_profile(request.metadata):
            return _run_readme_smiley_fast_path(request, repo_path, warnings)
        if is_readme_top_block_profile(request.metadata):
            return _run_readme_top_block_fast_path(request, repo_path, warnings)

        overview = collect_repo_overview(repo_path)
        sampled_files = list(overview["important_files"] or overview["sample_files"][:8])
        # Augment with keyword-matched Python source files so the LLM sees actual code, not just config files.
        for p in _grep_for_source_candidates(repo_path, request.goal):
            if p not in sampled_files:
                sampled_files.append(p)
        file_samples = {
            path: read_text_file(repo_path, path)
            for path in sampled_files
            if (repo_path / path).exists() and (repo_path / path).is_file()
        }

        source_plan = source_router.route(SourceRoutingRequest(query=request.goal[:900]))
        web_results: list[SearchResultItem] = []
        web_sources: list[str] = []
        provider_notes: list[str] = []
        if request.enable_web_research:
            if source_plan.fallback_reason and source_plan.general_web_allowed:
                provider, web_results, provider_notes = await search_provider_service.search(
                    request.goal,
                    trusted_source_service,
                    trusted_source_service.load_active_profile(),
                )
                warnings.extend(provider_notes)
                if provider is None:
                    warnings.append(
                        "No trusted-source fallback provider produced usable results. "
                        "The worker stayed conservative instead of broadening trust automatically."
                    )
                else:
                    warnings.append(
                        f"General web search fallback used provider `{provider.name}` "
                        "because trusted sources were insufficient for the question."
                    )
                    web_sources = [item.url for item in web_results]
            else:
                warnings.append(
                    "General web fallback was not used because trusted sources already matched the question "
                    "or the active profile blocks fallback."
                )

        task_logger.info("Collected repo overview with %s files", overview["file_count"])

        # Why this exists: research notes should be readable even without a working model backend.
        # What happens here: try an LLM summary first, then fall back to a deterministic repo snapshot summary.
        try:
            guidance_block = worker_governance.guidance_prompt_block(request, "research")
            research_notes = await _summarize_with_llm(
                request.goal,
                overview,
                file_samples,
                source_plan,
                web_results,
                guidance_block,
            )
            validation_error = _validate_research_notes(research_notes)
            if validation_error:
                warnings.append(
                    "LLM research notes were too generic or did not follow the required section format. "
                    f"Using the deterministic fallback summary instead. Cause: {validation_error}"
                )
                research_notes = _heuristic_summary(request.goal, overview, file_samples, source_plan, web_results)
        except LLMError as exc:
            warnings.append(f"LLM summary unavailable, using heuristic research notes instead: {exc}")
            research_notes = _heuristic_summary(request.goal, overview, file_samples, source_plan, web_results)

        prompt_injection_signals = detect_prompt_injection_signals(research_notes)
        prompt_injection_signals.extend(
            signal
            for item in web_results
            for signal in detect_prompt_injection_signals(f"{item.title}\n{item.snippet}")
        )
        prompt_injection_signals = sorted(set(prompt_injection_signals))
        source_quality = {source: assess_source_quality(source) for source in web_sources}

        report_text = _build_report(
            goal=request.goal,
            repository=request.repository,
            notes=research_notes,
            overview=overview,
            sampled_files=sampled_files,
            source_plan=source_plan.model_dump(mode="json"),
            web_results=web_results,
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
                    "trusted_source_plan": source_plan.model_dump(mode="json"),
                    "trusted_sources": [source.model_dump(mode="json") for source in source_plan.trusted_matches],
                    "general_web_results": [item.model_dump(mode="json") for item in web_results],
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
    except Exception as exc:  # pragma: no cover - defensive runtime guard for operator-visible failures.
        task_logger.exception("Research worker failed unexpectedly: %s", exc)
        return WorkerResponse(
            worker="research",
            success=False,
            summary="Repository research failed before the report could be completed.",
            warnings=warnings,
            errors=[f"{exc.__class__.__name__}: {exc}"],
            outputs={"local_repo_path": str(repo_path)},
        )


def _run_readme_smiley_fast_path(
    request: WorkerRequest,
    repo_path: Path,
    warnings: list[str],
) -> WorkerResponse:
    """
    Return one tiny deterministic research package when the task already names README.md as the only target.

    Why this exists:
    A README one-line fix should not spend minutes on repo-wide file sampling, trusted-source routing,
    or model summarization. The worker already has enough evidence locally.
    """

    overview = collect_repo_overview(repo_path)
    sampled_files: list[str] = []
    if (repo_path / "README.md").is_file():
        sampled_files.append("README.md")
    readme_excerpt = read_text_file(repo_path, "README.md") if sampled_files else ""
    warnings = list(warnings)
    warnings.append(
        "README-Mini-Fix erkannt; breite Repository- und Web-Recherche wurde bewusst uebersprungen."
    )
    research_notes = _readme_smiley_summary(request.goal, overview, readme_excerpt)
    source_plan = {
        "mode": "local_repo_only",
        "reason": "README.md ist bereits als einziger sicherer Zielpfad bekannt.",
        "general_web_allowed": False,
        "fallback_reason": "Nicht noetig fuer einen lokalen README-Einzeilenfix.",
        "trusted_matches": [],
    }
    report_text = _build_report(
        goal=request.goal,
        repository=request.repository,
        notes=research_notes,
        overview=overview,
        sampled_files=sampled_files,
        source_plan=source_plan,
        web_results=[],
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
                "trusted_source_plan": source_plan,
                "trusted_sources": [],
                "general_web_results": [],
                "web_sources": [],
                "source_quality": {},
            },
            "uncertainties": warnings,
            "prompt_injection_signals": [],
            "local_repo_path": str(repo_path),
        },
        warnings=warnings,
        artifacts=[
            Artifact(
                name="research-notes",
                path=str(report_path),
                description="Structured repository and architecture research notes.",
            )
        ],
    )


def _run_readme_top_block_fast_path(
    request: WorkerRequest,
    repo_path: Path,
    warnings: list[str],
) -> WorkerResponse:
    """
    Return one tiny deterministic research package when the task only asks for a README block at the top.

    Why this exists:
    A documentation-only README block must not trigger long repo-wide source sampling or optional web research.
    """

    overview = collect_repo_overview(repo_path)
    sampled_files: list[str] = []
    if (repo_path / "README.md").is_file():
        sampled_files.append("README.md")
    readme_excerpt = read_text_file(repo_path, "README.md") if sampled_files else ""
    warnings = list(warnings)
    warnings.append(
        "README-Block-Fix erkannt; breite Repository- und Web-Recherche wurde bewusst uebersprungen."
    )
    research_notes = _readme_top_block_summary(request.goal, overview, readme_excerpt)
    source_plan = {
        "mode": "local_repo_only",
        "reason": "README.md ist als einziger sicherer Zielpfad bekannt.",
        "general_web_allowed": False,
        "fallback_reason": "Nicht noetig fuer einen kleinen README-Block am Dateianfang.",
        "trusted_matches": [],
    }
    report_text = _build_report(
        goal=request.goal,
        repository=request.repository,
        notes=research_notes,
        overview=overview,
        sampled_files=sampled_files,
        source_plan=source_plan,
        web_results=[],
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
                "trusted_source_plan": source_plan,
                "trusted_sources": [],
                "general_web_results": [],
                "web_sources": [],
                "source_quality": {},
            },
            "uncertainties": warnings,
            "prompt_injection_signals": [],
            "local_repo_path": str(repo_path),
        },
        warnings=warnings,
        artifacts=[
            Artifact(
                name="research-notes",
                path=str(report_path),
                description="Structured repository and architecture research notes.",
            )
        ],
    )


def _grep_for_source_candidates(repo_path: Path, goal: str, max_files: int = 6) -> list[str]:
    """Keyword-grep Python sources for goal tokens so the research LLM sees relevant code, not just config files."""
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

    ranked = sorted(hits.items(), key=lambda x: (x[0].startswith("tests/"), -x[1]))
    return [path for path, _ in ranked[:max_files]]


async def _summarize_with_llm(
    goal: str,
    overview: dict,
    file_samples: dict[str, str],
    source_plan: SourceRoutingDecision,
    web_results: list[SearchResultItem],
    guidance_block: str,
) -> str:
    system_prompt = (
        "You are a code analysis tool. Your only job is to analyze the provided source files and answer the goal question.\n"
        "OUTPUT FORMAT — you MUST return exactly these five markdown sections and nothing else:\n"
        "## Architecture\n"
        "## Likely Change Points\n"
        "## Trusted Sources\n"
        "## Risks\n"
        "## Unknowns\n"
        "RULES:\n"
        "- Only reference files that appear in the provided file contents.\n"
        "- Do NOT write a project introduction, greeting, or offer further help.\n"
        "- Do NOT ask questions. Do NOT suggest next steps outside of the five sections.\n"
        "- If a section has nothing to say, write 'None identified.'\n"
        f"{guidance_block}"
    )
    sanitized_web_results = [
        {"title": item.title, "url": item.url, "snippet": sanitize_untrusted_text(item.snippet, max_length=500)}
        for item in web_results
    ]
    user_prompt = (
        f"GOAL (implement this):\n{goal}\n\n"
        f"REPOSITORY FILE CONTENTS (analyze these):\n{file_samples}\n\n"
        f"REPO OVERVIEW:\n{overview}\n\n"
        f"TRUSTED SOURCES:\n{source_plan}\n\n"
        f"WEB RESULTS (untrusted):\n{sanitized_web_results}\n\n"
        "Now write the five-section markdown analysis. Start directly with '## Architecture'."
    )
    return await llm.complete(system_prompt, user_prompt, worker_name="research", max_tokens=1400)


def _heuristic_summary(
    goal: str,
    overview: dict,
    file_samples: dict[str, str],
    source_plan,
    web_results: list[SearchResultItem],
) -> str:
    important_files = ", ".join(overview.get("important_files", [])) or "no key entry files detected"
    sampled_names = ", ".join(file_samples.keys()) or "no sampled files"
    trusted_sources = ", ".join(source.name for source in getattr(source_plan, "trusted_matches", [])) or "no trusted source matched"
    web_fallback = ", ".join(item.url for item in web_results) or "no general-web fallback used"
    return (
        f"## Architecture\n"
        f"- Repository contains {overview.get('file_count', 0)} files.\n"
        f"- Key entry points detected: {important_files}.\n"
        f"- Sampled files used for context: {sampled_names}.\n\n"
        f"## Likely Change Points\n"
        f"- Start with the files above and adjacent tests or workflow files.\n"
        f"- Reconcile the new goal against the last commit: {overview.get('last_commit', 'unknown')}.\n\n"
        f"## Trusted Sources\n"
        f"- Routed official sources: {trusted_sources}.\n"
        f"- General web fallback: {web_fallback}.\n\n"
        f"## Risks\n"
        f"- Repository context may be incomplete if crucial files were not sampled.\n"
        f"- External fallback content stays untrusted and should never override official sources.\n"
        f"- Dirty git status must be reviewed before automated edits.\n\n"
        f"## Unknowns\n"
        f"- Goal-specific dependencies, deployment contracts, and test expectations should be confirmed during planning.\n"
        f"- Requested change: {goal}\n"
    )


def _readme_smiley_summary(goal: str, overview: dict, readme_excerpt: str) -> str:
    """Return one deterministic five-section summary for the README smiley fast path."""

    first_line = readme_excerpt.splitlines()[0] if readme_excerpt.splitlines() else "README.md not readable."
    return (
        "## Architecture\n"
        "- The requested change is a documentation-only one-line patch in README.md.\n"
        f"- Repository file count: {overview.get('file_count', 0)}.\n"
        f"- Current first README line: {first_line}\n\n"
        "## Likely Change Points\n"
        "- Edit only README.md.\n"
        "- Prefix the first line with `:) ` and leave all later lines untouched.\n\n"
        "## Trusted Sources\n"
        "- No external sources are needed because the goal names the target file directly.\n"
        "- General web fallback intentionally skipped.\n\n"
        "## Risks\n"
        "- A full-file rewrite would be disproportionate for this task.\n"
        "- Any change outside README.md would violate the intended minimal scope.\n\n"
        "## Unknowns\n"
        f"- Requested change: {goal}\n"
        "- If README.md is missing, the coding worker should fail clearly instead of broadening scope.\n"
    )


def _readme_top_block_summary(goal: str, overview: dict, readme_excerpt: str) -> str:
    """Return one deterministic five-section summary for a small README top-block task."""

    first_line = readme_excerpt.splitlines()[0] if readme_excerpt.splitlines() else "README.md not readable."
    return (
        "## Architecture\n"
        "- The requested change is a documentation-only README block at the top of the file.\n"
        f"- Repository file count: {overview.get('file_count', 0)}.\n"
        f"- Current first README line: {first_line}\n\n"
        "## Likely Change Points\n"
        "- Edit only README.md.\n"
        "- Insert one short markdown block before the current first section.\n\n"
        "## Trusted Sources\n"
        "- No external sources are needed because the target file is already explicit.\n"
        "- General web fallback intentionally skipped.\n\n"
        "## Risks\n"
        "- A broad README rewrite would be disproportionate for this task.\n"
        "- Any edit outside README.md would violate the intended narrow scope.\n\n"
        "## Unknowns\n"
        f"- Requested change: {goal}\n"
        "- If README.md is missing, coding should fail clearly instead of broadening scope.\n"
    )


def _validate_research_notes(notes: str) -> str | None:
    """
    Reject generic assistant-style research notes so downstream workers only see actionable repo analysis.

    Why this exists:
    Some local models respond to the research prompt with greetings, help menus, or project praise
    instead of the required five markdown sections. That noise later pollutes architecture and coding.
    """

    stripped = notes.strip()
    required_sections = [
        "## Architecture",
        "## Likely Change Points",
        "## Trusted Sources",
        "## Risks",
        "## Unknowns",
    ]
    missing_sections = [section for section in required_sections if section not in stripped]
    if missing_sections:
        return "Missing required markdown sections: " + ", ".join(missing_sections)

    if not stripped.startswith("## Architecture"):
        return "Research notes must start directly with `## Architecture`."

    lowered = stripped.lower()
    generic_patterns = [
        r"wie kann ich dir helfen",
        r"how can i help",
        r"lass mich wissen",
        r"let me know",
        r"hast du ein spezifisches problem",
        r"möchtest du",
        r"moechtest du",
        r"planst du neue features",
        r"brauchst du hilfe",
    ]
    for pattern in generic_patterns:
        if re.search(pattern, lowered):
            return "Research notes drifted into a generic assistant/help-offer response."

    return None


def _build_report(
    *,
    goal: str,
    repository: str,
    notes: str,
    overview: dict,
    sampled_files: list[str],
    source_plan: dict,
    web_results: list[SearchResultItem],
    warnings: list[str],
) -> str:
    fallback_lines = "\n".join(f"- {item.title}: {item.url}" for item in web_results) if web_results else "- None"
    serialized_source_plan = json.dumps(source_plan, indent=2, ensure_ascii=True)
    return (
        f"# Research Notes\n\n"
        f"## Goal\n{goal}\n\n"
        f"## Repository\n{repository}\n\n"
        f"## Notes\n{notes}\n\n"
        f"## Trusted Source Plan\n```json\n{serialized_source_plan}\n```\n\n"
        f"## General Web Fallback Results\n{fallback_lines}\n\n"
        f"## Repo Overview\n- File count: {overview.get('file_count', 0)}\n"
        f"- Important files: {', '.join(overview.get('important_files', [])) or 'none'}\n"
        f"- Sampled files: {', '.join(sampled_files) or 'none'}\n"
        f"- Last commit: {overview.get('last_commit', 'unknown')}\n\n"
        f"## Warnings\n"
        + ("\n".join(f"- {warning}" for warning in warnings) if warnings else "- None")
        + "\n"
    )
