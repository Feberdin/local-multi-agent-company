"""
Purpose: Public readiness API that keeps the existing import path stable while delegating the
actual diagnostics work to modular check and model modules.
Input/Output: Call `run_system_readiness_check(...)` to receive one structured readiness report
that is always renderable by the API and UI.
Important invariants: The import path stays backward compatible for the orchestrator, and callers
should not need to know whether the report came from a quick check, deep check, or a fallback run.
How to debug: If the readiness endpoint behaves strangely, inspect `readiness_checks.py` for the
concrete check implementation and `readiness_models.py` for the serialized response contract.
"""

from __future__ import annotations

from services.shared.agentic_lab.readiness_checks import (
    ReadinessServices,
    build_catastrophic_readiness_report,
    build_readiness_report,
)
from services.shared.agentic_lab.readiness_models import (
    ReadinessCategorySummary,
    ReadinessCheckResult,
    ReadinessCheckStatus,
    ReadinessMode,
    ReadinessRecommendation,
    ReadinessReport,
    ReadinessSeverity,
    ReadinessSummary,
)

# Backward-compatible aliases so local helpers and older imports do not break abruptly.
CheckStatus = ReadinessCheckStatus
ReadinessCheckItem = ReadinessCheckResult

__all__ = [
    "CheckStatus",
    "ReadinessCategorySummary",
    "ReadinessCheckItem",
    "ReadinessCheckResult",
    "ReadinessCheckStatus",
    "ReadinessMode",
    "ReadinessRecommendation",
    "ReadinessReport",
    "ReadinessServices",
    "ReadinessSeverity",
    "ReadinessSummary",
    "build_catastrophic_readiness_report",
    "build_readiness_report",
    "run_system_readiness_check",
]


async def run_system_readiness_check(
    settings,
    *,
    mode: ReadinessMode = ReadinessMode.QUICK,
    services: ReadinessServices | None = None,
) -> ReadinessReport:
    """Run the readiness report through the new modular runner while keeping the old public API."""

    return await build_readiness_report(settings, mode=mode, services=services)
