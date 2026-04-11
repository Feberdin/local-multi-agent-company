"""
Purpose: Shared HTTP client with retries for orchestrator-to-worker communication.
Input/Output: The orchestrator sends `WorkerRequest` payloads and receives validated `WorkerResponse` results.
Important invariants: Calls are retried conservatively, and worker failures stay explicit instead of being silently ignored.
How to debug: If a workflow stalls between stages, inspect the URL, timeout, and last worker error reported here.
"""

from __future__ import annotations

import asyncio

import httpx

from services.shared.agentic_lab.config import get_settings
from services.shared.agentic_lab.schemas import WorkerRequest, WorkerResponse


class WorkerCallError(RuntimeError):
    """Raised when a worker call fails even after retries."""


def _http_error_detail(service_url: str, exc: Exception, attempt: int, total_attempts: int) -> RuntimeError:
    """Translate worker HTTP and validation failures into clearer operator-facing text."""

    if isinstance(exc, httpx.HTTPStatusError):
        response_text = exc.response.text.strip()
        response_preview = f" Response: {response_text[:400]}" if response_text else ""
        return RuntimeError(
            f"Worker at {service_url} returned HTTP {exc.response.status_code} on attempt {attempt}/{total_attempts}: "
            f"{exc}.{response_preview}"
        )

    if isinstance(exc, httpx.HTTPError):
        return RuntimeError(
            f"Worker at {service_url} failed on attempt {attempt}/{total_attempts}: {exc}"
        )

    error_text = str(exc).strip() or exc.__class__.__name__
    return RuntimeError(
        f"Worker at {service_url} returned an invalid response on attempt {attempt}/{total_attempts}: {error_text}"
    )


async def call_worker(service_url: str, payload: WorkerRequest, attempts: int | None = None) -> WorkerResponse:
    """POST the worker request and retry transient failures with small backoff."""

    settings = get_settings()
    total_attempts = attempts or settings.worker_retry_attempts
    last_error: Exception | None = None
    for attempt in range(1, total_attempts + 1):
        try:
            async with httpx.AsyncClient(timeout=settings.worker_http_timeout()) as client:
                response = await client.post(f"{service_url.rstrip('/')}/run", json=payload.model_dump())
                response.raise_for_status()
                return WorkerResponse.model_validate(response.json())
        except httpx.TimeoutException as exc:
            last_error = RuntimeError(
                f"Worker at {service_url} timed out on attempt {attempt}/{total_attempts}. "
                f"Configured worker transport timeouts: {settings.worker_timeout_summary()}. "
                "This usually means the worker is still busy with a long model call or repository operation. "
                f"Original error: {exc}"
            )
            if attempt == total_attempts:
                break
            await asyncio.sleep(attempt * 1.5)
        except (httpx.HTTPError, ValueError) as exc:
            last_error = _http_error_detail(service_url, exc, attempt, total_attempts)
            if attempt == total_attempts:
                break
            await asyncio.sleep(attempt * 1.5)

    raise WorkerCallError(f"Worker at {service_url} failed after {total_attempts} attempts: {last_error}")
