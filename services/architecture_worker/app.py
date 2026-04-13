"""
Purpose: Architecture worker for solution design, module boundaries, operational concerns, and implementation planning.
Input/Output: Consumes requirements and research outputs and returns a concrete architecture plus a safe implementation plan.
Important invariants: Architecture must remain explicit enough to guide coding and review without encouraging uncontrolled coding sprees.
How to debug: If coding changes feel unstructured, inspect the component map and implementation plan produced here.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from services.shared.agentic_lab.config import get_settings
from services.shared.agentic_lab.llm import LLMClient, LLMError
from services.shared.agentic_lab.logging_utils import TaskLoggerAdapter, configure_logging
from services.shared.agentic_lab.repo_tools import write_report
from services.shared.agentic_lab.schemas import Artifact, HealthResponse, WorkerRequest, WorkerResponse
from services.shared.agentic_lab.worker_governance import WorkerGovernanceService

settings = get_settings()
logger = configure_logging(settings.service_name, settings.log_level)
llm = LLMClient(settings)
worker_governance = WorkerGovernanceService(settings)
app = FastAPI(title="Feberdin Architecture Worker", version="0.1.0")


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
    guidance_block = worker_governance.guidance_prompt_block(request, "architecture")

    try:
        outputs = await llm.complete_json(
            system_prompt=(
                "You are a staff-plus architect. Return JSON with keys summary, components, responsibilities, "
                "data_flows, module_boundaries, deployment_strategy, logging_strategy, implementation_plan, "
                "test_strategy, risks, approval_gates, touched_areas.\n"
                "CRITICAL: touched_areas must be a list of actual relative file paths that need to be modified "
                "(e.g. ['services/coding_worker/app.py', 'services/shared/agentic_lab/repo_tools.py']). "
                "Use the research candidate_files and repository file listing to identify the exact source files. "
                "NEVER use generic descriptions like 'Backend-Infrastruktur' or directory names — only real relative file paths."
                f"{guidance_block}"
            ),
            user_prompt=(
                f"Goal:\n{request.goal}\n\n"
                f"Requirements:\n{requirements}\n\n"
                f"Research:\n{research}\n\n"
                f"Resource plan:\n{cost_plan}\n\n"
                "Design a practical implementation for a local-first, reviewable system. "
                "For touched_areas, look at the research results and list the specific source files to edit."
            ),
            worker_name="architecture",
            required_keys=[
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
            ],
        )
    except LLMError as exc:
        task_logger.warning("LLM architecture design unavailable: %s", exc)
        outputs = _heuristic_architecture(request.goal)

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
    research_candidates = [
        item
        for item in research_outputs.get("candidate_files", [])
        if isinstance(item, str) and (repo_path / item).exists() and (repo_path / item).is_file()
    ]

    existing_touched: list[str] = []
    if isinstance(raw_touched, list):
        for item in raw_touched:
            if not isinstance(item, str):
                continue
            candidate = item.strip()
            if not candidate:
                continue
            full_path = repo_path / candidate
            if full_path.exists() and full_path.is_file() and candidate not in existing_touched:
                existing_touched.append(candidate)

    if len(existing_touched) < 2:
        for candidate in research_candidates:
            if candidate not in existing_touched:
                existing_touched.append(candidate)
            if len(existing_touched) >= 6:
                break

    if existing_touched:
        normalized["touched_areas"] = existing_touched

    return normalized
