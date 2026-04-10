"""
Purpose: Test worker for linting, typing, and automated test command execution inside the checked-out repo.
Input/Output: Receives command lists and returns per-command exit codes, stdout/stderr snippets, and a summarized report.
Important invariants: Commands run without a shell, only allowed prefixes are executed, and failures stay visible.
How to debug: If a command is blocked or fails, inspect the policy file and captured stderr in the test report.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from fastapi import FastAPI

from services.shared.agentic_lab.config import get_settings
from services.shared.agentic_lab.guardrails import command_is_allowed, load_policy_file
from services.shared.agentic_lab.logging_utils import TaskLoggerAdapter, configure_logging
from services.shared.agentic_lab.repo_tools import CommandError, run_command, write_report
from services.shared.agentic_lab.schemas import Artifact, HealthResponse, WorkerRequest, WorkerResponse

settings = get_settings()
logger = configure_logging(settings.service_name, settings.log_level)
policy = load_policy_file(Path("/app/config/worker-policies.yaml"))
app = FastAPI(title="Feberdin Test Worker", version="0.1.0")


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(service="test-worker")


@app.post("/run", response_model=WorkerResponse)
async def run(request: WorkerRequest) -> WorkerResponse:
    task_logger = TaskLoggerAdapter(logger.logger, {"service": "test-worker", "task_id": request.task_id})
    repo_path = Path(request.local_repo_path)
    command_plan = _build_command_plan(request, repo_path)
    if not command_plan:
        return WorkerResponse(
            worker="tester",
            success=False,
            summary="No test commands were configured or inferred.",
            errors=["Configure lint, typing, or test commands for the target repository profile."],
        )

    results: list[dict] = []
    errors: list[str] = []
    warnings: list[str] = []
    for stage, command in command_plan:
        if not command_is_allowed(command, policy):
            message = f"Blocked disallowed command `{command}`."
            warnings.append(message)
            results.append({"stage": stage, "command": command, "blocked": True})
            continue

        try:
            completed = run_command(shlex.split(command), cwd=repo_path, timeout=900)
            results.append(
                {
                    "stage": stage,
                    "command": command,
                    "returncode": completed.returncode,
                    "stdout": completed.stdout[-4000:],
                    "stderr": completed.stderr[-4000:],
                }
            )
        except CommandError as exc:
            task_logger.warning("Command failed: %s", exc)
            errors.append(str(exc))
            results.append({"stage": stage, "command": command, "error": str(exc)})

    report = {"results": results, "errors": errors, "warnings": warnings}
    report_path = write_report(settings.task_report_dir(request.task_id), "test-report.json", report)
    return WorkerResponse(
        worker="tester",
        success=not errors,
        summary="Automated test commands completed." if not errors else "One or more test commands failed.",
        outputs=report,
        warnings=warnings,
        errors=errors,
        artifacts=[
            Artifact(
                name="test-report",
                path=str(report_path),
                description="Per-command lint, typing, and test execution results.",
            )
        ],
    )


def _build_command_plan(request: WorkerRequest, repo_path: Path) -> list[tuple[str, str]]:
    commands: list[tuple[str, str]] = []

    commands.extend(("lint", command) for command in request.lint_commands)
    commands.extend(("typing", command) for command in request.typing_commands)
    commands.extend(("test", command) for command in request.test_commands)

    # Why this exists: a repo may not provide an explicit profile yet during early onboarding.
    # What happens here: infer a narrow default command set from common Python or Node entry files.
    if not commands and (repo_path / "pyproject.toml").exists():
        commands.append(("test", "pytest -q"))
    if not commands and (repo_path / "package.json").exists():
        commands.append(("test", "npm test"))

    return commands
