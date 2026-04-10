"""
Purpose: Shared HTTP client with retries for orchestrator-to-worker communication.
Input/Output: The orchestrator sends `WorkerRequest` payloads and receives validated `WorkerResponse` results.
Important invariants: Calls are retried conservatively, and worker failures stay explicit instead of being silently ignored.
How to debug: If a workflow stalls between stages, inspect the URL, timeout, and last worker error reported here.
"""

from __future__ import annotations

import asyncio

import httpx

from services.shared.agentic_lab.schemas import WorkerRequest, WorkerResponse


class WorkerCallError(RuntimeError):
    """Raised when a worker call fails even after retries."""


async def call_worker(service_url: str, payload: WorkerRequest, attempts: int = 3) -> WorkerResponse:
    """POST the worker request and retry transient failures with small backoff."""

    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            async with httpx.AsyncClient(timeout=180) as client:
                response = await client.post(f"{service_url.rstrip('/')}/run", json=payload.model_dump())
                response.raise_for_status()
                return WorkerResponse.model_validate(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
            if attempt == attempts:
                break
            await asyncio.sleep(attempt * 1.5)

    raise WorkerCallError(f"Worker at {service_url} failed after {attempts} attempts: {last_error}")
