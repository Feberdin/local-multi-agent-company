"""
Purpose: Persist worker guidance, collect improvement suggestions, and build operator-visible decision trees.
Input/Output: The orchestrator loads guidance for worker requests and stores worker-sourced suggestions plus decision traces.
Important invariants: Operator guidance stays explicit, cross-scope suggestions require approval, and decision trees remain auditable.
How to debug: If a worker seems to ignore guidance, inspect the request metadata and the stored decision tree for that stage first.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from services.shared.agentic_lab.config import Settings
from services.shared.agentic_lab.schemas import (
    ImprovementSuggestion,
    ImprovementSuggestionDecisionRequest,
    ImprovementSuggestionRegistry,
    ImprovementSuggestionStatus,
    WorkerDecisionNode,
    WorkerDecisionTree,
    WorkerGuidancePolicy,
    WorkerGuidanceRegistry,
    WorkerRequest,
    WorkerResponse,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_GUIDANCE_SEED_PATH = PROJECT_ROOT / "config/worker_guidance.defaults.json"


class WorkerGovernanceError(ValueError):
    """Raised when worker guidance or suggestion state is invalid."""


class WorkerGovernanceService:
    """Persist guidance and suggestion data while helping the UI and orchestrator stay consistent."""

    def __init__(
        self,
        settings: Settings,
        *,
        guidance_path: Path | None = None,
        suggestions_path: Path | None = None,
        seed_path: Path | None = None,
    ) -> None:
        self.settings = settings
        self.guidance_path = guidance_path or settings.data_dir / "worker_guidance.json"
        self.suggestions_path = suggestions_path or settings.data_dir / "improvement_suggestions.json"
        self.seed_path = seed_path or DEFAULT_GUIDANCE_SEED_PATH

    def load_guidance_registry(self) -> WorkerGuidanceRegistry:
        """Load operator guidance from runtime storage or seed defaults."""

        if self.guidance_path.exists():
            return self._normalize_guidance_registry(
                WorkerGuidanceRegistry.model_validate_json(self.guidance_path.read_text("utf-8"))
            )

        registry = self._load_seed_guidance_registry()
        self._write_guidance_registry(registry)
        return registry

    def save_guidance_registry(self, registry: WorkerGuidanceRegistry) -> WorkerGuidanceRegistry:
        """Persist a complete guidance registry after normalization."""

        normalized = self._normalize_guidance_registry(registry)
        self._write_guidance_registry(normalized)
        return normalized

    def upsert_guidance(self, policy: WorkerGuidancePolicy) -> WorkerGuidanceRegistry:
        """Update one worker policy without forcing the caller to rewrite the full registry."""

        registry = self.load_guidance_registry()
        normalized_policy = self._normalize_guidance_policy(policy, previous_policy=self.policy_for_worker(policy.worker_name))
        existing_index = next((index for index, item in enumerate(registry.workers) if item.worker_name == policy.worker_name), None)
        if existing_index is None:
            registry.workers.append(normalized_policy)
        else:
            registry.workers[existing_index] = normalized_policy
        return self.save_guidance_registry(registry)

    def policy_for_worker(self, worker_name: str) -> WorkerGuidancePolicy:
        registry = self.load_guidance_registry()
        for policy in registry.workers:
            if policy.worker_name == worker_name:
                return policy
        raise WorkerGovernanceError(f"Worker guidance for `{worker_name}` was not found.")

    def guidance_map(self) -> dict[str, dict]:
        """Return a worker-name keyed map for request metadata injection."""

        return {policy.worker_name: policy.model_dump(mode="json") for policy in self.load_guidance_registry().workers}

    def guidance_prompt_block(self, request: WorkerRequest, worker_name: str) -> str:
        """Return a short prompt block that reasoning workers can append to their system instructions."""

        policy = self._policy_from_request(request, worker_name)
        if policy is None or not policy.enabled:
            return ""

        operator_recommendations = "\n".join(f"- {item}" for item in policy.operator_recommendations) or "- Follow the explicit task scope."
        decision_preferences = "\n".join(f"- {item}" for item in policy.decision_preferences) or "- Make decisions explicit and auditable."
        return (
            "\n\nOperator guidance for this worker:\n"
            f"Role summary: {policy.role_summary}\n"
            "Operator recommendations:\n"
            f"{operator_recommendations}\n"
            "Decision preferences:\n"
            f"{decision_preferences}\n"
            f"Competence boundary: {policy.competence_boundary}\n"
            "If you identify a potentially useful change outside this boundary, do not silently expand scope. "
            "Return it as an explicit improvement suggestion instead."
        )

    def annotate_worker_response(self, worker_name: str, request: WorkerRequest, response: WorkerResponse) -> WorkerResponse:
        """Attach applied guidance and a decision tree so the UI can render worker reasoning consistently."""

        policy = self._policy_from_request(request, worker_name)
        outputs = dict(response.outputs)
        outputs["applied_guidance"] = {
            "worker_name": worker_name,
            "guidance_enabled": bool(policy and policy.enabled),
            "operator_recommendations": policy.operator_recommendations if policy else [],
            "decision_preferences": policy.decision_preferences if policy else [],
            "competence_boundary": policy.competence_boundary if policy else None,
        }
        outputs["decision_tree"] = self.build_decision_tree(worker_name, request, response).model_dump(mode="json")
        response.outputs = outputs
        return response

    def build_decision_tree(self, worker_name: str, request: WorkerRequest, response: WorkerResponse) -> WorkerDecisionTree:
        """Build a compact but readable decision tree for the operator dashboard."""

        policy = self._policy_from_request(request, worker_name)
        input_evidence = [
            f"Goal length: {len(request.goal)} characters",
            f"Repository: {request.repository}",
            f"Prior result keys: {', '.join(sorted(request.prior_results.keys())) or 'none'}",
        ]
        if request.enable_web_research:
            input_evidence.append("General web research was allowed for this task.")

        guidance_node = WorkerDecisionNode(
            id="guidance",
            label="Operator guidance considered",
            evidence=(policy.operator_recommendations if policy else ["No explicit operator guidance configured."]),
            decision=(
                "Use configured worker guidance."
                if policy and policy.enabled
                else "Proceed with default worker behavior."
            ),
            outcome=policy.competence_boundary if policy else "Default worker boundaries apply.",
        )
        execution_node = WorkerDecisionNode(
            id="execution",
            label="Execution path chosen",
            evidence=self._execution_evidence(worker_name, response),
            decision=self._execution_decision(worker_name, response),
            outcome=response.summary,
            children=self._execution_children(worker_name, response),
        )
        risk_node = WorkerDecisionNode(
            id="risk",
            label="Risk and escalation evaluation",
            evidence=response.risk_flags or response.warnings or ["No additional risk signals were reported."],
            decision=(
                "Escalate to human approval."
                if response.requires_human_approval
                else "Continue within the worker competence boundary."
            ),
            outcome=response.approval_reason or "No extra approval required at this stage.",
        )
        outcome_node = WorkerDecisionNode(
            id="outcome",
            label="Outcome",
            evidence=response.errors or ["Worker completed without explicit error output."],
            decision="Persist worker result and expose it in the task detail.",
            outcome=response.summary,
        )
        root = WorkerDecisionNode(
            id="start",
            label="Worker start",
            evidence=input_evidence,
            decision=f"Run worker `{worker_name}` with explicit scope and stored operator guidance.",
            outcome="Decision trace recorded for dashboard review.",
            children=[guidance_node, execution_node, risk_node, outcome_node],
        )
        return WorkerDecisionTree(
            worker_name=worker_name,
            title=f"{worker_name} decision flow",
            root=root,
        )

    def register_worker_suggestions(
        self,
        *,
        worker_name: str,
        request: WorkerRequest,
        response: WorkerResponse,
    ) -> list[ImprovementSuggestion]:
        """Store deduplicated improvement suggestions derived from the worker result."""

        policy = self._policy_from_request(request, worker_name)
        if policy is not None and not policy.auto_submit_improvement_suggestions:
            return []

        generated = self._generate_suggestions(worker_name, request, response, policy)
        if not generated:
            return []

        registry = self.load_suggestion_registry()
        existing_signatures = {
            (item.worker_name, item.task_id or "", item.repository or "", item.title.lower()) for item in registry.suggestions
        }
        added: list[ImprovementSuggestion] = []
        for suggestion in generated:
            signature = (
                suggestion.worker_name,
                suggestion.task_id or "",
                suggestion.repository or "",
                suggestion.title.lower(),
            )
            if signature in existing_signatures:
                continue
            registry.suggestions.append(suggestion)
            existing_signatures.add(signature)
            added.append(suggestion)

        if added:
            self._write_suggestion_registry(self._normalize_suggestion_registry(registry))
        return added

    def load_suggestion_registry(self) -> ImprovementSuggestionRegistry:
        if self.suggestions_path.exists():
            return self._normalize_suggestion_registry(
                ImprovementSuggestionRegistry.model_validate_json(self.suggestions_path.read_text("utf-8"))
            )
        registry = ImprovementSuggestionRegistry(suggestions=[])
        self._write_suggestion_registry(registry)
        return registry

    def list_suggestions(
        self,
        *,
        status: ImprovementSuggestionStatus | None = None,
        task_id: str | None = None,
    ) -> list[ImprovementSuggestion]:
        suggestions = self.load_suggestion_registry().suggestions
        filtered = suggestions
        if status is not None:
            filtered = [item for item in filtered if item.status is status]
        if task_id is not None:
            filtered = [item for item in filtered if item.task_id == task_id]
        return sorted(filtered, key=lambda item: item.updated_at, reverse=True)

    def decide_suggestion(
        self,
        suggestion_id: str,
        request: ImprovementSuggestionDecisionRequest,
    ) -> ImprovementSuggestionRegistry:
        registry = self.load_suggestion_registry()
        matched = False
        for index, suggestion in enumerate(registry.suggestions):
            if suggestion.id != suggestion_id:
                continue
            registry.suggestions[index] = suggestion.model_copy(
                update={
                    "status": request.decision,
                    "actor": request.actor,
                    "decision_note": request.note,
                    "updated_at": datetime.now(UTC),
                }
            )
            matched = True
            break

        if not matched:
            raise WorkerGovernanceError(f"Improvement suggestion `{suggestion_id}` was not found.")
        normalized = self._normalize_suggestion_registry(registry)
        self._write_suggestion_registry(normalized)
        return normalized

    def _policy_from_request(self, request: WorkerRequest, worker_name: str) -> WorkerGuidancePolicy | None:
        raw_map = request.metadata.get("worker_guidance_map", {})
        raw_policy = raw_map.get(worker_name)
        if raw_policy is None:
            try:
                return self.policy_for_worker(worker_name)
            except WorkerGovernanceError:
                return None
        return WorkerGuidancePolicy.model_validate(raw_policy)

    def _load_seed_guidance_registry(self) -> WorkerGuidanceRegistry:
        if not self.seed_path.exists():
            raise WorkerGovernanceError(
                f"Worker guidance seed file `{self.seed_path}` is missing. "
                "Restore it before booting the stack."
            )
        return self._normalize_guidance_registry(
            WorkerGuidanceRegistry.model_validate_json(self.seed_path.read_text("utf-8"))
        )

    def _write_guidance_registry(self, registry: WorkerGuidanceRegistry) -> None:
        self.guidance_path.parent.mkdir(parents=True, exist_ok=True)
        self.guidance_path.write_text(json.dumps(registry.model_dump(mode="json"), indent=2, ensure_ascii=True), encoding="utf-8")

    def _write_suggestion_registry(self, registry: ImprovementSuggestionRegistry) -> None:
        self.suggestions_path.parent.mkdir(parents=True, exist_ok=True)
        self.suggestions_path.write_text(
            json.dumps(registry.model_dump(mode="json"), indent=2, ensure_ascii=True),
            encoding="utf-8",
        )

    def _normalize_guidance_registry(self, registry: WorkerGuidanceRegistry) -> WorkerGuidanceRegistry:
        seen_workers: set[str] = set()
        normalized_workers: list[WorkerGuidancePolicy] = []
        for item in registry.workers:
            normalized = self._normalize_guidance_policy(item, previous_policy=None)
            if normalized.worker_name in seen_workers:
                raise WorkerGovernanceError(f"Duplicate worker guidance entry for `{normalized.worker_name}`.")
            seen_workers.add(normalized.worker_name)
            normalized_workers.append(normalized)
        return WorkerGuidanceRegistry(workers=sorted(normalized_workers, key=lambda item: item.display_name.lower()))

    def _normalize_guidance_policy(
        self,
        policy: WorkerGuidancePolicy,
        previous_policy: WorkerGuidancePolicy | None,
    ) -> WorkerGuidancePolicy:
        created_at = previous_policy.created_at if previous_policy is not None else policy.created_at
        return WorkerGuidancePolicy(
            worker_name=policy.worker_name.strip(),
            display_name=policy.display_name.strip(),
            enabled=policy.enabled,
            role_summary=policy.role_summary.strip(),
            operator_recommendations=[item.strip() for item in policy.operator_recommendations if item.strip()],
            decision_preferences=[item.strip() for item in policy.decision_preferences if item.strip()],
            competence_boundary=policy.competence_boundary.strip(),
            escalate_beyond_boundary=policy.escalate_beyond_boundary,
            auto_submit_improvement_suggestions=policy.auto_submit_improvement_suggestions,
            created_at=created_at,
            updated_at=datetime.now(UTC),
        )

    def _normalize_suggestion_registry(self, registry: ImprovementSuggestionRegistry) -> ImprovementSuggestionRegistry:
        normalized = [self._normalize_suggestion(item) for item in registry.suggestions]
        return ImprovementSuggestionRegistry(
            suggestions=sorted(normalized, key=lambda item: item.updated_at, reverse=True)
        )

    def _normalize_suggestion(self, suggestion: ImprovementSuggestion) -> ImprovementSuggestion:
        return ImprovementSuggestion(
            id=suggestion.id.strip() or str(uuid4()),
            worker_name=suggestion.worker_name.strip(),
            task_id=suggestion.task_id,
            repository=suggestion.repository,
            title=suggestion.title.strip(),
            summary=suggestion.summary.strip(),
            rationale=suggestion.rationale.strip(),
            suggested_action=suggestion.suggested_action.strip(),
            impact=suggestion.impact.strip() or "medium",
            exceeds_competence_boundary=suggestion.exceeds_competence_boundary,
            requires_ceo_approval=suggestion.requires_ceo_approval,
            status=suggestion.status,
            actor=suggestion.actor,
            decision_note=suggestion.decision_note,
            created_at=suggestion.created_at,
            updated_at=suggestion.updated_at,
        )

    def _execution_evidence(self, worker_name: str, response: WorkerResponse) -> list[str]:
        outputs = response.outputs
        evidence = [f"Output keys: {', '.join(sorted(outputs.keys())) or 'none'}"]
        if worker_name == "research":
            plan = outputs.get("sources", {}).get("trusted_source_plan", {})
            evidence.append(
                f"Trusted matches: {len(plan.get('trusted_matches', [])) if isinstance(plan, dict) else 0}"
            )
            evidence.append(
                f"General fallback results: {len(outputs.get('sources', {}).get('general_web_results', []))}"
            )
        elif worker_name == "coding":
            evidence.append(f"Changed files: {len(outputs.get('changed_files', []))}")
        elif worker_name == "tester":
            evidence.append(f"Command results: {len(outputs.get('results', []))}")
        elif worker_name == "reviewer":
            evidence.append(f"Findings: {len(outputs.get('findings', []))}")
        elif worker_name == "deploy":
            evidence.append(f"Deployment target: {outputs.get('project_dir', 'unknown')}")
        return evidence

    def _execution_decision(self, worker_name: str, response: WorkerResponse) -> str:
        if worker_name == "research":
            return "Select trusted sources first and only use fallback search when allowed and necessary."
        if worker_name == "coding":
            return "Apply the smallest safe repository change set that satisfies the approved scope."
        if worker_name == "tester":
            return "Run only allowed commands and capture failures explicitly."
        if worker_name == "reviewer":
            return "Escalate material risks instead of silently accepting them."
        if worker_name == "deploy":
            return "Allow staging-only deployment and reject production targets."
        return "Execute the specialized worker role with explicit scope and audit output."

    def _execution_children(self, worker_name: str, response: WorkerResponse) -> list[WorkerDecisionNode]:
        outputs = response.outputs
        if worker_name == "research":
            plan = outputs.get("sources", {}).get("trusted_source_plan", {})
            return [
                WorkerDecisionNode(
                    id="source-selection",
                    label="Source routing",
                    evidence=[
                        f"Question type: {plan.get('inferred_question_type', 'unknown')}",
                        f"Ecosystem: {plan.get('inferred_ecosystem', 'unknown')}",
                    ]
                    + ([f"Fallback reason: {plan.get('fallback_reason')}"] if plan.get("fallback_reason") else []),
                    decision="Prefer official structured sources over HTML or fallback search.",
                    outcome=f"{len(plan.get('trusted_matches', [])) if isinstance(plan, dict) else 0} trusted source(s) matched.",
                )
            ]
        if worker_name == "coding":
            return [
                WorkerDecisionNode(
                    id="change-set",
                    label="Change set",
                    evidence=outputs.get("changed_files", []) or ["No changed files reported."],
                    decision="Apply generated operations only inside the repository boundary.",
                    outcome=outputs.get("diff_stat", "No diff stat available."),
                )
            ]
        if worker_name == "tester":
            return [
                WorkerDecisionNode(
                    id="commands",
                    label="Test command execution",
                    evidence=[
                        f"{item.get('stage', 'stage')}: {item.get('command', 'unknown command')}"
                        for item in outputs.get("results", [])[:6]
                    ]
                    or ["No commands were executed."],
                    decision="Respect command allowlist and capture each command result.",
                    outcome="Per-command results stored in the test report.",
                )
            ]
        return []

    def _generate_suggestions(
        self,
        worker_name: str,
        request: WorkerRequest,
        response: WorkerResponse,
        policy: WorkerGuidancePolicy | None,
    ) -> list[ImprovementSuggestion]:
        suggestions: list[ImprovementSuggestion] = []
        outputs = response.outputs
        now = datetime.now(UTC)

        def append_suggestion(
            *,
            title: str,
            summary: str,
            rationale: str,
            action: str,
            impact: str = "medium",
            exceeds: bool = False,
        ) -> None:
            requires_ceo_approval = exceeds or bool(policy and policy.escalate_beyond_boundary)
            suggestions.append(
                ImprovementSuggestion(
                    id=str(uuid4()),
                    worker_name=worker_name,
                    task_id=request.task_id,
                    repository=request.repository,
                    title=title,
                    summary=summary,
                    rationale=rationale,
                    suggested_action=action,
                    impact=impact,
                    exceeds_competence_boundary=exceeds,
                    requires_ceo_approval=requires_ceo_approval if exceeds else False,
                    status=ImprovementSuggestionStatus.PENDING,
                    created_at=now,
                    updated_at=now,
                )
            )

        if worker_name == "research":
            plan = outputs.get("sources", {}).get("trusted_source_plan", {})
            if isinstance(plan, dict) and plan.get("fallback_reason"):
                append_suggestion(
                    title="Trusted coding sources erweitern",
                    summary="Der Research Worker hatte zu wenige passende Trusted Sources für diese Fragestellung.",
                    rationale=str(plan.get("fallback_reason")),
                    action="Prüfe, ob zusätzliche offizielle Quellen oder ein besser passendes Profil ergänzt werden sollen.",
                    impact="medium",
                    exceeds=True,
                )

        if worker_name == "tester":
            if response.errors or not outputs.get("results"):
                append_suggestion(
                    title="Repo-spezifische Testbefehle hinterlegen",
                    summary="Der Test Worker konnte den Testpfad nicht sauber ausführen.",
                    rationale="Fehlende oder fehlerhafte Testbefehle erhöhen das Risiko für unbemerkte Regressionen.",
                    action="Definiere klare Lint-, Typing- und Testbefehle für dieses Repository.",
                    impact="high",
                    exceeds=False,
                )

        if worker_name == "reviewer" and any("test update" in finding.lower() for finding in outputs.get("findings", [])):
            append_suggestion(
                title="Test-Policy für Codeänderungen schärfen",
                summary="Der Reviewer sieht Codeänderungen ohne klare Testanpassung.",
                rationale="Wiederkehrende fehlende Tests deuten auf eine Prozesslücke im Repository hin.",
                action="Lege verbindliche Test-Erwartungen oder PR-Checks für Codeänderungen fest.",
                impact="high",
                exceeds=True,
            )

        if worker_name == "security" and response.risk_flags:
            append_suggestion(
                title="Sicherheitsleitplanken im Ziel-Repo ausbauen",
                summary="Der Security Worker hat wiederkehrende Risikoindikatoren erkannt.",
                rationale=", ".join(response.risk_flags),
                action="Prüfe zusätzliche Secret-, Infra- oder Review-Gates für dieses Repository.",
                impact="high",
                exceeds=True,
            )

        if worker_name == "architecture" and outputs.get("approval_gates"):
            append_suggestion(
                title="Repository-spezifische Governance dokumentieren",
                summary="Die Architektur empfiehlt mehrere Freigabepunkte, die noch nicht als Repo-Profil hinterlegt sind.",
                rationale="Wiederkehrende Approval-Gates sprechen für dokumentierte Standards statt Einzelfallwissen.",
                action="Lege ein Repository-Profil oder ADR für Freigabepunkte, Deploy-Grenzen und Infrastrukturänderungen an.",
                impact="medium",
                exceeds=True,
            )

        return suggestions
