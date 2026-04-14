"""
Purpose: Verify that GitHub Checks and legacy commit statuses are merged into one stable CI summary.
Input/Output: Tests feed representative GitHub API payloads into the normalization helper and inspect the buckets.
Important invariants: Failed checks must stay actionable, pending checks must not look successful, and legacy
                      commit-status contexts must still be considered.
How to debug: If this fails, inspect summarize_commit_checks() in services/shared/agentic_lab/github_client.py.
"""

from __future__ import annotations

from services.shared.agentic_lab.github_client import summarize_commit_checks


def test_summarize_commit_checks_merges_failed_and_pending_sources() -> None:
    summary = summarize_commit_checks(
        head_sha="abc123",
        check_runs_payload={
            "check_runs": [
                {
                    "name": "pytest",
                    "status": "completed",
                    "conclusion": "failure",
                    "details_url": "https://github.com/example/run/1",
                    "output": {"summary": "Two tests failed."},
                },
                {
                    "name": "lint",
                    "status": "in_progress",
                    "conclusion": None,
                    "details_url": "https://github.com/example/run/2",
                    "output": {"summary": "Still running."},
                },
            ]
        },
        status_payload={
            "state": "failure",
            "statuses": [
                {
                    "context": "mypy",
                    "state": "failure",
                    "description": "services/orchestrator/workflow.py has one type error",
                    "target_url": "https://github.com/example/status/1",
                }
            ],
        },
    )

    assert summary["head_sha"] == "abc123"
    assert summary["overall_state"] == "failure"
    assert summary["completed"] is False
    assert [item["name"] for item in summary["failed_checks"]] == ["pytest", "mypy"]
    assert [item["name"] for item in summary["pending_checks"]] == ["lint"]
