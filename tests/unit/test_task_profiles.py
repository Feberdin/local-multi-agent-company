"""
Purpose: Verify that narrow deterministic task profiles are inferred only for well-known tiny fix shapes.
Input/Output: Tests call the profile helper with representative goals and inspect the returned metadata.
Important invariants:
  - The timeout fast path must trigger on the real WORKER_STAGE_TIMEOUT_SECONDS goal.
  - Even a mistaken `worker.py` mention must still resolve to the config-based fix path.
How to debug: If this fails, inspect services/shared/agentic_lab/task_profiles.py and the goal normalization rules.
"""

from __future__ import annotations

from services.shared.agentic_lab.task_profiles import infer_task_profile


def test_infer_task_profile_detects_worker_stage_timeout_config_fix() -> None:
    profile = infer_task_profile(
        "Change WORKER_STAGE_TIMEOUT_SECONDS to 3600 in worker.py",
        {"problem_class": "timeout"},
    )

    assert profile is not None
    assert profile["name"] == "worker_stage_timeout_config_fix"
    assert profile["target_timeout_seconds"] == 3600.0
    assert profile["target_files"][0] == "services/shared/agentic_lab/config.py"
    assert profile["skip_research"] is True
    assert profile["route_after_coding"] == "validation"


def test_infer_task_profile_does_not_overmatch_generic_timeout_discussion() -> None:
    profile = infer_task_profile(
        "Investigate why a worker timed out overnight and summarize possible causes.",
        {"problem_class": "timeout"},
    )

    assert profile is None
