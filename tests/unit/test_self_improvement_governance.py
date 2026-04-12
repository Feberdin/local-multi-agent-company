"""
Purpose: Verify the governance policy that controls autonomous self-improvement decisions.
Input/Output: The tests feed mode/risk combinations into the governance service and assert
              the resulting execution permissions, notification intent, and approval gates.
Important invariants:
  - Legacy mode names like `auto` are normalized safely.
  - High and critical changes stay approval-driven.
  - Low and medium changes keep moving when the configured mode allows it.
How to debug: If these tests fail, inspect the inline defaults and
              config/self-improvement.policy.yaml together.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from services.shared.agentic_lab.config import Settings
from services.shared.agentic_lab.self_improvement_governance import (
    ApprovalEmailIntent,
    GovernanceAction,
    GovernanceStatus,
    SelfImprovementGovernanceService,
    SelfImprovementMode,
    normalize_self_improvement_mode,
)


def _governance_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **overrides: str) -> Settings:
    """Build one isolated Settings object with optional self-improvement overrides."""

    monkeypatch.setenv("SELF_IMPROVEMENT_POLICY_PATH", str(tmp_path / "missing-policy.yaml"))
    for key, value in overrides.items():
        monkeypatch.setenv(key, value)
    return Settings()


def test_normalize_self_improvement_mode_accepts_legacy_auto() -> None:
    assert normalize_self_improvement_mode("auto") == SelfImprovementMode.AUTOMATIC


def test_manual_high_risk_requires_approval_and_stops_after_analysis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _governance_settings(tmp_path, monkeypatch, SELF_IMPROVEMENT_MODE="manual")
    service = SelfImprovementGovernanceService(settings)

    decision = service.decide(risk_level="high", mode="manual")

    assert decision.action == GovernanceAction.ANALYZE_ONLY
    assert decision.governance_status == GovernanceStatus.AWAITING_APPROVAL
    assert decision.allow_task_execution is False
    assert decision.email_intent == ApprovalEmailIntent.APPROVAL


def test_assisted_medium_executes_and_notifies_without_deploy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _governance_settings(
        tmp_path,
        monkeypatch,
        SELF_IMPROVEMENT_MODE="assisted",
        SELF_IMPROVEMENT_DEPLOY_AFTER_SUCCESS="true",
    )
    service = SelfImprovementGovernanceService(settings)

    decision = service.decide(risk_level="medium", mode="assisted")

    assert decision.action == GovernanceAction.EXECUTE_AND_NOTIFY
    assert decision.governance_status == GovernanceStatus.PENDING
    assert decision.allow_task_execution is True
    assert decision.allow_deploy is False
    assert decision.email_intent == ApprovalEmailIntent.INFO


def test_automatic_high_risk_prepares_but_requires_publish_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _governance_settings(
        tmp_path,
        monkeypatch,
        SELF_IMPROVEMENT_MODE="automatic",
        SELF_IMPROVEMENT_DEPLOY_AFTER_SUCCESS="true",
    )
    service = SelfImprovementGovernanceService(settings)

    decision = service.decide(risk_level="high", mode="automatic")

    assert decision.action == GovernanceAction.PREPARE_AND_AWAIT_APPROVAL
    assert decision.governance_status == GovernanceStatus.PENDING
    assert decision.allow_task_execution is True
    assert decision.allow_deploy is False
    assert decision.require_publish_approval is True
    assert decision.email_intent == ApprovalEmailIntent.APPROVAL
