"""
Purpose: Run a fast, operator-friendly LLM routing probe across selected workers without touching repositories.
Input/Output: Starts a persisted benchmark run, asks the configured worker routes for small synthetic answers,
and stores readable results for the web UI.
Important invariants: The probe must stay side-effect free, short enough for local homelabs,
and explicit about which provider/model answered.
How to debug: If a probe stalls or shows the wrong model, inspect the persisted run JSON together
with the resolved model route and the recorded fallback flags.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from services.shared.agentic_lab.config import Settings
from services.shared.agentic_lab.llm import LLMClient, LLMError
from services.shared.agentic_lab.logging_utils import configure_logging
from services.shared.agentic_lab.model_routing import resolve_fallback_provider, resolve_worker_route
from services.shared.agentic_lab.schemas import (
    WorkerProbeRegistryResponse,
    WorkerProbeResultResponse,
    WorkerProbeRunResponse,
    WorkerProbeRunStatus,
    WorkerProbeStartRequest,
    WorkerRequest,
)
from services.shared.agentic_lab.worker_governance import WorkerGovernanceService

PROBE_WORKERS: tuple[str, ...] = (
    "requirements",
    "research",
    "architecture",
    "coding",
    "reviewer",
    "security",
    "validation",
    "documentation",
)
PROBE_WORKER_LABELS: dict[str, str] = {
    "requirements": "Anforderungen",
    "research": "Recherche",
    "architecture": "Architektur",
    "coding": "Code",
    "reviewer": "Review",
    "security": "Sicherheit",
    "validation": "Validierung",
    "documentation": "Doku",
}
PROBE_HISTORY_LIMIT = 10
PROBE_MAX_TOKENS = 320


class WorkerProbeError(RuntimeError):
    """Raised when a probe run cannot be started or persisted safely."""


@dataclass(frozen=True)
class WorkerProbeDefinition:
    """Describe how one worker should be probed in a fast, side-effect free way."""

    worker_name: str
    output_contract: str
    response_format: str
    required_keys: tuple[str, ...] = ()


PROBE_DEFINITIONS: dict[str, WorkerProbeDefinition] = {
    "requirements": WorkerProbeDefinition(
        worker_name="requirements",
        output_contract="json",
        response_format="json",
        required_keys=(
            "summary",
            "requirements",
            "wishes",
            "assumptions",
            "risks",
            "acceptance_criteria",
            "open_questions",
            "recommended_workers",
        ),
    ),
    "research": WorkerProbeDefinition(worker_name="research", output_contract="text", response_format="text"),
    "architecture": WorkerProbeDefinition(
        worker_name="architecture",
        output_contract="json",
        response_format="json",
        required_keys=(
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
        ),
    ),
    "coding": WorkerProbeDefinition(
        worker_name="coding",
        output_contract="edit_plan",
        response_format="json",
        required_keys=("summary", "operations"),
    ),
    "reviewer": WorkerProbeDefinition(
        worker_name="reviewer",
        output_contract="json",
        response_format="json",
        required_keys=("findings", "warnings"),
    ),
    "security": WorkerProbeDefinition(
        worker_name="security",
        output_contract="json",
        response_format="json",
        required_keys=("findings", "residual_risks", "requires_human_approval", "approval_reason"),
    ),
    "validation": WorkerProbeDefinition(
        worker_name="validation",
        output_contract="json",
        response_format="json",
        required_keys=(
            "fulfilled",
            "partially_verified",
            "unverified",
            "residual_risks",
            "release_readiness",
            "recommendation",
        ),
    ),
    "documentation": WorkerProbeDefinition(
        worker_name="documentation",
        output_contract="text",
        response_format="markdown",
    ),
}


class WorkerProbeService:
    """Persist and execute small benchmark-style LLM probes for operator visibility."""

    def __init__(
        self,
        settings: Settings,
        llm: LLMClient,
        *,
        governance_service: WorkerGovernanceService | None = None,
        storage_path: Path | None = None,
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.governance_service = governance_service or WorkerGovernanceService(settings)
        self.storage_path = storage_path or settings.data_dir / "worker_probe_runs.json"
        self._lock = asyncio.Lock()
        self.logger = configure_logging("worker-probe-service", settings.log_level)

    def load_registry(self) -> WorkerProbeRegistryResponse:
        """Load probe history defensively so a malformed file does not break the benchmarks page."""

        if not self.storage_path.exists():
            return WorkerProbeRegistryResponse()
        try:
            return WorkerProbeRegistryResponse.model_validate_json(self.storage_path.read_text("utf-8"))
        except Exception as exc:  # pragma: no cover - defensive operator-facing fallback.
            self.logger.warning("Worker probe registry could not be parsed cleanly: %s", exc)
            return WorkerProbeRegistryResponse()

    def resume_orphaned_runs(self) -> None:
        """Mark interrupted in-flight runs as failed so the UI does not wait forever after a restart."""

        registry = self.load_registry()
        mutated = False
        normalized_runs: list[WorkerProbeRunResponse] = []
        for run in registry.runs:
            if run.status not in {WorkerProbeRunStatus.QUEUED, WorkerProbeRunStatus.RUNNING}:
                normalized_runs.append(run)
                continue
            mutated = True
            errors = list(run.errors)
            errors.append(
                "Die Modell-Probe wurde durch einen Neustart oder Prozessabbruch unterbrochen, bevor alle Antworten gesammelt waren."
            )
            normalized_runs.append(
                run.model_copy(
                    update={
                        "status": WorkerProbeRunStatus.FAILED,
                        "active_worker_name": None,
                        "updated_at": _utc_now(),
                        "completed_at": _utc_now(),
                        "errors": errors,
                    }
                )
            )
        if mutated:
            self._write_registry(WorkerProbeRegistryResponse(runs=normalized_runs))

    async def start_run(self, request: WorkerProbeStartRequest) -> WorkerProbeRunResponse:
        """Create a new queued probe run, unless another one is already active."""

        if not self.settings.has_llm_backend():
            raise WorkerProbeError(
                "Es ist kein LLM-Backend konfiguriert. Pruefe MISTRAL_BASE_URL/QWEN_BASE_URL und die Modellnamen."
            )

        async with self._lock:
            registry = self.load_registry()
            if any(run.status in {WorkerProbeRunStatus.QUEUED, WorkerProbeRunStatus.RUNNING} for run in registry.runs):
                raise WorkerProbeError(
                    "Es laeuft bereits eine Modell-Probe. Warte auf deren Abschluss, bevor du einen neuen Schnelltest startest."
                )

            run = WorkerProbeRunResponse(
                id=str(uuid4()),
                status=WorkerProbeRunStatus.QUEUED,
                probe_goal=request.probe_goal.strip(),
                total_workers=len(PROBE_WORKERS),
            )
            registry.runs.insert(0, run)
            registry.runs = registry.runs[:PROBE_HISTORY_LIMIT]
            self._write_registry(registry)
            return run

    async def execute_run(self, run_id: str) -> WorkerProbeRunResponse:
        """Execute the queued probe run in the background and persist progress after every worker."""

        await self._update_run(
            run_id,
            status=WorkerProbeRunStatus.RUNNING,
            started_at=_utc_now(),
            updated_at=_utc_now(),
            active_worker_name=PROBE_WORKERS[0],
        )
        try:
            for worker_name in PROBE_WORKERS:
                result = await self._probe_one_worker(run_id, worker_name)
                current = self._find_run(run_id)
                results = [item for item in current.results if item.worker_name != worker_name]
                results.append(result)
                results.sort(key=lambda item: PROBE_WORKERS.index(item.worker_name))
                completed_workers = sum(1 for item in results if item.status == "ok")
                failed_workers = sum(1 for item in results if item.status == "failed")
                next_index = PROBE_WORKERS.index(worker_name) + 1
                await self._update_run(
                    run_id,
                    results=results,
                    completed_workers=completed_workers,
                    failed_workers=failed_workers,
                    active_worker_name=PROBE_WORKERS[next_index] if next_index < len(PROBE_WORKERS) else None,
                    updated_at=_utc_now(),
                )
        except Exception as exc:  # pragma: no cover - defensive top-level guard.
            current = self._find_run(run_id)
            errors = list(current.errors)
            errors.append(f"{exc.__class__.__name__}: {exc}")
            await self._update_run(
                run_id,
                status=WorkerProbeRunStatus.FAILED,
                active_worker_name=None,
                completed_at=_utc_now(),
                updated_at=_utc_now(),
                errors=errors,
            )
            return self._find_run(run_id)

        await self._update_run(
            run_id,
            status=WorkerProbeRunStatus.COMPLETED,
            active_worker_name=None,
            completed_at=_utc_now(),
            updated_at=_utc_now(),
        )
        return self._find_run(run_id)

    async def _probe_one_worker(self, run_id: str, worker_name: str) -> WorkerProbeResultResponse:
        """Ask one configured worker route for a tiny synthetic answer and capture readable diagnostics."""

        definition = PROBE_DEFINITIONS[worker_name]
        synthetic_request = self._synthetic_request(run_id, worker_name)
        guidance_block = self.governance_service.guidance_prompt_block(synthetic_request, worker_name)
        primary_provider, route = resolve_worker_route(self.settings, worker_name)
        fallback_provider = resolve_fallback_provider(self.settings, worker_name)
        started_at = _utc_now()

        try:
            system_prompt, user_prompt = self._probe_prompts(
                definition=definition,
                probe_goal=synthetic_request.goal,
                guidance_block=guidance_block,
            )
            if definition.response_format in {"json"}:
                payload, trace = await self.llm.complete_json_with_trace(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    worker_name=worker_name,
                    required_keys=definition.required_keys,
                    max_tokens=PROBE_MAX_TOKENS,
                )
                response_text = json.dumps(payload, indent=2, ensure_ascii=False)
                summary = str(payload.get("summary") or payload.get("recommendation") or "Strukturierte Modellantwort erhalten.")
                response_data = payload
            else:
                text, trace = await self.llm.complete_with_trace(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    worker_name=worker_name,
                    max_tokens=PROBE_MAX_TOKENS,
                )
                response_text = text
                response_data = {}
                summary = _clip_text(text, max_length=180)

            completed_at = _utc_now()
            notes = [
                f"Konfiguriertes Primärmodell: {primary_provider.name} / {primary_provider.model_name}",
                (
                    f"Konfigurierter Fallback: {fallback_provider.name} / {fallback_provider.model_name}"
                    if fallback_provider
                    else "Kein konfigurierter Fallback."
                ),
            ]
            return WorkerProbeResultResponse(
                worker_name=worker_name,
                worker_label=PROBE_WORKER_LABELS.get(worker_name, worker_name),
                status="ok",
                output_contract=route.output_contract,
                response_format=definition.response_format,
                summary=summary,
                response_text=response_text,
                response_data=response_data,
                provider=str(trace.get("provider") or primary_provider.name),
                model_name=str(trace.get("model_name") or primary_provider.model_name),
                base_url=str(trace.get("base_url") or primary_provider.base_url),
                used_fallback=bool(trace.get("used_fallback")),
                repair_pass_used=bool(trace.get("repair_pass_used")),
                started_at=started_at,
                completed_at=completed_at,
                elapsed_seconds=round((completed_at - started_at).total_seconds(), 1),
                notes=notes,
            )
        except LLMError as exc:
            completed_at = _utc_now()
            return WorkerProbeResultResponse(
                worker_name=worker_name,
                worker_label=PROBE_WORKER_LABELS.get(worker_name, worker_name),
                status="failed",
                output_contract=route.output_contract,
                response_format=definition.response_format,
                summary="Keine nutzbare Modellantwort erhalten.",
                provider=primary_provider.name,
                model_name=primary_provider.model_name,
                base_url=primary_provider.base_url,
                used_fallback=False,
                repair_pass_used=False,
                started_at=started_at,
                completed_at=completed_at,
                elapsed_seconds=round((completed_at - started_at).total_seconds(), 1),
                error_message=str(exc),
                notes=[
                    f"Konfiguriertes Primärmodell: {primary_provider.name} / {primary_provider.model_name}",
                    (
                        f"Konfigurierter Fallback: {fallback_provider.name} / {fallback_provider.model_name}"
                        if fallback_provider
                        else "Kein konfigurierter Fallback."
                    ),
                ],
            )

    def _probe_prompts(
        self,
        *,
        definition: WorkerProbeDefinition,
        probe_goal: str,
        guidance_block: str,
    ) -> tuple[str, str]:
        """Return a compact prompt pair that exercises the same route contract without touching a real repo."""

        candidate_files = [
            "services/web_ui/app.py",
            "services/shared/agentic_lab/llm.py",
            "services/orchestrator/app.py",
            "tests/unit/test_web_ui_benchmarks.py",
            "README.md",
        ]
        synthetic_diff = (
            "diff --git a/services/web_ui/app.py b/services/web_ui/app.py\n"
            "@@\n"
            "- return {\"service\": \"web-ui\", \"status\": \"ok\"}\n"
            "+ logger.info(\"health check ok for %s\", request.url)\n"
            "+ response_headers[\"X-Debug-Token\"] = os.getenv(\"DEBUG_TOKEN\", \"\")\n"
            "+ return {\"service\": \"web-ui\", \"status\": \"ok\"}\n"
        )
        validation_input = {
            "fulfilled": ["Die Healthchecks geben Status 200 zurueck."],
            "partially_verified": ["Fallback-Verhalten wurde nur mit Unit-Tests geprueft."],
            "unverified": ["Produktive Last auf Unraid wurde noch nicht simuliert."],
        }
        security_input = {
            "risk_flags": ["debug-header"],
            "findings": ["Ein potentiell sensibler Header darf nicht ungeprueft gesetzt werden."],
        }

        if definition.worker_name == "requirements":
            return (
                "You are a requirements engineer. Return JSON with keys summary, requirements, wishes, assumptions, "
                "risks, acceptance_criteria, open_questions, recommended_workers."
                f"{guidance_block}",
                (
                    f"Original Auftrag:\n{probe_goal}\n\n"
                    "Dies ist nur ein schneller Modell-Probelauf. "
                    "Formuliere die Antwort kurz, aber vollstaendig und gut lesbar."
                ),
            )
        if definition.worker_name == "research":
            return (
                "You are a research lead. Write a short operator-readable analysis with sections "
                "First Checks, Likely Files, Open Questions, and Trusted Sources."
                f"{guidance_block}",
                (
                    f"Probe-Auftrag:\n{probe_goal}\n\n"
                    f"Repository: Feberdin/local-multi-agent-company\n"
                    f"Vermutete Kandidatdateien: {candidate_files}\n"
                    "Wichtig: Dies ist ein schneller Probelauf ohne echten Repo-Zugriff. "
                    "Antworte konkret, aber so, als waere dies deine erste orientierende Analyse."
                ),
            )
        if definition.worker_name == "architecture":
            return (
                "You are a staff-plus architect. Return JSON with keys summary, components, responsibilities, "
                "data_flows, module_boundaries, deployment_strategy, logging_strategy, implementation_plan, "
                "test_strategy, risks, approval_gates, touched_areas. "
                "touched_areas must only use these relative paths: "
                + ", ".join(candidate_files)
                + "."
                + guidance_block
            ), (
                f"Goal:\n{probe_goal}\n\n"
                "Research summary:\n"
                f"- Candidate files: {candidate_files}\n"
                "- Main risk: observability changes can accidentally leak debug data.\n"
                "Prepare a compact, reviewable architecture answer for a local Docker homelab stack."
            )
        if definition.worker_name == "coding":
            return (
                "You are a careful coding worker. Return a single JSON edit plan with keys summary, operations, "
                "and optionally blocking_reason. Keep it small, safe, and audit-friendly."
                f"{guidance_block}",
                (
                    f"Goal:\n{probe_goal}\n\n"
                    "This is a benchmark probe only. No real repository access is available. "
                    "Create an illustrative edit plan that touches only these synthetic files:\n"
                    "- services/web_ui/app.py\n"
                    "- tests/unit/test_web_ui_benchmarks.py\n"
                    "Add safer error output and one small visibility test."
                ),
            )
        if definition.worker_name == "reviewer":
            return (
                "You are a strict reviewer focused on bugs, regressions, security, and maintainability. "
                "Return JSON with keys findings and warnings."
                f"{guidance_block}",
                (
                    f"Goal:\n{probe_goal}\n\n"
                    f"Changed files:\n{candidate_files[:2]}\n\n"
                    f"Unified diff:\n{synthetic_diff}\n\n"
                    "List only the most important review observations."
                ),
            )
        if definition.worker_name == "security":
            return (
                "You are a security reviewer. Return JSON with keys findings, residual_risks, requires_human_approval, approval_reason."
                f"{guidance_block}",
                (
                    f"Goal:\n{probe_goal}\n\n"
                    f"Changed files:\n{candidate_files[:2]}\n\n"
                    f"Unified diff:\n{synthetic_diff}\n\n"
                    "Focus on secret exposure, debug leakage, and operator-safe defaults."
                ),
            )
        if definition.worker_name == "validation":
            return (
                "You are a validation lead. Return JSON with keys fulfilled, partially_verified, unverified, residual_risks, "
                "release_readiness, recommendation."
                f"{guidance_block}"
            ), (
                f"Original Auftrag:\n{probe_goal}\n\n"
                f"Validation input:\n{validation_input}\n\n"
                f"Security input:\n{security_input}\n\n"
                "Be strict and separate verified evidence from assumptions."
            )
        return (
            "You are a documentation lead. Produce markdown with sections Summary, Validation, Risks, Deployment Notes, Next Steps."
            f"{guidance_block}",
            (
                f"Goal:\n{probe_goal}\n\n"
                f"Validation:\n{validation_input}\n\n"
                f"Security:\n{security_input}\n\n"
                "Write a short operator handoff that is easy to skim in a benchmark UI."
            ),
        )

    def _synthetic_request(self, run_id: str, worker_name: str) -> WorkerRequest:
        """Create a small synthetic request so worker guidance can still influence the benchmark prompt."""

        return WorkerRequest(
            task_id=f"{run_id}-{worker_name}",
            goal=self._find_run(run_id).probe_goal,
            repository="Feberdin/local-multi-agent-company",
            local_repo_path="/probe/noop",
            base_branch="main",
            metadata={},
        )

    def _find_run(self, run_id: str) -> WorkerProbeRunResponse:
        registry = self.load_registry()
        for run in registry.runs:
            if run.id == run_id:
                return run
        raise WorkerProbeError(f"Modell-Probelauf `{run_id}` wurde nicht gefunden.")

    async def _update_run(self, run_id: str, **changes: Any) -> None:
        async with self._lock:
            registry = self.load_registry()
            updated_runs: list[WorkerProbeRunResponse] = []
            matched = False
            for run in registry.runs:
                if run.id != run_id:
                    updated_runs.append(run)
                    continue
                updated_runs.append(run.model_copy(update=changes))
                matched = True
            if not matched:
                raise WorkerProbeError(f"Modell-Probelauf `{run_id}` wurde nicht gefunden.")
            self._write_registry(WorkerProbeRegistryResponse(runs=updated_runs))

    def _write_registry(self, registry: WorkerProbeRegistryResponse) -> None:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self.storage_path.write_text(
            json.dumps(registry.model_dump(mode="json"), indent=2, ensure_ascii=True),
            encoding="utf-8",
        )


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _clip_text(value: str, *, max_length: int = 180) -> str:
    compact = " ".join(str(value or "").split())
    if len(compact) <= max_length:
        return compact
    return compact[: max_length - 1].rstrip() + "…"
