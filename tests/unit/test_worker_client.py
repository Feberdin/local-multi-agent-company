"""
Purpose: Verify that worker transport errors are translated into readable operator-facing messages.
Input/Output: Tests build synthetic HTTP and validation failures and inspect the formatted detail text.
Important invariants: HTTP status, URL, and response body preview should remain visible in the final error.
How to debug: If this fails, inspect services/shared/agentic_lab/worker_client.py and compare the helper output with the expected wording.
"""

from __future__ import annotations

import httpx

from services.shared.agentic_lab.worker_client import _http_error_detail


def test_http_error_detail_includes_response_body_preview() -> None:
    request = httpx.Request("POST", "http://research-worker:8091/run")
    response = httpx.Response(500, request=request, text="traceback: dirty worktree prevented checkout")
    exc = httpx.HTTPStatusError("Server error", request=request, response=response)

    detail = _http_error_detail("http://research-worker:8091", exc, 2, 3)

    assert "HTTP 500" in str(detail)
    assert "attempt 2/3" in str(detail)
    assert "dirty worktree prevented checkout" in str(detail)


def test_http_error_detail_handles_validation_errors_without_empty_messages() -> None:
    detail = _http_error_detail("http://requirements-worker:8090", ValueError("bad payload"), 1, 3)
    assert "invalid response" in str(detail)
    assert "bad payload" in str(detail)
