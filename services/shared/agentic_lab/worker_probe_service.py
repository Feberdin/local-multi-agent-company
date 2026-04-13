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
import subprocess
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
    WorkerProbeMode,
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
FOCUS_CONTEXT_MAX_CHARS = 2200


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

            selected_workers = list(_normalize_probe_workers(request.selected_workers))
            focus_paths = list(_normalize_focus_paths(request.focus_paths))
            run = WorkerProbeRunResponse(
                id=str(uuid4()),
                status=WorkerProbeRunStatus.QUEUED,
                probe_goal=request.probe_goal.strip(),
                probe_mode=request.probe_mode,
                selected_workers=selected_workers,
                focus_paths=focus_paths,
                total_workers=len(selected_workers),
            )
            registry.runs.insert(0, run)
            registry.runs = registry.runs[:PROBE_HISTORY_LIMIT]
            self._write_registry(registry)
            return run

    async def execute_run(self, run_id: str) -> WorkerProbeRunResponse:
        """Execute the queued probe run in the background and persist progress after every worker."""

        ordered_workers = _normalize_probe_workers(self._find_run(run_id).selected_workers)
        await self._update_run(
            run_id,
            status=WorkerProbeRunStatus.RUNNING,
            started_at=_utc_now(),
            updated_at=_utc_now(),
            active_worker_name=ordered_workers[0],
        )
        try:
            for worker_name in ordered_workers:
                result = await self._probe_one_worker(run_id, worker_name)
                current = self._find_run(run_id)
                results = [item for item in current.results if item.worker_name != worker_name]
                results.append(result)
                results.sort(key=lambda item: ordered_workers.index(item.worker_name))
                completed_workers = sum(1 for item in results if item.status == "ok")
                failed_workers = sum(1 for item in results if item.status == "failed")
                next_index = ordered_workers.index(worker_name) + 1
                await self._update_run(
                    run_id,
                    results=results,
                    completed_workers=completed_workers,
                    failed_workers=failed_workers,
                    active_worker_name=ordered_workers[next_index] if next_index < len(ordered_workers) else None,
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
        probe_run = self._find_run(run_id)
        synthetic_request = self._synthetic_request(run_id, worker_name)
        guidance_block = self.governance_service.guidance_prompt_block(synthetic_request, worker_name)
        primary_provider, route = resolve_worker_route(self.settings, worker_name)
        fallback_provider = resolve_fallback_provider(self.settings, worker_name)
        started_at = _utc_now()

        request_max_tokens = 160 if probe_run.probe_mode == WorkerProbeMode.OK_CONTRACT else PROBE_MAX_TOKENS

        try:
            system_prompt, user_prompt = self._probe_prompts(
                definition=definition,
                probe_goal=probe_run.probe_goal,
                probe_mode=probe_run.probe_mode,
                focus_paths=probe_run.focus_paths,
                selected_worker_count=len(probe_run.selected_workers),
                guidance_block=guidance_block,
            )
            if definition.response_format in {"json"}:
                payload, trace = await self.llm.complete_json_with_trace(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    worker_name=worker_name,
                    required_keys=definition.required_keys,
                    max_tokens=request_max_tokens,
                )
                response_text = json.dumps(payload, indent=2, ensure_ascii=False)
                summary = str(payload.get("summary") or payload.get("recommendation") or "Strukturierte Modellantwort erhalten.")
                response_data = payload
            else:
                text, trace = await self.llm.complete_with_trace(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    worker_name=worker_name,
                    max_tokens=request_max_tokens,
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
        probe_mode: WorkerProbeMode,
        focus_paths: list[str],
        selected_worker_count: int,
        guidance_block: str,
    ) -> tuple[str, str]:
        """Return a compact prompt pair that exercises the same route contract without touching a real repo."""

        if probe_mode == WorkerProbeMode.OK_CONTRACT:
            return self._ok_contract_probe_prompts(
                definition=definition,
                probe_goal=probe_goal,
                guidance_block=guidance_block,
                focus_paths=focus_paths,
            )

        candidate_files = focus_paths or [
            "services/web_ui/app.py",
            "services/shared/agentic_lab/llm.py",
            "services/orchestrator/app.py",
            "tests/unit/test_web_ui_benchmarks.py",
            "README.md",
        ]
        focus_block = ""
        if focus_paths:
            focus_block = (
                "Fix-Fokus fuer diesen Teiltest:\n"
                + "\n".join(f"- {path}" for path in focus_paths)
                + "\n\n"
            )
        focus_context_block = self._focus_context_block(focus_paths)
        targeted_probe_note = (
            "Dies ist ein fokussierter Teiltest nur fuer den ausgewaehlten Worker. "
            if selected_worker_count == 1
            else "Dies ist ein kleiner Teiltest fuer eine ausgewaehlte Worker-Gruppe. "
        )
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
                    f"{focus_block}"
                    f"{focus_context_block}"
                    f"{targeted_probe_note}"
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
                    f"{focus_block}"
                    f"{focus_context_block}"
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
                f"{focus_block}"
                f"{focus_context_block}"
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
                    f"{focus_block}"
                    f"{focus_context_block}"
                    f"{targeted_probe_note}"
                    "Use the provided file context and do not claim missing file access when excerpts or diffs are present. "
                    "Return the smallest useful edit plan that proves you "
                    "understood the fix focus. Touch only these files:\n"
                    + "\n".join(f"- {path}" for path in candidate_files[:4])
                    + "\nUse at most one small operation per relevant file, avoid long file contents, and never invent unrelated files."
                ),
            )
        if definition.worker_name == "reviewer":
            return (
                "You are a strict reviewer focused on bugs, regressions, security, and maintainability. "
                "Return JSON with keys findings and warnings."
                f"{guidance_block}",
                (
                    f"Goal:\n{probe_goal}\n\n"
                    f"{focus_block}"
                    f"{focus_context_block}"
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
                    f"{focus_block}"
                    f"{focus_context_block}"
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
                f"{focus_block}"
                f"{focus_context_block}"
                f"Validation input:\n{validation_input}\n\n"
                f"Security input:\n{security_input}\n\n"
                "Be strict and separate verified evidence from assumptions."
            )
        return (
            "You are a documentation lead. Produce markdown with sections Summary, Validation, Risks, Deployment Notes, Next Steps."
            f"{guidance_block}",
            (
                f"Goal:\n{probe_goal}\n\n"
                f"{focus_block}"
                f"{focus_context_block}"
                f"Validation:\n{validation_input}\n\n"
                f"Security:\n{security_input}\n\n"
                "Write a short operator handoff that is easy to skim in a benchmark UI."
            ),
        )

    def _ok_contract_probe_prompts(
        self,
        *,
        definition: WorkerProbeDefinition,
        probe_goal: str,
        guidance_block: str,
        focus_paths: list[str],
    ) -> tuple[str, str]:
        """Return the smallest contract-valid prompt set so operators can run one empty smoke test quickly."""

        focus_block = ""
        if focus_paths:
            focus_block = (
                "Fix-Fokus fuer diesen Teiltest:\n"
                + "\n".join(f"- {path}" for path in focus_paths)
                + "\n\n"
            )
        focus_context_block = self._focus_context_block(focus_paths)

        if definition.worker_name == "requirements":
            return (
                "You are a requirements engineer. Return a single JSON object with keys summary, requirements, wishes, "
                "assumptions, risks, acceptance_criteria, open_questions, recommended_workers. "
                "Use the smallest valid payload. Set summary to 'OK'. Use short 'OK' entries where a list is required. "
                "No prose outside the JSON object."
                f"{guidance_block}",
                (
                    f"Contract smoke test:\n{probe_goal}\n\n"
                    f"{focus_block}"
                    f"{focus_context_block}"
                    "Return only the smallest valid JSON payload that proves the requirements contract works."
                ),
            )
        if definition.worker_name == "research":
            return (
                "You are a research lead. Return plain text only. Reply with exactly `OK` and nothing else."
                f"{guidance_block}",
                (
                    "This is an empty contract smoke test.\n\n"
                    f"{focus_block}"
                    f"{focus_context_block}"
                    "Do not add sections, explanations, or markdown fences."
                ),
            )
        if definition.worker_name == "architecture":
            return (
                "You are a staff-plus architect. Return a single JSON object with keys summary, components, responsibilities, "
                "data_flows, module_boundaries, deployment_strategy, logging_strategy, implementation_plan, test_strategy, "
                "risks, approval_gates, touched_areas. Use the smallest realistic payload. Set summary to 'OK'. "
                "Use one tiny example object per structured list and keep every string value as short as possible. "
                "No prose outside the JSON object. "
                "Use exactly this minimal shape and keep all keys present: "
                '{"summary":"OK","components":[{"name":"OK","type":"service","description":"OK"}],'
                '"responsibilities":{"OK":"OK"},"data_flows":[{"source":"OK","destination":"OK","type":"OK","description":"OK"}],'
                '"module_boundaries":[{"module":"services/web_ui/app.py","boundary":"OK"}],"deployment_strategy":"OK",'
                '"logging_strategy":"OK","implementation_plan":[{"step":1,"task":"OK","status":"ok"}],'
                '"test_strategy":"OK","risks":["OK"],"approval_gates":["OK"],'
                '"touched_areas":["services/web_ui/app.py"]}'
                f"{guidance_block}",
                (
                    f"Contract smoke test:\n{probe_goal}\n\n"
                    f"{focus_block}"
                    f"{focus_context_block}"
                    "Return exactly the minimal JSON skeleton from the system prompt with the same keys and the same "
                    "overall structure. Do not omit any key."
                ),
            )
        if definition.worker_name == "coding":
            return (
                "You are a careful coding worker. Return a single JSON object with keys summary, operations, and blocking_reason. "
                "This is a contract smoke test without repository access. Return summary 'OK', operations [], and a short "
                "blocking_reason 'OK'. No prose outside the JSON object."
                f"{guidance_block}",
                (
                    f"Contract smoke test:\n{probe_goal}\n\n"
                    f"{focus_block}"
                    f"{focus_context_block}"
                    "Do not invent file operations. Prove only that the edit_plan contract can be emitted cleanly."
                ),
            )
        if definition.worker_name == "reviewer":
            return (
                "You are a strict reviewer. Return a single JSON object with keys findings and warnings. "
                "Use the smallest valid payload with 'OK' content. No prose outside the JSON object."
                f"{guidance_block}",
                f"Contract smoke test:\n{probe_goal}\n\n{focus_block}{focus_context_block}",
            )
        if definition.worker_name == "security":
            return (
                "You are a security reviewer. Return a single JSON object with keys findings, residual_risks, "
                "requires_human_approval, approval_reason. Use the smallest valid payload with 'OK' content and "
                "requires_human_approval false. No prose outside the JSON object."
                f"{guidance_block}",
                f"Contract smoke test:\n{probe_goal}\n\n{focus_block}{focus_context_block}",
            )
        if definition.worker_name == "validation":
            return (
                "You are a validation lead. Return a single JSON object with keys fulfilled, partially_verified, unverified, "
                "residual_risks, release_readiness, recommendation. Use the smallest valid payload with 'OK' content. "
                "No prose outside the JSON object."
                f"{guidance_block}",
                f"Contract smoke test:\n{probe_goal}\n\n{focus_block}{focus_context_block}",
            )
        return (
            "You are a documentation lead. Return markdown only with these sections: Summary, Validation, Risks, Deployment Notes, "
            "Next Steps. Every section should contain only `OK`."
            f"{guidance_block}",
            (
                f"Contract smoke test:\n{probe_goal}\n\n"
                f"{focus_block}"
                f"{focus_context_block}"
                "Return the smallest valid markdown handoff with exactly the requested headings."
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
            metadata={"focus_paths": list(self._find_run(run_id).focus_paths)},
        )

    def _focus_context_block(self, focus_paths: list[str]) -> str:
        """Load small real file excerpts or recent diffs so targeted probes can inspect the actual fix area."""

        context = _build_focus_context(self.settings, focus_paths)
        if not context:
            return ""
        return f"Verfuegbarer Dateikontext:\n{context}\n\n"

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


def _normalize_probe_workers(raw_workers: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    """Return a validated, de-duplicated worker order for full or targeted probe runs."""

    if not raw_workers:
        return PROBE_WORKERS

    selected: set[str] = set()
    for raw in raw_workers:
        worker_name = str(raw or "").strip()
        if not worker_name:
            continue
        if worker_name not in PROBE_DEFINITIONS:
            raise WorkerProbeError(
                f"Der Worker-Probelauf kennt den Worker `{worker_name}` nicht. "
                "Waehle einen bekannten Worker aus der Teiltest-Liste."
            )
        selected.add(worker_name)

    if not selected:
        raise WorkerProbeError(
            "Es wurde kein gueltiger Worker fuer den Teiltest uebergeben. "
            "Waehle mindestens einen Worker aus."
        )

    return tuple(worker_name for worker_name in PROBE_WORKERS if worker_name in selected)


def _normalize_focus_paths(raw_paths: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    """Keep focused probe paths compact and deterministic for persisted operator-facing runs."""

    if not raw_paths:
        return ()

    normalized: list[str] = []
    seen: set[str] = set()
    for raw in raw_paths:
        path = str(raw or "").strip().replace("\\", "/")
        if not path or path in seen:
            continue
        seen.add(path)
        normalized.append(path)
        if len(normalized) >= 8:
            break
    return tuple(normalized)


def _build_focus_context(settings: Settings, focus_paths: list[str]) -> str:
    """Return compact git diff or file excerpts for focused probe files without mutating the repository."""

    repo_root = _resolve_probe_repo_root(settings)
    if repo_root is None or not focus_paths:
        return ""

    sections: list[str] = []
    for path in focus_paths:
        normalized_path = str(path or "").strip().replace("\\", "/")
        if not normalized_path:
            continue
        diff_text = _read_git_focus_diff(repo_root, normalized_path)
        if diff_text:
            sections.append(f"[{normalized_path}] Letzter Commit-Diff\n{diff_text}")
            continue
        file_excerpt = _read_focus_file_excerpt(repo_root / normalized_path)
        if file_excerpt:
            sections.append(f"[{normalized_path}] Aktueller Auszug\n{file_excerpt}")
    return "\n\n".join(sections)


def _resolve_probe_repo_root(settings: Settings) -> Path | None:
    """Locate the checked-out project root so probe prompts can inspect the latest fixed files."""

    candidates = [
        Path(settings.self_improvement_local_repo_path) if settings.self_improvement_local_repo_path else None,
        Path(__file__).resolve().parents[3],
    ]
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate is None:
            continue
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.exists():
            return resolved
    return None


def _read_git_focus_diff(repo_root: Path, relative_path: str) -> str:
    """Read the last commit diff for one file so targeted probes see the actual recent fix area first."""

    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "show", "--format=", "--unified=10", "HEAD", "--", relative_path],
            check=True,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return ""

    content = completed.stdout.strip()
    if not content:
        return ""
    return _clip_text(content, max_length=FOCUS_CONTEXT_MAX_CHARS)


def _read_focus_file_excerpt(file_path: Path) -> str:
    """Fallback to a compact file excerpt when no recent diff is available for the selected focus path."""

    try:
        lines = file_path.read_text("utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return ""

    if not lines:
        return ""

    numbered_head = [f"{index + 1:>4}: {line}" for index, line in enumerate(lines[:40])]
    if len(lines) <= 60:
        excerpt_lines = numbered_head + [f"{index + 1:>4}: {line}" for index, line in enumerate(lines[40:], start=40)]
    else:
        numbered_tail = [
            f"{len(lines) - len(lines[-20:]) + offset + 1:>4}: {line}"
            for offset, line in enumerate(lines[-20:])
        ]
        excerpt_lines = numbered_head + [" ...."] + numbered_tail
    return _clip_text("\n".join(excerpt_lines), max_length=FOCUS_CONTEXT_MAX_CHARS)


def _clip_text(value: str, *, max_length: int = 180) -> str:
    compact = " ".join(str(value or "").split())
    if len(compact) <= max_length:
        return compact
    return compact[: max_length - 1].rstrip() + "…"
