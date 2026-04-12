"""
Purpose: Typed report models for the operator-facing readiness and diagnostics dashboard.
Input/Output: The readiness runner returns these models so the API and UI can render partial
results even when individual checks fail.
Important invariants: A readiness report is always serializable, every check result is explicit,
and operators never need to infer state from a missing field.
How to debug: If the dashboard looks inconsistent, inspect one `ReadinessCheckResult` first and
verify its status, severity, timing fields, and hint text.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ReadinessCheckStatus(StrEnum):
    OK = "ok"
    WARNING = "warning"
    FAIL = "fail"
    SKIPPED = "skipped"
    RUNNING = "running"


class ReadinessSeverity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ReadinessMode(StrEnum):
    QUICK = "quick"
    DEEP = "deep"


class ReadinessCheckResult(BaseModel):
    id: str
    category: str
    name: str
    status: ReadinessCheckStatus
    severity: ReadinessSeverity
    started_at: datetime
    finished_at: datetime
    duration_ms: float
    message: str
    detail: str = ""
    hint: str = ""
    target: str | None = None
    raw_value: Any | None = None
    depends_on: list[str] = Field(default_factory=list)


class ReadinessSummary(BaseModel):
    total: int = 0
    ok: int = 0
    warning: int = 0
    fail: int = 0
    skipped: int = 0
    running: int = 0


class ReadinessCategorySummary(BaseModel):
    id: str
    label: str
    status: ReadinessCheckStatus
    total: int = 0
    ok: int = 0
    warning: int = 0
    fail: int = 0
    skipped: int = 0
    running: int = 0


class ReadinessRecommendation(BaseModel):
    id: str
    priority: int = 0
    category: str
    title: str
    message: str
    related_check_ids: list[str] = Field(default_factory=list)


class ReadinessReport(BaseModel):
    mode: ReadinessMode
    overall_status: ReadinessCheckStatus
    started_at: datetime
    finished_at: datetime
    duration_ms: float
    summary: ReadinessSummary
    categories: list[ReadinessCategorySummary] = Field(default_factory=list)
    checks: list[ReadinessCheckResult] = Field(default_factory=list)
    environment_overview: dict[str, Any] = Field(default_factory=dict)
    recommendations: list[ReadinessRecommendation] = Field(default_factory=list)
    ready_for_workflows: bool = False
    headline: str = ""
    summary_message: str = ""
