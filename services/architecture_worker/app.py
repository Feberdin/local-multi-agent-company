"""
Purpose: Architecture worker for solution design, module boundaries, operational concerns, and implementation planning.
Input/Output: Consumes requirements and research outputs and returns a concrete architecture plus a safe implementation plan.
Important invariants: Architecture must remain explicit enough to guide coding and review without encouraging uncontrolled coding sprees.
How to debug: If coding changes feel unstructured, inspect the component map and implementation plan produced here.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from services.shared.agentic_lab.config import get_settings
from services.shared.agentic_lab.llm import LLMClient, LLMError
from services.shared.agentic_lab.logging_utils import TaskLoggerAdapter, configure_logging
from services.shared.agentic_lab.repo_tools import write_report
from services.shared.agentic_lab.schemas import Artifact, HealthResponse, WorkerRequest, WorkerResponse
from services.shared.agentic_lab.task_profiles import is_readme_smiley_profile, is_readme_top_block_profile
from services.shared.agentic_lab.worker_governance import WorkerGovernanceService

settings = get_settings()
logger = configure_logging(settings.service_name, settings.log_level)
llm = LLMClient(settings)
worker_governance = WorkerGovernanceService(settings)
app = FastAPI(title="Feberdin Architecture Worker", version="0.1.0")

ARCHITECTURE_REQUIRED_NON_EMPTY_FIELDS: tuple[str, ...] = (
    "summary",
    "components",
    "responsibilities",
    "data_flows",
    "module_boundaries",
    "deployment_strategy",
    "logging_strategy",
    "implementation_plan",
    "test_strategy",
    "risks",
    "approval_gates",
)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(service="architecture-worker")


@app.post("/run", response_model=WorkerResponse)
async def run(request: WorkerRequest) -> WorkerResponse:
    task_logger = TaskLoggerAdapter(logger.logger, {"service": "architecture-worker", "task_id": request.task_id})
    repo_path = Path(request.local_repo_path)
    requirements = request.prior_results.get("requirements", {}).get("outputs", {})
    research = request.prior_results.get("research", {}).get("outputs", {})
    cost_plan = request.prior_results.get("cost", {}).get("outputs", {})

    if is_readme_smiley_profile(request.metadata):
        outputs = _readme_smiley_architecture(request.goal)
        outputs = _normalize_architecture_outputs(outputs, repo_path, research)
        report_path = write_report(settings.task_report_dir(request.task_id), "architecture.json", outputs)
        return WorkerResponse(
            worker="architecture",
            summary="Architecture and implementation plan prepared.",
            outputs=outputs,
            artifacts=[
                Artifact(
                    name="architecture",
                    path=str(report_path),
                    description="Architecture design, data flows, deployment approach, and implementation plan.",
                )
            ],
        )

    if is_readme_top_block_profile(request.metadata):
        outputs = _readme_top_block_architecture(request.goal)
        outputs = _normalize_architecture_outputs(outputs, repo_path, research)
        report_path = write_report(settings.task_report_dir(request.task_id), "architecture.json", outputs)
        return WorkerResponse(
            worker="architecture",
            summary="Architecture and implementation plan prepared.",
            outputs=outputs,
            artifacts=[
                Artifact(
                    name="architecture",
                    path=str(report_path),
                    description="Architecture design, data flows, deployment approach, and implementation plan.",
                )
            ],
        )

    guidance_block = worker_governance.guidance_prompt_block(request, "architecture")

    system_prompt = _architecture_system_prompt(guidance_block)
    user_prompt = _architecture_user_prompt(
        request.goal,
        requirements=requirements,
        research=research,
        cost_plan=cost_plan,
    )

    try:
        outputs = await llm.complete_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            worker_name="architecture",
            required_keys=_architecture_required_keys(),
        )
    except LLMError as exc:
        task_logger.warning("LLM architecture design unavailable: %s", exc)
        outputs = _heuristic_architecture(request.goal)
    else:
        outputs = await _repair_empty_architecture_fields(
            outputs=outputs,
            request=request,
            task_logger=task_logger,
            system_prompt=system_prompt,
            base_user_prompt=user_prompt,
        )

    outputs = _normalize_architecture_outputs(outputs, repo_path, research)

    report_path = write_report(settings.task_report_dir(request.task_id), "architecture.json", outputs)
    return WorkerResponse(
        worker="architecture",
        summary="Architecture and implementation plan prepared.",
        outputs=outputs,
        artifacts=[
            Artifact(
                name="architecture",
                path=str(report_path),
                description="Architecture design, data flows, deployment approach, and implementation plan.",
            )
        ],
    )


def _heuristic_architecture(goal: str) -> dict:
    return {
        "summary": f"Controlled implementation plan for: {goal}",
        "components": ["orchestrator", "workers", "shared runtime", "github integration", "staging deployment"],
        "responsibilities": {
            "orchestrator": "Route tasks, persist state, enforce approval gates.",
            "workers": "Perform one specialized responsibility each and report outputs explicitly.",
        },
        "data_flows": [
            "Auftrag -> requirements -> research -> architecture -> coding -> review -> testing -> validation -> github"
        ],
        "module_boundaries": [
            "Shared code contains contracts, policy, routing, and repo helpers.",
            "Worker services remain small and independently replaceable.",
        ],
        "touched_areas": ["README.md", "services/", "tests/", "docker-compose.yml", "config/"],
        "deployment_strategy": ["Containerized services on Unraid with staging-only deployment by default."],
        "logging_strategy": ["Structured logs with task IDs and masked sensitive markers."],
        "implementation_plan": [
            "Implement the smallest safe change set first.",
            "Keep docs, tests, and deployment notes close to the changed behavior.",
        ],
        "test_strategy": ["Lint, typing, unit tests, then staging smoke checks if deployment is enabled."],
        "risks": ["Repository-specific assumptions may still need manual confirmation."],
        "approval_gates": [
            "Infrastructure changes",
            "Secret-related changes",
            "Destructive actions",
            "Production deployment",
        ],
    }


def _readme_smiley_architecture(goal: str) -> dict[str, Any]:
    """Return a fully non-empty architecture package for the trivial README smiley fast path."""

    return {
        "summary": f"Minimaler README-Einzeilenfix fuer: {goal}",
        "components": [
            {
                "name": "README.md",
                "type": "documentation",
                "description": "Operator-facing project overview at the repository root.",
            }
        ],
        "responsibilities": {
            "README.md": "Carries the requested visible text change and is the only allowed edit target."
        },
        "data_flows": ["Goal -> coding -> README.md first line update -> validation of the resulting diff."],
        "module_boundaries": ["Only README.md may change.", "No source code, tests, CI, or deployment files are in scope."],
        "touched_areas": ["README.md"],
        "deployment_strategy": ["No deployment changes are required for this documentation-only patch."],
        "logging_strategy": ["Normal task-level logs are sufficient; no new runtime logging is needed."],
        "implementation_plan": [
            "Open README.md in the task-local workspace.",
            "Prefix the first line with `:) ` exactly once.",
            "Leave all remaining lines unchanged and avoid any second file edit.",
        ],
        "test_strategy": [
            "Verify that only README.md appears in the diff.",
            "Verify that the first README line now begins with `:) `.",
        ],
        "risks": ["Do not rewrite the full document unnecessarily.", "Do not create or touch any additional files."],
        "approval_gates": ["No additional approval gate is needed beyond normal repository-modification approval."],
    }


def _readme_top_block_architecture(goal: str) -> dict[str, Any]:
    """Return a fully non-empty architecture package for a small README top-block task."""

    return {
        "summary": f"Minimaler README-Block-Fix fuer: {goal}",
        "components": [
            {
                "name": "README.md",
                "type": "documentation",
                "description": "Operator-facing project overview at the repository root.",
            }
        ],
        "responsibilities": {
            "README.md": "Carries the requested top-of-file markdown block and remains the only allowed edit target."
        },
        "data_flows": ["Goal -> coding -> README.md block insertion at file top -> validation of the resulting diff."],
        "module_boundaries": ["Only README.md may change.", "No source code, CI, tests, or deployment files are in scope."],
        "touched_areas": ["README.md"],
        "deployment_strategy": ["No deployment changes are required for this documentation-only patch."],
        "logging_strategy": ["Normal task-level logs are sufficient; no new runtime logging is needed."],
        "implementation_plan": [
            "Open README.md in the task-local workspace.",
            "Insert a short markdown block at the top of the file.",
            "Keep the rest of the README stable and avoid any second file edit.",
        ],
        "test_strategy": [
            "Verify that only README.md appears in the diff.",
            "Verify that the new markdown block is visible at the top of README.md.",
        ],
        "risks": ["Do not rewrite the full document unnecessarily.", "Do not create or touch any additional files."],
        "approval_gates": ["No additional approval gate is needed beyond normal repository-modification approval."],
    }


def _architecture_required_keys() -> list[str]:
    """Keep the raw JSON contract in one place so retries and tests stay aligned."""

    return [
        "summary",
        "components",
        "responsibilities",
        "data_flows",
        "module_boundaries",
        "deployment_strategy",
        "logging_strategy",
        "implementation_plan",
        "test_strategy",
        "risks",
        "approval_gates",
        "touched_areas",
    ]


def _architecture_system_prompt(guidance_block: str) -> str:
    """Centralize the architecture contract so retries do not drift from the main request."""

    return (
        "You are a staff-plus architect. Return JSON with keys summary, components, responsibilities, "
        "data_flows, module_boundaries, deployment_strategy, logging_strategy, implementation_plan, "
        "test_strategy, risks, approval_gates, touched_areas.\n"
        "CRITICAL: touched_areas must be a list of actual relative file paths that need to be modified "
        "(e.g. ['services/coding_worker/app.py', 'services/shared/agentic_lab/repo_tools.py']). "
        "Use the research candidate_files and repository file listing to identify the exact source files. "
        "NEVER use generic descriptions like 'Backend-Infrastruktur' or directory names — only real relative file paths."
        f"{guidance_block}"
    )


def _architecture_user_prompt(
    goal: str,
    *,
    requirements: object,
    research: object,
    cost_plan: object,
) -> str:
    """Build the baseline architecture prompt so validation retries can extend the same context."""

    return (
        f"Goal:\n{goal}\n\n"
        f"Requirements:\n{requirements}\n\n"
        f"Research:\n{research}\n\n"
        f"Resource plan:\n{cost_plan}\n\n"
        "Design a practical implementation for a local-first, reviewable system. "
        "For touched_areas, look at the research results and list the specific source files to edit."
    )


async def _repair_empty_architecture_fields(
    *,
    outputs: dict[str, Any],
    request: WorkerRequest,
    task_logger: TaskLoggerAdapter,
    system_prompt: str,
    base_user_prompt: str,
) -> dict[str, Any]:
    """
    Reject semantically empty architecture answers before they poison the coding handoff.

    Example:
      summary = ""
      components = []
      implementation_plan = ""
    must trigger one explicit retry instead of silently passing as a successful architecture stage.
    """

    empty_fields = _empty_architecture_fields(outputs)
    if not empty_fields:
        return outputs

    task_logger.warning(
        "Architecture response had empty required fields: %s",
        ", ".join(empty_fields),
    )
    retry_prompt = _architecture_empty_field_retry_prompt(base_user_prompt, empty_fields)
    try:
        repaired = await llm.complete_json(
            system_prompt=system_prompt,
            user_prompt=retry_prompt,
            worker_name="architecture",
            required_keys=_architecture_required_keys(),
        )
    except LLMError as exc:
        task_logger.warning("Architecture retry failed after empty-field validation: %s", exc)
        return _merge_missing_architecture_fields(outputs, _heuristic_architecture(request.goal))

    repaired_empty_fields = _empty_architecture_fields(repaired)
    if not repaired_empty_fields:
        return repaired

    task_logger.warning(
        "Architecture retry still left empty required fields: %s",
        ", ".join(repaired_empty_fields),
    )
    return _merge_missing_architecture_fields(repaired, _merge_missing_architecture_fields(outputs, _heuristic_architecture(request.goal)))


def _architecture_empty_field_retry_prompt(base_user_prompt: str, empty_fields: list[str]) -> str:
    """Tell the model exactly why the previous architecture answer was rejected."""

    return (
        f"{base_user_prompt}\n\n"
        "The previous architecture answer was rejected because these required fields were empty or semantically blank:\n"
        f"{', '.join(empty_fields)}\n\n"
        "Return the full JSON again. None of those required fields may be empty.\n"
        "If details are uncertain, provide the smallest concrete non-empty value that still helps downstream coding.\n"
        "Do not return empty strings, empty lists, or placeholders like 'TBD'."
    )


def _merge_missing_architecture_fields(primary: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    """Fill only missing semantic gaps so we keep any useful specific data from the model output."""

    merged = dict(primary)
    for key, fallback_value in fallback.items():
        if not _architecture_value_has_content(merged.get(key)):
            merged[key] = fallback_value
    return merged


def _empty_architecture_fields(outputs: dict[str, Any]) -> list[str]:
    """Return required keys whose values are present but still semantically empty."""

    empty_fields: list[str] = []
    for field_name in ARCHITECTURE_REQUIRED_NON_EMPTY_FIELDS:
        if not _architecture_value_has_content(outputs.get(field_name)):
            empty_fields.append(field_name)
    return empty_fields


def _architecture_value_has_content(value: object) -> bool:
    """Treat blank strings, empty lists, and nested blank payloads as missing content."""

    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(_architecture_value_has_content(item) for item in value.values())
    if isinstance(value, list):
        return any(_architecture_value_has_content(item) for item in value)
    return True


def _normalize_architecture_outputs(outputs: dict, repo_path: Path, research: object) -> dict:
    """
    Keep touched_areas grounded in real files so downstream workers do not inherit hallucinated paths.

    Example:
      Input touched_areas:
        ["services/coding_worker/app.py", "services/coding_worker/task_dispatcher.py"]
      Output touched_areas:
        ["services/coding_worker/app.py", "services/shared/agentic_lab/repo_tools.py"]
      when only the first file exists and research suggested the second one.
    """

    normalized = dict(outputs)
    raw_touched = normalized.get("touched_areas", [])
    research_outputs = research if isinstance(research, dict) else {}
    research_candidates = _existing_repo_files(research_outputs.get("candidate_files", []), repo_path)
    existing_touched = _existing_repo_files(raw_touched, repo_path)

    # Why this exists:
    # Architecture answers occasionally mention the correct target in the summary/components,
    # but still emit generic touched_areas like README.md or docker-compose.yml. Downstream
    # coding then trusts the wrong files and loses the real implementation target.
    ranked_touched = _rank_architecture_touched_areas(
        outputs=normalized,
        repo_path=repo_path,
        research_outputs=research_outputs,
        existing_touched=existing_touched,
        research_candidates=research_candidates,
    )

    if ranked_touched:
        normalized["touched_areas"] = ranked_touched

    return normalized


def _existing_repo_files(raw_paths: object, repo_path: Path) -> list[str]:
    """Keep only unique existing files from a mixed worker payload."""

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


def _rank_architecture_touched_areas(
    *,
    outputs: dict[str, Any],
    repo_path: Path,
    research_outputs: dict[str, Any],
    existing_touched: list[str],
    research_candidates: list[str],
) -> list[str]:
    """
    Rank touched areas by semantic fit so concrete source files win over generic repo metadata.

    Example:
      summary references ensure_repository_checkout in repo_tools.py
      existing touched_areas contain README.md and docker-compose.yml
      research suggests services/shared/agentic_lab/repo_tools.py
      => repo_tools.py must rise to the top.
    """

    candidate_pool = _merge_unique_paths(research_candidates, existing_touched)
    if not candidate_pool:
        return []

    architecture_text = _normalize_search_text(_flatten_text(outputs))
    research_text = _normalize_search_text(_flatten_text(research_outputs))
    has_specific_source_candidate = any(_looks_like_source_file(path) for path in candidate_pool)
    ranked: list[tuple[int, int, str]] = []
    for index, path in enumerate(candidate_pool):
        score = 0
        normalized_path = _normalize_search_text(path)
        path_terms = _path_terms(path)
        basename_terms = _path_terms(Path(path).stem)

        if normalized_path and normalized_path in architecture_text:
            score += 18

        for term in basename_terms:
            if term in architecture_text:
                score += 7

        for term in path_terms:
            if term in architecture_text:
                score += 3
            if term in research_text:
                score += 2

        if path in research_candidates:
            score += 10
        if path in existing_touched:
            score += 5
        if _looks_like_source_file(path):
            score += 4
        if _is_generic_repo_metadata_path(path) and has_specific_source_candidate:
            score -= 9
        if path.startswith("tests/") and has_specific_source_candidate:
            score -= 2

        preview = _safe_read_preview(repo_path / path)
        preview_text = _normalize_search_text(preview)
        if "git" in preview_text and "clone" in preview_text:
            score += 6
        if "ensure_repository_checkout" in preview_text:
            score += 5
        if "_clone_target_from_best_source" in preview_text:
            score += 4

        ranked.append((score, -index, path))

    ranked.sort(key=lambda item: (-item[0], item[1], item[2]))
    return [path for _, _, path in ranked[:6]]


def _merge_unique_paths(*groups: list[str]) -> list[str]:
    """Preserve order while merging candidate path lists."""

    merged: list[str] = []
    for group in groups:
        for path in group:
            if path not in merged:
                merged.append(path)
    return merged


def _flatten_text(value: object) -> str:
    """Collect free text recursively so we can compare file paths against the full architecture intent."""

    parts: list[str] = []

    def _collect(item: object) -> None:
        if isinstance(item, str):
            if item.strip():
                parts.append(item.strip())
            return
        if isinstance(item, dict):
            for nested in item.values():
                _collect(nested)
            return
        if isinstance(item, list):
            for nested in item:
                _collect(nested)

    _collect(value)
    return "\n".join(parts)


def _normalize_search_text(value: str) -> str:
    """Normalize mixed English/German worker text into a stable lowercase search string."""

    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", normalized.lower()).strip()


def _path_terms(path: str) -> list[str]:
    """Extract useful search terms from file paths without generic filler words."""

    raw_terms = re.split(r"[^a-z0-9_]+", _normalize_search_text(path))
    ignore = {"services", "shared", "agentic_lab", "tests", "unit", "app", "main", "file"}
    terms: list[str] = []
    for term in raw_terms:
        if len(term) < 4 or term in ignore:
            continue
        if term not in terms:
            terms.append(term)
    return terms


def _looks_like_source_file(path: str) -> bool:
    """Prefer concrete source files over repo metadata when both are present."""

    return Path(path).suffix.lower() in {".py", ".sh", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs"}


def _is_generic_repo_metadata_path(path: str) -> bool:
    """Recognize broad repo-level files that often drown out the real implementation target."""

    normalized = path.strip().lower()
    return normalized in {
        "readme.md",
        "docker-compose.yml",
        "docker-compose.yaml",
        "pyproject.toml",
        ".github/workflows/ci.yml",
        ".github/workflows/ci.yaml",
    }


def _safe_read_preview(path: Path, max_chars: int = 6000) -> str:
    """Read a small preview for ranking without letting one large file dominate the decision."""

    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:max_chars]
    except OSError:
        return ""
