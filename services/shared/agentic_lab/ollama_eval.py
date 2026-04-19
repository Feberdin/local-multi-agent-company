"""
Purpose: Shared helpers for classifying and summarizing live Ollama output probes.
Input/Output: Accepts raw assistant content, reasoning snippets, HTTP metadata, and
returns stable operator-facing classifications.
Important invariants:
  - `success` means the provider returned visible content in the expected shape.
  - `degraded` means the provider answered, but only in a reasoning-only or otherwise
    operator-actionable fallback shape.
  - `failure` means transport, HTTP, or unusable output with no helpful reasoning signal.
How to debug: If batch evaluations look surprising, inspect `classify_probe_outcome()`
first because all summary counters derive from that function.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

ProbeExpectation = Literal["json_visible", "text_visible"]
ProbeOutcome = Literal["success", "degraded", "failure"]
ProbeContentShape = Literal["visible_json", "visible_text", "reasoning_only", "empty", "non_json_when_json_expected"]


@dataclass(frozen=True, slots=True)
class OllamaProbeClassification:
    """Normalized shape for one live output probe."""

    outcome: ProbeOutcome
    content_shape: ProbeContentShape
    visible_content: bool
    parseable_json: bool
    reasoning_present: bool
    finish_reason: str
    explanation: str

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-serializable representation for reports."""

        return asdict(self)


def classify_probe_outcome(
    *,
    expectation: ProbeExpectation,
    content: str,
    reasoning: str,
    finish_reason: str,
) -> OllamaProbeClassification:
    """Classify one raw Ollama reply into success, degraded, or hard failure."""

    normalized_content = content.strip()
    normalized_reasoning = reasoning.strip()
    has_content = bool(normalized_content)
    has_reasoning = bool(normalized_reasoning)
    parseable_json = _is_parseable_json_object(normalized_content) if has_content else False
    normalized_finish_reason = finish_reason.strip()

    if expectation == "json_visible":
        if parseable_json:
            return OllamaProbeClassification(
                outcome="success",
                content_shape="visible_json",
                visible_content=True,
                parseable_json=True,
                reasoning_present=has_reasoning,
                finish_reason=normalized_finish_reason,
                explanation="Sichtbares JSON wurde direkt im Assistant-Content geliefert.",
            )
        if has_content:
            return OllamaProbeClassification(
                outcome="failure",
                content_shape="non_json_when_json_expected",
                visible_content=True,
                parseable_json=False,
                reasoning_present=has_reasoning,
                finish_reason=normalized_finish_reason,
                explanation="Es gab sichtbaren Content, aber kein parsebares JSON obwohl JSON erwartet wurde.",
            )
        if has_reasoning:
            return OllamaProbeClassification(
                outcome="degraded",
                content_shape="reasoning_only",
                visible_content=False,
                parseable_json=False,
                reasoning_present=True,
                finish_reason=normalized_finish_reason,
                explanation="Es wurde nur Reasoning ohne sichtbaren Assistant-Content geliefert.",
            )
        return OllamaProbeClassification(
            outcome="failure",
            content_shape="empty",
            visible_content=False,
            parseable_json=False,
            reasoning_present=False,
            finish_reason=normalized_finish_reason,
            explanation="Weder sichtbarer Content noch Reasoning wurden geliefert.",
        )

    if has_content:
        return OllamaProbeClassification(
            outcome="success",
            content_shape="visible_json" if parseable_json else "visible_text",
            visible_content=True,
            parseable_json=parseable_json,
            reasoning_present=has_reasoning,
            finish_reason=normalized_finish_reason,
            explanation="Sichtbarer Assistant-Content wurde geliefert.",
        )
    if has_reasoning:
        return OllamaProbeClassification(
            outcome="degraded",
            content_shape="reasoning_only",
            visible_content=False,
            parseable_json=False,
            reasoning_present=True,
            finish_reason=normalized_finish_reason,
            explanation="Es wurde nur Reasoning ohne sichtbaren Assistant-Content geliefert.",
        )
    return OllamaProbeClassification(
        outcome="failure",
        content_shape="empty",
        visible_content=False,
        parseable_json=False,
        reasoning_present=False,
        finish_reason=normalized_finish_reason,
        explanation="Weder sichtbarer Content noch Reasoning wurden geliefert.",
    )


def summarize_provider_results(classifications: list[OllamaProbeClassification]) -> dict[str, object]:
    """Aggregate one provider's probe classifications into operator-facing counters."""

    total = len(classifications)
    success = sum(1 for item in classifications if item.outcome == "success")
    degraded = sum(1 for item in classifications if item.outcome == "degraded")
    failure = sum(1 for item in classifications if item.outcome == "failure")
    visible_json = sum(1 for item in classifications if item.content_shape == "visible_json")
    reasoning_only = sum(1 for item in classifications if item.content_shape == "reasoning_only")
    return {
        "total": total,
        "success": success,
        "degraded": degraded,
        "failure": failure,
        "visible_json": visible_json,
        "reasoning_only": reasoning_only,
        "success_rate": _safe_rate(success, total),
        "degraded_rate": _safe_rate(degraded, total),
        "failure_rate": _safe_rate(failure, total),
        "visible_json_rate": _safe_rate(visible_json, total),
        "reasoning_only_rate": _safe_rate(reasoning_only, total),
    }


def recommend_provider_actions(*, provider_name: str, summary: dict[str, object]) -> list[str]:
    """Generate small, practical recommendations from batch-eval counters."""

    degraded_rate = float(summary.get("degraded_rate") or 0.0)
    failure_rate = float(summary.get("failure_rate") or 0.0)
    visible_json_rate = float(summary.get("visible_json_rate") or 0.0)

    recommendations: list[str] = []
    if degraded_rate >= 0.5:
        recommendations.append(
            f"{provider_name}: Für JSON-kritische Worker nicht als Primärmodell verwenden, "
            "weil zu oft nur Reasoning ohne sichtbaren Content kommt."
        )
    if failure_rate > 0.0:
        recommendations.append(
            f"{provider_name}: Hard-Failures im Batch prüfen; Transport- oder Promptfehler "
            "sollten vor produktiven Läufen abgefangen werden."
        )
    if visible_json_rate >= 0.8:
        recommendations.append(
            f"{provider_name}: Geeignet für kleine strukturierte Smoke-Tests, weil sichtbares JSON meist direkt geliefert wird."
        )
    if not recommendations:
        recommendations.append(f"{provider_name}: Keine unmittelbare Routing-Anpassung aus diesem Batch nötig.")
    return recommendations


def _is_parseable_json_object(content: str) -> bool:
    """Treat only JSON objects as successful structured payloads."""

    import json

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return False
    return isinstance(parsed, dict)


def _safe_rate(value: int, total: int) -> float:
    """Avoid division errors in small operator-facing summaries."""

    if total <= 0:
        return 0.0
    return round(value / total, 4)
