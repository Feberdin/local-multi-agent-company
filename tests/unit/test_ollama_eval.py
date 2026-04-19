"""
Purpose: Validate the local classification logic for live Ollama output probes.
Input/Output: Feeds synthetic content/reasoning combinations into the shared evaluator and checks the summarized outcome.
Important invariants: Reasoning-only responses are degraded, visible JSON is success for JSON probes, and empty replies are failures.
How to debug: If these tests fail, inspect `services/shared/agentic_lab/ollama_eval.py` because the batch evaluator uses the same helpers.
"""

from services.shared.agentic_lab.ollama_eval import (
    classify_probe_outcome,
    recommend_provider_actions,
    summarize_provider_results,
)


def test_json_probe_with_visible_json_is_success() -> None:
    result = classify_probe_outcome(
        expectation="json_visible",
        content='{"ok": true}',
        reasoning="",
        finish_reason="stop",
    )

    assert result.outcome == "success"
    assert result.content_shape == "visible_json"
    assert result.parseable_json is True


def test_json_probe_with_reasoning_only_is_degraded() -> None:
    result = classify_probe_outcome(
        expectation="json_visible",
        content="",
        reasoning="Thinking Process: I would return JSON here.",
        finish_reason="length",
    )

    assert result.outcome == "degraded"
    assert result.content_shape == "reasoning_only"
    assert result.reasoning_present is True


def test_text_probe_with_empty_reply_is_failure() -> None:
    result = classify_probe_outcome(
        expectation="text_visible",
        content="",
        reasoning="",
        finish_reason="stop",
    )

    assert result.outcome == "failure"
    assert result.content_shape == "empty"


def test_provider_summary_and_recommendations_flag_reasoning_only_model() -> None:
    summary = summarize_provider_results(
        [
            classify_probe_outcome(
                expectation="json_visible",
                content="",
                reasoning="Thinking Process",
                finish_reason="length",
            )
            for _ in range(6)
        ]
        + [
            classify_probe_outcome(
                expectation="json_visible",
                content='{"ok": true}',
                reasoning="",
                finish_reason="stop",
            )
            for _ in range(4)
        ]
    )

    recommendations = recommend_provider_actions(provider_name="qwen", summary=summary)

    assert summary["degraded"] == 6
    assert summary["visible_json"] == 4
    assert any("nicht als Primärmodell" in item for item in recommendations)
