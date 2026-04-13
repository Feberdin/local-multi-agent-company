"""
Purpose: LangGraph-based orchestration for the Feberdin multi-agent workflow.
Input/Output: The orchestrator loads persisted task state, routes work to specialist workers, and stores every result and approval gate.
Important invariants: The workflow is resumable, approvals are required before risky GitHub or deployment steps.
Each worker stays specialized instead of acting as an unbounded all-purpose agent.
How to debug: If a task stops unexpectedly, compare the last persisted status, resume target, and stored worker result.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from services.shared.agentic_lab.config import Settings
from services.shared.agentic_lab.logging_utils import TaskLoggerAdapter, configure_logging
from services.shared.agentic_lab.model_routing import resolve_worker_route
from services.shared.agentic_lab.policy_service import RepositoryPolicyError, RepositoryPolicyService
from services.shared.agentic_lab.schemas import DeploymentConfig, SmokeCheck, TaskDetail, TaskStatus, WorkerRequest
from services.shared.agentic_lab.task_service import TaskService
from services.shared.agentic_lab.worker_client import WorkerCallError, call_worker
from services.shared.agentic_lab.worker_governance import WorkerGovernanceService


class WorkflowState(TypedDict, total=False):
    task_id: str
    goal: str
    repository: str
    repo_url: str | None
    local_repo_path: str
    base_branch: str
    branch_name: str | None
    current_status: str
    resume_target: str | None
    approval_required: bool
    approval_reason: str | None
    auto_deploy_staging: bool
    issue_number: int | None
    metadata: dict[str, Any]
    worker_results: dict[str, Any]
    risk_flags: list[str]
    test_commands: list[str]
    lint_commands: list[str]
    typing_commands: list[str]
    smoke_checks: list[SmokeCheck]
    deployment: DeploymentConfig | None
    pull_request_url: str | None
    latest_error: str | None


WORKER_STAGE_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "requirements": {
        "label": "Requirements",
        "description": "Anforderungen, Annahmen, Risiken und Akzeptanzkriterien werden strukturiert.",
    },
    "cost": {
        "label": "Ressourcenplanung",
        "description": "Modell- und Ressourcenbedarf werden grob eingeschaetzt.",
    },
    "human_resources": {
        "label": "Worker-Auswahl",
        "description": "Spezialisierte Worker und ihr sinnvoller Einsatz werden geplant.",
    },
    "research": {
        "label": "Recherche",
        "description": "Repo-Kontext und bei Bedarf zulaessige Quellen werden ausgewertet.",
    },
    "architecture": {
        "label": "Architektur",
        "description": "Loesungsstruktur, Schnittstellen und Implementierungsrichtung werden vorbereitet.",
    },
    "data": {
        "label": "Daten",
        "description": "Datenlogik, Parsing oder Klassifikation werden vertieft betrachtet.",
    },
    "ux": {
        "label": "UX",
        "description": "Bedienfluss, UI-Risiken und Nutzerfuehrung werden bewertet.",
    },
    "coding": {
        "label": "Coding",
        "description": "Codeaenderungen werden vorbereitet oder umgesetzt.",
    },
    "rollback": {
        "label": "Rollback",
        "description": "Deterministische Ruecknahme oder Self-Update-Watchdog laufen.",
    },
    "reviewer": {
        "label": "Review",
        "description": "Korrektheit, Risiken und Wartbarkeit werden geprueft.",
    },
    "tester": {
        "label": "Tests",
        "description": "Tests, Linting und Typpruefung werden bewertet oder angestossen.",
    },
    "security": {
        "label": "Security",
        "description": "Sicherheits-, Secret- und Shell-Risiken werden untersucht.",
    },
    "validation": {
        "label": "Validierung",
        "description": "Das Ergebnis wird gegen Auftrag und Akzeptanzkriterien gespiegelt.",
    },
    "documentation": {
        "label": "Dokumentation",
        "description": "Verstaendliche Betriebs- und Uebergabedokumentation wird vorbereitet.",
    },
    "github": {
        "label": "GitHub",
        "description": "Commit, Push und Pull Request werden vorbereitet oder erstellt.",
    },
    "deploy": {
        "label": "Staging Deploy",
        "description": "Staging-Deployment und Rollout-Schritte laufen an.",
    },
    "qa": {
        "label": "QA",
        "description": "Smoke-Checks und Health-Pruefungen werden zusammengefasst.",
    },
    "memory": {
        "label": "Memory",
        "description": "Entscheidungen und Learnings werden dauerhaft festgehalten.",
    },
}


class WorkflowOrchestrator:
    """LangGraph wrapper that routes the Auftrag through the specialist worker team."""

    def __init__(
        self,
        settings: Settings,
        task_service: TaskService,
        policy_service: RepositoryPolicyService | None = None,
        worker_governance_service: WorkerGovernanceService | None = None,
    ) -> None:
        self.settings = settings
        self.task_service = task_service
        self.policy_service = policy_service or RepositoryPolicyService(settings)
        self.worker_governance_service = worker_governance_service or WorkerGovernanceService(settings)
        self.logger = configure_logging(settings.service_name, settings.log_level)
        self.graph = self._build_graph().compile()

    async def run_task(self, task_id: str) -> TaskDetail:
        """Load the latest persisted state and continue the workflow from the correct stage."""

        task = self.task_service.get_task(task_id)
        try:
            self.policy_service.assert_repository_allowed(task.repository)
        except RepositoryPolicyError as exc:
            failed = self.task_service.update_status(
                task_id,
                TaskStatus.FAILED,
                message="Repository access policy blocked the task.",
                details={"repository": task.repository, "error": str(exc)},
                latest_error=str(exc),
            )
            return failed
        await self.graph.ainvoke(self._task_to_state(task))
        return self.task_service.get_task(task_id)

    def _build_graph(self) -> StateGraph:
        graph = StateGraph(WorkflowState)

        graph.add_node("requirements", self._requirements_node)
        graph.add_node("cost", self._cost_node)
        graph.add_node("human_resources", self._human_resources_node)
        graph.add_node("research", self._research_node)
        graph.add_node("architecture", self._architecture_node)
        graph.add_node("data", self._data_node)
        graph.add_node("ux", self._ux_node)
        graph.add_node("coding", self._coding_node)
        graph.add_node("rollback", self._rollback_node)
        graph.add_node("review", self._review_node)
        graph.add_node("testing", self._testing_node)
        graph.add_node("security", self._security_node)
        graph.add_node("validation", self._validation_node)
        graph.add_node("documentation", self._documentation_node)
        graph.add_node("github", self._github_node)
        graph.add_node("deploy", self._deploy_node)
        graph.add_node("qa", self._qa_node)
        graph.add_node("memory", self._memory_node)

        graph.add_conditional_edges(
            START,
            self._route_entry,
            {
                "requirements": "requirements",
                "cost": "cost",
                "human_resources": "human_resources",
                "research": "research",
                "architecture": "architecture",
                "data": "data",
                "ux": "ux",
                "coding": "coding",
                "rollback": "rollback",
                "review": "review",
                "testing": "testing",
                "security": "security",
                "validation": "validation",
                "documentation": "documentation",
                "github": "github",
                "deploy": "deploy",
                "qa": "qa",
                "memory": "memory",
                "stop": END,
            },
        )
        graph.add_conditional_edges("requirements", self._route_after_requirements, {"cost": "cost", "stop": END})
        graph.add_conditional_edges("cost", self._route_after_cost, {"human_resources": "human_resources", "stop": END})
        graph.add_conditional_edges(
            "human_resources",
            self._route_after_human_resources,
            {"research": "research", "stop": END},
        )
        graph.add_conditional_edges(
            "research",
            self._route_after_research,
            {"architecture": "architecture", "stop": END},
        )
        graph.add_conditional_edges(
            "architecture",
            self._route_after_architecture,
            {"data": "data", "ux": "ux", "coding": "coding", "stop": END},
        )
        graph.add_conditional_edges("data", self._route_after_data, {"ux": "ux", "coding": "coding", "stop": END})
        graph.add_conditional_edges("ux", self._route_after_ux, {"coding": "coding", "stop": END})
        graph.add_conditional_edges("rollback", self._route_after_rollback, {"stop": END})
        graph.add_conditional_edges("coding", self._route_after_coding, {"review": "review", "stop": END})
        graph.add_conditional_edges("review", self._route_after_review, {"testing": "testing", "stop": END})
        graph.add_conditional_edges("testing", self._route_after_testing, {"security": "security", "stop": END})
        graph.add_conditional_edges(
            "security",
            self._route_after_security,
            {"validation": "validation", "stop": END},
        )
        graph.add_conditional_edges(
            "validation",
            self._route_after_validation,
            {"documentation": "documentation", "stop": END},
        )
        graph.add_conditional_edges(
            "documentation",
            self._route_after_documentation,
            {"github": "github", "stop": END},
        )
        graph.add_conditional_edges(
            "github",
            self._route_after_github,
            {"deploy": "deploy", "memory": "memory", "stop": END},
        )
        graph.add_conditional_edges("deploy", self._route_after_deploy, {"qa": "qa", "stop": END})
        graph.add_conditional_edges("qa", self._route_after_qa, {"memory": "memory", "stop": END})
        graph.add_conditional_edges("memory", self._route_after_memory, {"stop": END})
        return graph

    def _route_entry(self, state: WorkflowState) -> str:
        if state.get("current_status") in {TaskStatus.DONE.value, TaskStatus.FAILED.value}:
            return "stop"
        if state.get("approval_required"):
            return "stop"
        if state.get("resume_target"):
            return state["resume_target"]  # type: ignore[return-value]
        if state.get("metadata", {}).get("rollback_commit_sha"):
            return "rollback"
        if state.get("current_status") == TaskStatus.SELF_UPDATING.value:
            return "stop"

        if state.get("current_status") == TaskStatus.PR_CREATED.value:
            return "deploy" if state.get("auto_deploy_staging", True) else "memory"

        mapping = {
            TaskStatus.NEW.value: "requirements",
            TaskStatus.REQUIREMENTS.value: "requirements",
            TaskStatus.RESOURCE_PLANNING.value: "cost",
            TaskStatus.RESEARCHING.value: "research",
            TaskStatus.ARCHITECTING.value: "architecture",
            TaskStatus.CODING.value: "coding",
            TaskStatus.ROLLING_BACK.value: "rollback",
            TaskStatus.REVIEWING.value: "review",
            TaskStatus.TESTING.value: "testing",
            TaskStatus.SECURITY_REVIEW.value: "security",
            TaskStatus.VALIDATING.value: "validation",
            TaskStatus.DOCUMENTING.value: "documentation",
            TaskStatus.STAGING_DEPLOYED.value: "qa",
            TaskStatus.QA_PENDING.value: "qa",
            TaskStatus.MEMORY_UPDATING.value: "memory",
        }
        return mapping.get(state.get("current_status", TaskStatus.NEW.value), "requirements")

    def _route_after_requirements(self, state: WorkflowState) -> str:
        return "stop" if self._should_stop(state) else "cost"

    def _route_after_cost(self, state: WorkflowState) -> str:
        return "stop" if self._should_stop(state) else "human_resources"

    def _route_after_human_resources(self, state: WorkflowState) -> str:
        return "stop" if self._should_stop(state) else "research"

    def _route_after_research(self, state: WorkflowState) -> str:
        return "stop" if self._should_stop(state) else "architecture"

    def _route_after_architecture(self, state: WorkflowState) -> str:
        if self._should_stop(state):
            return "stop"
        if self._specialist_requested(state, "data"):
            return "data"
        if self._specialist_requested(state, "ux"):
            return "ux"
        return "coding"

    def _route_after_data(self, state: WorkflowState) -> str:
        if self._should_stop(state):
            return "stop"
        return "ux" if self._specialist_requested(state, "ux") else "coding"

    def _route_after_ux(self, state: WorkflowState) -> str:
        return "stop" if self._should_stop(state) else "coding"

    def _route_after_rollback(self, state: WorkflowState) -> str:
        return "stop"

    def _route_after_coding(self, state: WorkflowState) -> str:
        return "stop" if self._should_stop(state) else "review"

    def _route_after_review(self, state: WorkflowState) -> str:
        return "stop" if self._should_stop(state) else "testing"

    def _route_after_testing(self, state: WorkflowState) -> str:
        return "stop" if self._should_stop(state) else "security"

    def _route_after_security(self, state: WorkflowState) -> str:
        return "stop" if self._should_stop(state) else "validation"

    def _route_after_validation(self, state: WorkflowState) -> str:
        return "stop" if self._should_stop(state) else "documentation"

    def _route_after_documentation(self, state: WorkflowState) -> str:
        return "stop" if self._should_stop(state) else "github"

    def _route_after_github(self, state: WorkflowState) -> str:
        if self._should_stop(state):
            return "stop"
        return "deploy" if state.get("auto_deploy_staging", True) else "memory"

    def _route_after_deploy(self, state: WorkflowState) -> str:
        if state.get("metadata", {}).get("deployment_target") == "self":
            return "stop"
        return "stop" if self._should_stop(state) else "qa"

    def _route_after_qa(self, state: WorkflowState) -> str:
        return "stop" if self._should_stop(state) else "memory"

    def _route_after_memory(self, state: WorkflowState) -> str:
        return "stop"

    def _should_stop(self, state: WorkflowState) -> bool:
        return state.get("current_status") in {
            TaskStatus.APPROVAL_REQUIRED.value,
            TaskStatus.DONE.value,
            TaskStatus.FAILED.value,
        }

    def _specialist_requested(self, state: WorkflowState, worker_name: str) -> bool:
        hr_outputs = state.get("worker_results", {}).get("human_resources", {}).get("outputs", {})
        recommended = hr_outputs.get("recommended_workers", [])
        return worker_name in recommended

    def _service_url(self, worker_name: str) -> str:
        mapping = {
            "requirements": self.settings.requirements_worker_url,
            "cost": self.settings.cost_worker_url,
            "human_resources": self.settings.human_resources_worker_url,
            "research": self.settings.research_worker_url,
            "architecture": self.settings.architecture_worker_url,
            "data": self.settings.data_worker_url,
            "ux": self.settings.ux_worker_url,
            "coding": self.settings.coding_worker_url,
            "rollback": self.settings.rollback_worker_url,
            "reviewer": self.settings.reviewer_worker_url,
            "tester": self.settings.test_worker_url,
            "security": self.settings.security_worker_url,
            "validation": self.settings.validation_worker_url,
            "documentation": self.settings.documentation_worker_url,
            "github": self.settings.github_worker_url,
            "deploy": self.settings.deploy_worker_url,
            "qa": self.settings.qa_worker_url,
            "memory": self.settings.memory_worker_url,
        }
        return mapping[worker_name]

    def _stage_metadata(self, worker_name: str) -> dict[str, str]:
        """Return a short operator-facing label and description for the current worker stage."""

        return WORKER_STAGE_DESCRIPTIONS.get(
            worker_name,
            {"label": worker_name.replace("_", " ").title(), "description": "Der Worker bearbeitet diese Stage."},
        )

    def _model_route_summary(self, worker_name: str) -> dict[str, Any]:
        """Expose the currently resolved model route for debugging long local LLM calls."""

        try:
            provider, route = resolve_worker_route(self.settings, worker_name)
        except Exception as exc:  # pragma: no cover - defensive only, route loading is tested separately.
            return {"route_error": str(exc)}
        return self._route_summary_payload(provider, route)

    @staticmethod
    def _route_summary_payload(provider, route) -> dict[str, Any]:
        """Normalize provider and route details into one stable, JSON-friendly structure."""

        return {
            "provider": provider.name,
            "fallback_provider": route.fallback_provider,
            "model_name": provider.model_name,
            "base_url": provider.base_url,
            "request_timeout_seconds": route.request_timeout_seconds,
            "reasoning": route.reasoning,
            "output_contract": route.output_contract,
            "routing_note": route.routing_note,
            "purpose": route.purpose,
        }

    def _build_worker_route_snapshot(self) -> dict[str, dict[str, Any]]:
        """Freeze one route summary per worker so long tasks stay reproducible even if routing changes later."""

        snapshot: dict[str, dict[str, Any]] = {}
        for worker_name in WORKER_STAGE_DESCRIPTIONS:
            snapshot[worker_name] = self._model_route_summary(worker_name)
        return snapshot

    def _ensure_execution_snapshots(self, state: WorkflowState) -> WorkflowState:
        """Persist one immutable guidance/routing snapshot for the task's remaining lifetime."""

        metadata = dict(state.get("metadata", {}))
        metadata_updates: dict[str, Any] = {}

        guidance_snapshot = metadata.get("worker_guidance_snapshot")
        if not isinstance(guidance_snapshot, dict) or not guidance_snapshot:
            guidance_snapshot = self.worker_governance_service.guidance_map()
            metadata_updates["worker_guidance_snapshot"] = guidance_snapshot

        route_snapshot = metadata.get("worker_route_snapshot")
        if not isinstance(route_snapshot, dict) or not route_snapshot:
            route_snapshot = self._build_worker_route_snapshot()
            metadata_updates["worker_route_snapshot"] = route_snapshot

        if metadata_updates:
            self.task_service.update_runtime_context(state["task_id"], metadata_updates=metadata_updates)
            metadata.update(metadata_updates)

        return {**state, "metadata": metadata}

    def _frozen_model_route_summary(self, state: WorkflowState, worker_name: str) -> dict[str, Any]:
        """Return the persisted per-task model route, falling back to the live route only when no snapshot exists yet."""

        route_snapshot = state.get("metadata", {}).get("worker_route_snapshot", {})
        if isinstance(route_snapshot, dict):
            route_summary = route_snapshot.get(worker_name)
            if isinstance(route_summary, dict) and route_summary:
                return route_summary
        return self._model_route_summary(worker_name)

    def _truncate_text(self, value: str, max_length: int = 220) -> str:
        """Keep UI-facing progress summaries short enough for dashboards and event cards."""

        compact = " ".join(value.split())
        if len(compact) <= max_length:
            return compact
        return compact[: max_length - 1].rstrip() + "…"

    def _previous_worker_name(self, worker_name: str) -> str | None:
        """Return the worker that usually completes immediately before the current stage."""

        stage_order = list(WORKER_STAGE_DESCRIPTIONS)
        try:
            index = stage_order.index(worker_name)
        except ValueError:
            return None
        if index == 0:
            return None
        return stage_order[index - 1]

    def _next_worker_name(self, worker_name: str) -> str | None:
        """Return the next worker in the default stage order for handoff hints."""

        stage_order = list(WORKER_STAGE_DESCRIPTIONS)
        try:
            index = stage_order.index(worker_name)
        except ValueError:
            return None
        if index + 1 >= len(stage_order):
            return None
        return stage_order[index + 1]

    def _current_instruction(self, state: WorkflowState, worker_name: str, stage_meta: dict[str, str]) -> str:
        """Summarize the concrete assignment for one worker in operator-friendly German."""

        worker_specific_focus = {
            "requirements": "Leite Anforderungen, Annahmen, Risiken und Akzeptanzkriterien aus dem Ziel ab.",
            "cost": "Schaetze Modell- und Ressourcenbedarf fuer langsame Homelab-Hardware realistisch ein.",
            "human_resources": "Waehle passende Worker und begruende ihre Reihenfolge.",
            "research": "Analysiere Repository, vorhandene Dateien und zulaessige Quellen, ohne zu raten.",
            "architecture": "Lege Struktur, Schnittstellen und einen kleinen, sicheren Umsetzungsplan fest.",
            "data": "Untersuche Datenfluss, Parsing oder Klassifikation nur fuer die wirklich noetigen Teile.",
            "ux": "Pruefe Nutzerfuehrung, Bedienfluss und klare Rueckmeldungen fuer die Oberflaeche.",
            "coding": "Bereite minimale, nachvollziehbare Codeaenderungen im isolierten Task-Workspace vor.",
            "rollback": "Ueberwache Self-Updates oder stelle den letzten stabilen Commit deterministisch wieder her.",
            "reviewer": "Suche gezielt nach Bugs, Risiken, Regressionen und fehlenden Tests.",
            "tester": "Fuehre sichere Test-, Lint- und Typpruefungen aus und fasse Abweichungen knapp zusammen.",
            "security": "Pruefe Secrets, riskante Shell-Kommandos und Supply-Chain-Risiken.",
            "validation": "Spiegele Ergebnis gegen Ziel und Akzeptanzkriterien und benenne Rest-Risiken.",
            "documentation": "Erstelle verstaendliche Betriebs- und Uebergabehinweise fuer Nicht-Programmierer.",
            "github": "Bereite Commit, Push und Pull Request transparent und nachvollziehbar vor.",
            "deploy": "Fuehre nur die freigegebenen Staging-Schritte aus und melde Healthchecks klar zurueck.",
            "qa": "Fasse Smoke-Checks und Freigabestatus fuer den Operator zusammen.",
            "memory": "Halte Learnings, Entscheidungen und Folgepunkte dauerhaft fest.",
        }
        return self._truncate_text(
            f"{worker_specific_focus.get(worker_name, stage_meta['description'])} Ziel: {state['goal']}",
            max_length=260,
        )

    def _progress_details(
        self,
        *,
        state: WorkflowState,
        worker_name: str,
        stage_meta: dict[str, str],
        event_kind: str,
        worker_state: str,
        service_url: str,
        route_summary: dict[str, Any],
        started_at_iso: str,
        elapsed_seconds: float,
        waiting_for: str | None = None,
        blocked_by: str | None = None,
        last_result_summary: str | None = None,
        last_error: str | None = None,
        progress_message: str | None = None,
    ) -> dict[str, Any]:
        """Build one normalized progress payload that both the UI and debug bundles can consume."""

        previous_worker = self._previous_worker_name(worker_name)
        next_worker = self._next_worker_name(worker_name)
        current_instruction = self._current_instruction(state, worker_name, stage_meta)
        return {
            "event_kind": event_kind,
            "worker_name": worker_name,
            "stage_label": stage_meta["label"],
            "stage_description": stage_meta["description"],
            "service_url": service_url,
            "model_route": route_summary,
            "state": worker_state,
            "current_step": stage_meta["label"],
            "current_action": stage_meta["description"],
            "current_instruction": current_instruction,
            "current_prompt_summary": current_instruction,
            "waiting_for": waiting_for,
            "blocked_by": blocked_by,
            "previous_worker": previous_worker,
            "next_worker": next_worker,
            "started_at": started_at_iso,
            "updated_at": datetime.now(UTC).isoformat(),
            "elapsed_seconds": elapsed_seconds,
            "last_result_summary": last_result_summary,
            "last_error": last_error,
            "last_event_message": progress_message,
            "progress_message": progress_message,
        }

    async def _stage_heartbeat(
        self,
        *,
        state: WorkflowState,
        task_id: str,
        worker_name: str,
        stage_status: TaskStatus,
        service_url: str,
        started_at: float,
        started_at_iso: str,
        route_summary: dict[str, Any],
    ) -> None:
        """Persist periodic progress events so the UI shows that a slow stage is still alive."""

        interval = max(5.0, self.settings.stage_heartbeat_interval_seconds)
        stage_meta = self._stage_metadata(worker_name)
        while True:
            await asyncio.sleep(interval)
            elapsed_seconds = round(asyncio.get_running_loop().time() - started_at, 1)
            self.task_service.append_event(
                task_id,
                stage=stage_status.value,
                message=(
                    f"{stage_meta['label']} arbeitet weiter. "
                    "Die Antwort aus dem Worker-Service oder vom lokalen Modell steht noch aus."
                ),
                details={
                    **self._progress_details(
                        state=state,
                        worker_name=worker_name,
                        stage_meta=stage_meta,
                        event_kind="stage_heartbeat",
                        worker_state="running",
                        service_url=service_url,
                        route_summary=route_summary,
                        started_at_iso=started_at_iso,
                        elapsed_seconds=elapsed_seconds,
                        waiting_for=(
                            f"Antwort des Worker-Services unter {service_url}"
                            if service_url
                            else "Antwort des Worker-Services"
                        ),
                        progress_message=(
                            f"{stage_meta['label']} arbeitet seit {elapsed_seconds:.1f}s im Hintergrund. "
                            "Modell- oder Worker-Antwort steht noch aus."
                        ),
                    ),
                    "heartbeat": True,
                },
            )

    async def _requirements_node(self, state: WorkflowState) -> WorkflowState:
        return await self._run_stage(
            state=state,
            worker_name="requirements",
            service_url=self._service_url("requirements"),
            stage_status=TaskStatus.REQUIREMENTS,
        )

    async def _cost_node(self, state: WorkflowState) -> WorkflowState:
        return await self._run_stage(
            state=state,
            worker_name="cost",
            service_url=self._service_url("cost"),
            stage_status=TaskStatus.RESOURCE_PLANNING,
        )

    async def _human_resources_node(self, state: WorkflowState) -> WorkflowState:
        return await self._run_stage(
            state=state,
            worker_name="human_resources",
            service_url=self._service_url("human_resources"),
            stage_status=TaskStatus.RESOURCE_PLANNING,
        )

    async def _research_node(self, state: WorkflowState) -> WorkflowState:
        return await self._run_stage(
            state=state,
            worker_name="research",
            service_url=self._service_url("research"),
            stage_status=TaskStatus.RESEARCHING,
        )

    async def _architecture_node(self, state: WorkflowState) -> WorkflowState:
        return await self._run_stage(
            state=state,
            worker_name="architecture",
            service_url=self._service_url("architecture"),
            stage_status=TaskStatus.ARCHITECTING,
        )

    async def _data_node(self, state: WorkflowState) -> WorkflowState:
        return await self._run_stage(
            state=state,
            worker_name="data",
            service_url=self._service_url("data"),
            stage_status=TaskStatus.ARCHITECTING,
        )

    async def _ux_node(self, state: WorkflowState) -> WorkflowState:
        return await self._run_stage(
            state=state,
            worker_name="ux",
            service_url=self._service_url("ux"),
            stage_status=TaskStatus.ARCHITECTING,
        )

    async def _coding_node(self, state: WorkflowState) -> WorkflowState:
        if self._modification_approval_required(state):
            task = self.task_service.set_approval_required(
                state["task_id"],
                reason=self._modification_approval_reason(state),
                resume_target="coding",
                gate_name="repository-modification",
            )
            return self._task_to_state(task)
        return await self._run_stage(
            state=state,
            worker_name="coding",
            service_url=self._service_url("coding"),
            stage_status=TaskStatus.CODING,
        )

    async def _rollback_node(self, state: WorkflowState) -> WorkflowState:
        stage_state = await self._run_stage(
            state=state,
            worker_name="rollback",
            service_url=self._service_url("rollback"),
            stage_status=TaskStatus.ROLLING_BACK,
        )
        if stage_state.get("current_status") == TaskStatus.FAILED.value:
            return stage_state

        task = self.task_service.update_status(
            stage_state["task_id"],
            TaskStatus.DONE,
            message="Rollback wurde erfolgreich abgeschlossen.",
            details={"rollback_completed": True},
        )
        return self._task_to_state(task)

    async def _review_node(self, state: WorkflowState) -> WorkflowState:
        return await self._run_stage(
            state=state,
            worker_name="reviewer",
            service_url=self._service_url("reviewer"),
            stage_status=TaskStatus.REVIEWING,
        )

    async def _testing_node(self, state: WorkflowState) -> WorkflowState:
        return await self._run_stage(
            state=state,
            worker_name="tester",
            service_url=self._service_url("tester"),
            stage_status=TaskStatus.TESTING,
        )

    async def _security_node(self, state: WorkflowState) -> WorkflowState:
        return await self._run_stage(
            state=state,
            worker_name="security",
            service_url=self._service_url("security"),
            stage_status=TaskStatus.SECURITY_REVIEW,
        )

    async def _validation_node(self, state: WorkflowState) -> WorkflowState:
        return await self._run_stage(
            state=state,
            worker_name="validation",
            service_url=self._service_url("validation"),
            stage_status=TaskStatus.VALIDATING,
        )

    async def _documentation_node(self, state: WorkflowState) -> WorkflowState:
        stage_state = await self._run_stage(
            state=state,
            worker_name="documentation",
            service_url=self._service_url("documentation"),
            stage_status=TaskStatus.DOCUMENTING,
        )
        if stage_state.get("current_status") == TaskStatus.FAILED.value:
            return stage_state
        if self._approval_needed_before_github(stage_state):
            reason = self._approval_reason(stage_state)
            task = self.task_service.set_approval_required(
                stage_state["task_id"],
                reason=reason,
                resume_target="github",
                gate_name="risk-review",
            )
            return self._task_to_state(task)
        return stage_state

    async def _github_node(self, state: WorkflowState) -> WorkflowState:
        stage_state = await self._run_stage(
            state=state,
            worker_name="github",
            service_url=self._service_url("github"),
            stage_status=TaskStatus.DOCUMENTING,
            resume_target="github",
        )
        if stage_state.get("current_status") == TaskStatus.FAILED.value:
            return stage_state

        github_result = stage_state.get("worker_results", {}).get("github", {})
        pull_request_url = github_result.get("outputs", {}).get("pull_request_url")
        if pull_request_url:
            self.task_service.set_pull_request(stage_state["task_id"], pull_request_url)
        task = self.task_service.update_status(
            stage_state["task_id"],
            TaskStatus.PR_CREATED,
            message="Draft pull request created successfully.",
            details={"pull_request_url": pull_request_url},
        )
        return self._task_to_state(task)

    async def _deploy_node(self, state: WorkflowState) -> WorkflowState:
        deploy_metadata = dict(state.get("metadata", {}))
        deployment_commit_sha = str(
            deploy_metadata.get("deployment_target_commit_sha")
            or state.get("worker_results", {}).get("github", {}).get("outputs", {}).get("commit_sha")
            or ""
        ).strip()
        if deployment_commit_sha and deploy_metadata.get("deployment_target_commit_sha") != deployment_commit_sha:
            self.task_service.update_runtime_context(
                state["task_id"],
                metadata_updates={"deployment_target_commit_sha": deployment_commit_sha},
            )
            deploy_metadata["deployment_target_commit_sha"] = deployment_commit_sha
            state = {**state, "metadata": deploy_metadata}

        stage_state = await self._run_stage(
            state=state,
            worker_name="deploy",
            service_url=self._service_url("deploy"),
            stage_status=TaskStatus.PR_CREATED,
        )
        if stage_state.get("current_status") == TaskStatus.FAILED.value:
            return stage_state

        if state.get("metadata", {}).get("deployment_target") == "self":
            rollback_meta = self._stage_metadata("rollback")
            watchdog_summary = (
                stage_state.get("worker_results", {}).get("deploy", {}).get("outputs", {}).get("watchdog_status")
                or "monitoring"
            )
            task = self.task_service.update_status(
                stage_state["task_id"],
                TaskStatus.SELF_UPDATING,
                message="Self-Update wurde dispatcht; rollback-worker ueberwacht jetzt Health und Rollback.",
                details=self._progress_details(
                    state=stage_state,
                    worker_name="rollback",
                    stage_meta=rollback_meta,
                    event_kind="self_update_monitoring",
                    worker_state="waiting",
                    service_url=self._service_url("rollback"),
                    route_summary=self._model_route_summary("rollback"),
                    started_at_iso=datetime.now(UTC).isoformat(),
                    elapsed_seconds=0.0,
                    waiting_for="Healthcheck-Rueckkehr des aktualisierten Stacks",
                    last_result_summary=f"Watchdog-Status: {watchdog_summary}",
                    progress_message="Rollback-Watchdog beobachtet den Self-Update-Rollout.",
                ),
            )
            return self._task_to_state(task)

        task = self.task_service.update_status(
            stage_state["task_id"],
            TaskStatus.STAGING_DEPLOYED,
            message="Staging deployment completed.",
            details={"staging_healthcheck_url": self.settings.staging_healthcheck_url},
        )
        return self._task_to_state(task)

    async def _qa_node(self, state: WorkflowState) -> WorkflowState:
        self.task_service.update_status(
            state["task_id"],
            TaskStatus.QA_PENDING,
            message="Smoke and health checks started.",
            details={},
        )
        return await self._run_stage(
            state={**state, "current_status": TaskStatus.QA_PENDING.value},
            worker_name="qa",
            service_url=self._service_url("qa"),
            stage_status=TaskStatus.QA_PENDING,
        )

    async def _memory_node(self, state: WorkflowState) -> WorkflowState:
        stage_state = await self._run_stage(
            state=state,
            worker_name="memory",
            service_url=self._service_url("memory"),
            stage_status=TaskStatus.MEMORY_UPDATING,
        )
        if stage_state.get("current_status") == TaskStatus.FAILED.value:
            return stage_state

        task = self.task_service.update_status(
            stage_state["task_id"],
            TaskStatus.DONE,
            message="Workflow completed successfully.",
            details={"pull_request_url": stage_state.get("pull_request_url")},
        )
        return self._task_to_state(task)

    def _approval_needed_before_github(self, state: WorkflowState) -> bool:
        review_result = state.get("worker_results", {}).get("reviewer", {})
        security_result = state.get("worker_results", {}).get("security", {})
        metadata = state.get("metadata", {})
        review_requires = review_result.get("requires_human_approval", False)
        security_requires = security_result.get("requires_human_approval", False)
        force_publish_approval = bool(metadata.get("force_publish_approval"))
        return bool(state.get("risk_flags") or review_requires or security_requires or force_publish_approval)

    def _modification_approval_required(self, state: WorkflowState) -> bool:
        metadata = state.get("metadata", {})
        return not metadata.get("allow_repository_modifications", False)

    def _modification_approval_reason(self, state: WorkflowState) -> str:
        metadata = state.get("metadata", {})
        worker_project_label = metadata.get("worker_project_label", "Feberdin local-multi-agent-company worker project")
        return (
            f"Explizite Freigabe erforderlich: Das erlaubte Repository `{state['repository']}` darf erst geändert werden, "
            f"wenn du bestätigst, dass das `{worker_project_label}` Änderungen vornehmen darf."
        )

    def _approval_reason(self, state: WorkflowState) -> str:
        metadata = state.get("metadata", {})
        if metadata.get("force_publish_approval"):
            return (
                "Diese Self-Improvement-Aenderung ist als riskant eingestuft. "
                "Branch, Diffs und Testergebnisse sind vorbereitet, aber GitHub/Deploy bleiben bis zur Freigabe gesperrt."
            )
        review_reason = state.get("worker_results", {}).get("reviewer", {}).get("approval_reason")
        security_reason = state.get("worker_results", {}).get("security", {}).get("approval_reason")
        return review_reason or security_reason or "Risky changes detected before GitHub publication."

    async def _run_stage(
        self,
        *,
        state: WorkflowState,
        worker_name: str,
        service_url: str,
        stage_status: TaskStatus,
        resume_target: str | None = None,
    ) -> WorkflowState:
        """Shared worker-stage behavior with logging, retries, persistence, and failure handling."""

        state = self._ensure_execution_snapshots(state)
        task_id = state["task_id"]
        logger = TaskLoggerAdapter(self.logger.logger, {"service": self.settings.service_name, "task_id": task_id})
        stage_meta = self._stage_metadata(worker_name)
        route_summary = self._frozen_model_route_summary(state, worker_name)
        started_at_iso = datetime.now(UTC).isoformat()
        self.task_service.update_status(
            task_id,
            stage_status,
            message=f"{stage_meta['label']} gestartet.",
            details=self._progress_details(
                state=state,
                worker_name=worker_name,
                stage_meta=stage_meta,
                event_kind="stage_started",
                worker_state="running",
                service_url=service_url,
                route_summary=route_summary,
                started_at_iso=started_at_iso,
                elapsed_seconds=0.0,
                progress_message=f"{stage_meta['label']} wurde gestartet.",
            ),
            resume_target=resume_target,
        )
        self.task_service.append_event(
            task_id,
            stage=stage_status.value,
            message=(
                f"Worker-Anfrage fuer {stage_meta['label']} wurde versendet. "
                "Auf langsamer lokaler Hardware kann die naechste Antwort mehrere Minuten brauchen."
            ),
            details={
                **self._progress_details(
                    state=state,
                    worker_name=worker_name,
                    stage_meta=stage_meta,
                    event_kind="worker_dispatch",
                    worker_state="running",
                    service_url=service_url,
                    route_summary=route_summary,
                    started_at_iso=started_at_iso,
                    elapsed_seconds=0.0,
                    waiting_for=(
                        f"Antwort des Worker-Services unter {service_url} "
                        f"mit Modell {route_summary.get('model_name', 'unbekannt')}"
                    ),
                    progress_message=(
                        f"Arbeitsauftrag an {stage_meta['label']} gesendet. "
                        "Bei lokalen Modellen kann die Antwort mehrere Minuten dauern."
                    ),
                ),
                "worker_timeout_summary": self.settings.worker_timeout_summary(),
            },
        )
        logger.info("Starting %s stage against %s", worker_name, service_url)

        worker_request = self._build_worker_request(state, worker_name)
        stage_started_at = asyncio.get_running_loop().time()
        heartbeat_task = asyncio.create_task(
            self._stage_heartbeat(
                state=state,
                task_id=task_id,
                worker_name=worker_name,
                stage_status=stage_status,
                service_url=service_url,
                started_at=stage_started_at,
                started_at_iso=started_at_iso,
                route_summary=route_summary,
            )
        )

        try:
            async with asyncio.timeout(self.settings.worker_stage_timeout_seconds):
                response = await call_worker(service_url, worker_request)
        except TimeoutError as exc:
            logger.error("%s stage exceeded the configured stage timeout: %s", worker_name, exc)
            failed_task = self.task_service.update_status(
                task_id,
                TaskStatus.FAILED,
                message=f"{stage_meta['label']} hat das konfigurierte Stage-Zeitbudget ueberschritten.",
                details={
                    **self._progress_details(
                        state=state,
                        worker_name=worker_name,
                        stage_meta=stage_meta,
                        event_kind="stage_timeout",
                        worker_state="failed",
                        service_url=service_url,
                        route_summary=route_summary,
                        started_at_iso=started_at_iso,
                        elapsed_seconds=self.settings.worker_stage_timeout_seconds,
                        waiting_for=(
                            f"Antwort von {service_url} innerhalb des Stage-Limits "
                            f"von {self.settings.worker_stage_timeout_seconds}s"
                        ),
                        last_error=(
                            f"Stage-Zeitlimit von {self.settings.worker_stage_timeout_seconds}s ueberschritten."
                        ),
                        progress_message=f"{stage_meta['label']} hat das Stage-Zeitlimit erreicht.",
                    ),
                    "worker_stage_timeout_seconds": self.settings.worker_stage_timeout_seconds,
                    "worker_timeout_summary": self.settings.worker_timeout_summary(),
                },
                latest_error=(
                    f"{stage_meta['label']} lief laenger als {self.settings.worker_stage_timeout_seconds}s. "
                    "Erhoehe WORKER_STAGE_TIMEOUT_SECONDS fuer sehr langsame lokale Hardware."
                ),
            )
            return self._task_to_state(failed_task)
        except WorkerCallError as exc:
            logger.error("%s stage failed: %s", worker_name, exc)
            failed_task = self.task_service.update_status(
                task_id,
                TaskStatus.FAILED,
                message=f"{stage_meta['label']} ist fehlgeschlagen.",
                details=self._progress_details(
                    state=state,
                    worker_name=worker_name,
                    stage_meta=stage_meta,
                    event_kind="stage_failed",
                    worker_state="failed",
                    service_url=service_url,
                    route_summary=route_summary,
                    started_at_iso=started_at_iso,
                    elapsed_seconds=round(asyncio.get_running_loop().time() - stage_started_at, 1),
                    last_error=str(exc),
                    progress_message=f"{stage_meta['label']} ist mit einem Worker-Fehler abgebrochen.",
                ),
                latest_error=str(exc),
            )
            return self._task_to_state(failed_task)
        finally:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task

        annotated_response = self.worker_governance_service.annotate_worker_response(
            worker_name,
            worker_request,
            response,
        )
        self.worker_governance_service.register_worker_suggestions(
            worker_name=worker_name,
            request=worker_request,
            response=annotated_response,
        )
        self.task_service.store_worker_result(task_id, worker_name, annotated_response)
        runtime_repo_path = str(annotated_response.outputs.get("local_repo_path") or "").strip()
        if runtime_repo_path and runtime_repo_path != state.get("local_repo_path"):
            self.task_service.update_runtime_context(
                task_id,
                local_repo_path=runtime_repo_path,
                metadata_updates={"task_workspace_path": runtime_repo_path},
            )
        elapsed_seconds = round(asyncio.get_running_loop().time() - stage_started_at, 1)

        if not annotated_response.success:
            self.task_service.append_event(
                task_id,
                stage=stage_status.value,
                message=f"{stage_meta['label']} meldete einen Fehlerzustand.",
                details={
                    **self._progress_details(
                        state=state,
                        worker_name=worker_name,
                        stage_meta=stage_meta,
                        event_kind="stage_failed",
                        worker_state="failed",
                        service_url=service_url,
                        route_summary=route_summary,
                        started_at_iso=started_at_iso,
                        elapsed_seconds=elapsed_seconds,
                        last_result_summary=annotated_response.summary,
                        last_error="; ".join(annotated_response.errors) or None,
                        progress_message=f"{stage_meta['label']} meldete einen Fehlerzustand.",
                    ),
                    "success": annotated_response.success,
                    "warnings": annotated_response.warnings,
                    "errors": annotated_response.errors,
                },
                level="WARNING",
            )
            error_text = "; ".join(annotated_response.errors) or f"{worker_name} reported failure."
            failed_task = self.task_service.update_status(
                task_id,
                TaskStatus.FAILED,
                message=f"{stage_meta['label']} meldete einen Fehler.",
                details={"errors": annotated_response.errors, "warnings": annotated_response.warnings},
                latest_error=error_text,
            )
            return self._task_to_state(failed_task)

        self.task_service.append_event(
            task_id,
            stage=stage_status.value,
            message=f"{stage_meta['label']} abgeschlossen.",
            details={
                **self._progress_details(
                    state=state,
                    worker_name=worker_name,
                    stage_meta=stage_meta,
                    event_kind="stage_completed",
                    worker_state="complete",
                    service_url=service_url,
                    route_summary=route_summary,
                    started_at_iso=started_at_iso,
                    elapsed_seconds=elapsed_seconds,
                    waiting_for=(
                        f"Uebergabe an {self._next_worker_name(worker_name)}"
                        if self._next_worker_name(worker_name)
                        else None
                    ),
                    last_result_summary=annotated_response.summary,
                    progress_message=f"{stage_meta['label']} wurde erfolgreich abgeschlossen.",
                ),
                "success": annotated_response.success,
                "warnings": annotated_response.warnings,
                "errors": annotated_response.errors,
            },
        )
        logger.info("%s stage completed successfully", worker_name)

        return self._task_to_state(self.task_service.get_task(task_id))

    def _build_worker_request(self, state: WorkflowState, worker_name: str) -> WorkerRequest:
        metadata = dict(state.get("metadata", {}))
        guidance_map = metadata.get("worker_guidance_snapshot")
        if not isinstance(guidance_map, dict) or not guidance_map:
            guidance_map = self.worker_governance_service.guidance_map()
        metadata["worker_guidance_map"] = guidance_map
        metadata["current_worker_guidance"] = guidance_map.get(worker_name)
        metadata["current_worker_name"] = worker_name
        route_snapshot = metadata.get("worker_route_snapshot")
        if isinstance(route_snapshot, dict):
            metadata["current_worker_route"] = route_snapshot.get(worker_name)
        return WorkerRequest(
            task_id=state["task_id"],
            goal=state["goal"],
            repository=state["repository"],
            repo_url=state.get("repo_url") or metadata.get("repo_url"),
            local_repo_path=state["local_repo_path"],
            base_branch=state["base_branch"],
            branch_name=state.get("branch_name"),
            issue_number=state.get("issue_number") or metadata.get("issue_number"),
            enable_web_research=metadata.get("enable_web_research", False),
            auto_deploy_staging=state.get("auto_deploy_staging", True),
            test_commands=state.get("test_commands", []),
            lint_commands=state.get("lint_commands", []),
            typing_commands=state.get("typing_commands", []),
            smoke_checks=state.get("smoke_checks", []),
            deployment=state.get("deployment"),
            metadata=metadata,
            prior_results=state.get("worker_results", {}),
        )

    def _task_to_state(self, task: TaskDetail) -> WorkflowState:
        metadata = dict(task.metadata)
        return WorkflowState(
            task_id=task.id,
            goal=task.goal,
            repository=task.repository,
            repo_url=task.repo_url,
            local_repo_path=task.local_repo_path,
            base_branch=task.base_branch,
            branch_name=task.branch_name,
            current_status=task.status.value,
            resume_target=task.resume_target,
            approval_required=task.approval_required,
            approval_reason=task.approval_reason,
            auto_deploy_staging=metadata.get("auto_deploy_staging", True),
            issue_number=metadata.get("issue_number"),
            metadata=metadata,
            worker_results=task.worker_results,
            risk_flags=task.risk_flags,
            test_commands=metadata.get("test_commands", []),
            lint_commands=metadata.get("lint_commands", []),
            typing_commands=metadata.get("typing_commands", []),
            smoke_checks=task.smoke_checks,
            deployment=task.deployment,
            pull_request_url=task.pull_request_url,
            latest_error=task.latest_error,
        )
