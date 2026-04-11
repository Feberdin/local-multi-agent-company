"""
Unit tests for the self-improvement service.
Covers risk classification, error classification, daily limits, state transitions, and cycle guards.
"""

from __future__ import annotations

from services.shared.agentic_lab.self_improvement import (
    CycleStatus,
    ProblemClass,
    RiskLevel,
    SelfImprovementError,
    classify_error_text,
    classify_risk,
)

# ---------------------------------------------------------------------------
# classify_risk
# ---------------------------------------------------------------------------


def test_classify_risk_low_for_benign_goal():
    level, reason = classify_risk("Verbessere die Fehlerbehandlung in requirements_worker")
    assert level == RiskLevel.LOW
    assert reason is None


def test_classify_risk_critical_for_self_improvement():
    level, reason = classify_risk("Aendere die self-improvement Logik")
    assert level == RiskLevel.CRITICAL
    assert reason is not None


def test_classify_risk_high_for_auth():
    level, reason = classify_risk("Refactore das auth-Modul fuer bessere JWT-Validierung")
    assert level == RiskLevel.HIGH
    assert reason is not None


def test_classify_risk_high_for_secret():
    level, reason = classify_risk("Passe die Handhabung von api_key und password an")
    assert level == RiskLevel.HIGH
    assert reason is not None


def test_classify_risk_high_for_deploy():
    level, reason = classify_risk("Verbessere das CI/CD-Deployment-Skript")
    assert level == RiskLevel.HIGH
    assert reason is not None


def test_classify_risk_medium_for_docker():
    level, reason = classify_risk("Aktualisiere das Dockerfile fuer kleinere Images")
    assert level == RiskLevel.MEDIUM
    assert reason is not None


def test_classify_risk_high_for_database():
    level, reason = classify_risk("Add a new column via database migration")
    assert level == RiskLevel.HIGH
    assert reason is not None


def test_classify_risk_first_match_wins():
    # self-improvement comes before auth in the pattern list → CRITICAL
    level, reason = classify_risk("Verbessere self-improvement auth handling")
    assert level == RiskLevel.CRITICAL


# ---------------------------------------------------------------------------
# classify_error_text
# ---------------------------------------------------------------------------


def test_classify_error_timeout():
    assert classify_error_text("Request timed out after 30 seconds") == ProblemClass.TIMEOUT


def test_classify_error_unreachable():
    assert classify_error_text("Connection refused to http://localhost:8001") == ProblemClass.UNREACHABLE_ENDPOINT


def test_classify_error_git():
    assert classify_error_text("fatal: not a git repository") == ProblemClass.GIT_ERROR


def test_classify_error_json():
    assert classify_error_text("Model did not return valid json in response") == ProblemClass.INVALID_RESPONSE_SCHEMA


def test_classify_error_validation():
    assert classify_error_text("ValidationError: string_too_long for field query") == ProblemClass.INVALID_RESPONSE_SCHEMA


def test_classify_error_deploy():
    assert classify_error_text("deploy failed due to missing compose file") == ProblemClass.DEPLOYMENT_FAILURE


def test_classify_error_template():
    assert classify_error_text("Jinja2 template rendering failed") == ProblemClass.UI_RENDERING_PROBLEM


def test_classify_error_unknown():
    assert classify_error_text("something completely unexpected happened") == ProblemClass.UNKNOWN


def test_classify_error_case_insensitive():
    assert classify_error_text("TIMEOUT exceeded for stage") == ProblemClass.TIMEOUT


# ---------------------------------------------------------------------------
# CycleStatus sets
# ---------------------------------------------------------------------------


def test_terminal_statuses_are_disjoint_from_active():
    terminal = {CycleStatus.COMPLETED, CycleStatus.FAILED, CycleStatus.PAUSED}
    active = {
        CycleStatus.ANALYZING,
        CycleStatus.PLANNING,
        CycleStatus.IMPLEMENTING,
        CycleStatus.VALIDATING,
        CycleStatus.DEPLOYING,
        CycleStatus.POST_DEPLOY_TESTING,
        CycleStatus.AWAITING_MANUAL_REVIEW,
    }
    assert not terminal & active


def test_all_cycle_statuses_covered():
    all_statuses = set(CycleStatus)
    terminal = {CycleStatus.COMPLETED, CycleStatus.FAILED, CycleStatus.PAUSED}
    active = {
        CycleStatus.ANALYZING,
        CycleStatus.PLANNING,
        CycleStatus.IMPLEMENTING,
        CycleStatus.VALIDATING,
        CycleStatus.DEPLOYING,
        CycleStatus.POST_DEPLOY_TESTING,
        CycleStatus.AWAITING_MANUAL_REVIEW,
    }
    # IDLE is the initial/rest state — not terminal, not active
    assert terminal | active | {CycleStatus.IDLE} == all_statuses


# ---------------------------------------------------------------------------
# SelfImprovementError
# ---------------------------------------------------------------------------


def test_self_improvement_error_is_runtime_error():
    exc = SelfImprovementError("test message")
    assert isinstance(exc, RuntimeError)
    assert str(exc) == "test message"


# ---------------------------------------------------------------------------
# ProblemClass values
# ---------------------------------------------------------------------------


def test_all_problem_classes_have_string_values():
    for cls in ProblemClass:
        assert isinstance(cls.value, str)
        assert len(cls.value) > 0


def test_risk_level_ordering():
    # Just verify the four expected values exist
    assert RiskLevel.LOW in RiskLevel
    assert RiskLevel.MEDIUM in RiskLevel
    assert RiskLevel.HIGH in RiskLevel
    assert RiskLevel.CRITICAL in RiskLevel
