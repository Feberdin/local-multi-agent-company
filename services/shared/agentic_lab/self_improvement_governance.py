"""
Purpose: Central governance policy for autonomous self-improvement cycles.
Input/Output: Reads a small YAML policy and turns (mode, risk_level) into one explicit
              execution decision that the self-improvement pipeline can audit and display.
Important invariants:
  - The policy only applies to the system's own repository.
  - High-risk work may prepare artifacts but must not publish silently.
  - Critical work remains analysis-only until a human deliberately approves it.
How to debug: If a cycle starts, pauses, or skips unexpectedly, inspect the loaded policy
              path and the resolved GovernanceDecision for that mode/risk pair.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, Field

from services.shared.agentic_lab.config import Settings


class SelfImprovementMode(StrEnum):
    MANUAL = "manual"
    ASSISTED = "assisted"
    AUTOMATIC = "automatic"


class GovernanceAction(StrEnum):
    ANALYZE_ONLY = "analyze_only"
    EXECUTE_AUTONOMOUSLY = "execute_autonomously"
    EXECUTE_AND_NOTIFY = "execute_and_notify"
    PREPARE_AND_AWAIT_APPROVAL = "prepare_and_await_approval"


class GovernanceStatus(StrEnum):
    PENDING = "pending"
    ANALYZED = "analyzed"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    BLOCKED = "blocked"
    IMPLEMENTED = "implemented"
    FAILED = "failed"


class ApprovalEmailIntent(StrEnum):
    NONE = "none"
    INFO = "info"
    APPROVAL = "approval"


class GovernanceRule(BaseModel):
    action: GovernanceAction
    email_intent: ApprovalEmailIntent = ApprovalEmailIntent.NONE
    allow_deploy: bool = False
    require_publish_approval: bool = False


class RepositoryScopePolicy(BaseModel):
    repository: str
    local_repo_path: str
    docs_root: str = "docs/automation"
    ai_change_index: str = "docs/automation/ai-change-index.md"
    approval_gate_name: str = "self-improvement-risk-review"


class SelfImprovementGovernancePolicy(BaseModel):
    repository_scope: RepositoryScopePolicy
    mode_rules: dict[str, dict[str, GovernanceRule]] = Field(default_factory=dict)


class GovernanceDecision(BaseModel):
    mode: str
    risk_level: str
    action: GovernanceAction
    governance_status: GovernanceStatus
    email_intent: ApprovalEmailIntent
    allow_task_execution: bool
    allow_deploy: bool
    require_publish_approval: bool
    approval_gate_name: str
    note: str


def normalize_self_improvement_mode(raw_mode: str | None) -> SelfImprovementMode:
    """Accept legacy `auto` and normalize every caller onto one explicit mode value."""

    normalized = (raw_mode or SelfImprovementMode.MANUAL.value).strip().lower()
    if normalized == "auto":
        normalized = SelfImprovementMode.AUTOMATIC.value
    try:
        return SelfImprovementMode(normalized)
    except ValueError:
        return SelfImprovementMode.MANUAL


def default_self_improvement_policy(settings: Settings) -> SelfImprovementGovernancePolicy:
    """Provide a safe inline fallback when no policy file has been mounted yet."""

    repository_scope = RepositoryScopePolicy(
        repository=settings.self_improvement_target_repo,
        local_repo_path=settings.self_improvement_local_repo_path,
    )

    def _rule(
        action: GovernanceAction,
        *,
        email_intent: ApprovalEmailIntent,
        allow_deploy: bool,
        require_publish_approval: bool,
    ) -> GovernanceRule:
        return GovernanceRule(
            action=action,
            email_intent=email_intent,
            allow_deploy=allow_deploy,
            require_publish_approval=require_publish_approval,
        )

    return SelfImprovementGovernancePolicy(
        repository_scope=repository_scope,
        mode_rules={
            SelfImprovementMode.MANUAL.value: {
                "low": _rule(
                    GovernanceAction.ANALYZE_ONLY,
                    email_intent=ApprovalEmailIntent.INFO,
                    allow_deploy=False,
                    require_publish_approval=False,
                ),
                "medium": _rule(
                    GovernanceAction.ANALYZE_ONLY,
                    email_intent=ApprovalEmailIntent.INFO,
                    allow_deploy=False,
                    require_publish_approval=False,
                ),
                "high": _rule(
                    GovernanceAction.ANALYZE_ONLY,
                    email_intent=ApprovalEmailIntent.APPROVAL,
                    allow_deploy=False,
                    require_publish_approval=False,
                ),
                "critical": _rule(
                    GovernanceAction.ANALYZE_ONLY,
                    email_intent=ApprovalEmailIntent.APPROVAL,
                    allow_deploy=False,
                    require_publish_approval=False,
                ),
            },
            SelfImprovementMode.ASSISTED.value: {
                "low": _rule(
                    GovernanceAction.EXECUTE_AUTONOMOUSLY,
                    email_intent=ApprovalEmailIntent.NONE,
                    allow_deploy=False,
                    require_publish_approval=False,
                ),
                "medium": _rule(
                    GovernanceAction.EXECUTE_AND_NOTIFY,
                    email_intent=ApprovalEmailIntent.INFO,
                    allow_deploy=False,
                    require_publish_approval=False,
                ),
                "high": _rule(
                    GovernanceAction.PREPARE_AND_AWAIT_APPROVAL,
                    email_intent=ApprovalEmailIntent.APPROVAL,
                    allow_deploy=False,
                    require_publish_approval=True,
                ),
                "critical": _rule(
                    GovernanceAction.ANALYZE_ONLY,
                    email_intent=ApprovalEmailIntent.APPROVAL,
                    allow_deploy=False,
                    require_publish_approval=False,
                ),
            },
            SelfImprovementMode.AUTOMATIC.value: {
                "low": _rule(
                    GovernanceAction.EXECUTE_AUTONOMOUSLY,
                    email_intent=ApprovalEmailIntent.NONE,
                    allow_deploy=True,
                    require_publish_approval=False,
                ),
                "medium": _rule(
                    GovernanceAction.EXECUTE_AND_NOTIFY,
                    email_intent=ApprovalEmailIntent.INFO,
                    allow_deploy=True,
                    require_publish_approval=False,
                ),
                "high": _rule(
                    GovernanceAction.PREPARE_AND_AWAIT_APPROVAL,
                    email_intent=ApprovalEmailIntent.APPROVAL,
                    allow_deploy=False,
                    require_publish_approval=True,
                ),
                "critical": _rule(
                    GovernanceAction.ANALYZE_ONLY,
                    email_intent=ApprovalEmailIntent.APPROVAL,
                    allow_deploy=False,
                    require_publish_approval=False,
                ),
            },
        },
    )


class SelfImprovementGovernanceService:
    """Load policy once per call-site and resolve operator-visible governance decisions."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def load_policy(self) -> SelfImprovementGovernancePolicy:
        """Load YAML policy when present, otherwise fall back to safe inline defaults."""

        config_path = Path(self.settings.self_improvement_policy_path)
        if not config_path.exists():
            return default_self_improvement_policy(self.settings)

        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        try:
            policy = SelfImprovementGovernancePolicy.model_validate(raw)
        except Exception:
            return default_self_improvement_policy(self.settings)

        # Backfill repository scope from settings when the file omits host-specific paths.
        repository_scope = policy.repository_scope.model_copy(
            update={
                "repository": policy.repository_scope.repository or self.settings.self_improvement_target_repo,
                "local_repo_path": policy.repository_scope.local_repo_path or self.settings.self_improvement_local_repo_path,
            }
        )
        return policy.model_copy(update={"repository_scope": repository_scope})

    def decide(self, *, risk_level: str, mode: str | None = None) -> GovernanceDecision:
        """Resolve one explicit decision for a cycle based on the current policy."""

        normalized_mode = normalize_self_improvement_mode(mode or self.settings.self_improvement_mode)
        normalized_risk = (risk_level or "low").strip().lower()
        if normalized_risk not in {"low", "medium", "high", "critical"}:
            normalized_risk = "low"

        policy = self.load_policy()
        mode_rules = policy.mode_rules.get(normalized_mode.value, {})
        rule = mode_rules.get(normalized_risk)
        if rule is None:
            rule = default_self_improvement_policy(self.settings).mode_rules[normalized_mode.value][normalized_risk]

        if rule.action == GovernanceAction.ANALYZE_ONLY and rule.email_intent == ApprovalEmailIntent.APPROVAL:
            governance_status = GovernanceStatus.AWAITING_APPROVAL
            allow_task_execution = False
            note = "Analyse ist abgeschlossen. Vor einer Umsetzung ist eine ausdrueckliche Freigabe erforderlich."
        elif rule.action == GovernanceAction.ANALYZE_ONLY:
            governance_status = GovernanceStatus.ANALYZED
            allow_task_execution = False
            note = "Im aktuellen Modus endet der Zyklus nach der Analyse ohne automatische Codeaenderung."
        elif rule.action == GovernanceAction.PREPARE_AND_AWAIT_APPROVAL:
            governance_status = GovernanceStatus.PENDING
            allow_task_execution = True
            note = "Der Zyklus darf Branch und Testergebnisse vorbereiten, pausiert aber vor der Veroeffentlichung."
        elif rule.action == GovernanceAction.EXECUTE_AND_NOTIFY:
            governance_status = GovernanceStatus.PENDING
            allow_task_execution = True
            note = "Der Zyklus darf autonom arbeiten und verschickt zusaetzlich eine informative E-Mail."
        else:
            governance_status = GovernanceStatus.PENDING
            allow_task_execution = True
            note = "Der Zyklus darf innerhalb des freigegebenen Scopes autonom weiterarbeiten."

        return GovernanceDecision(
            mode=normalized_mode.value,
            risk_level=normalized_risk,
            action=rule.action,
            governance_status=governance_status,
            email_intent=rule.email_intent,
            allow_task_execution=allow_task_execution,
            allow_deploy=rule.allow_deploy and self.settings.self_improvement_deploy_after_success,
            require_publish_approval=rule.require_publish_approval,
            approval_gate_name=policy.repository_scope.approval_gate_name,
            note=note,
        )
