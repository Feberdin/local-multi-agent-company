"""
Purpose: Verify the dedicated SearXNG client that powers provider searches and health checks.
Input/Output: Exercises request building, JSON parsing, error translation, and HTML-vs-JSON diagnostics with mocked HTTP responses.
Important invariants: SearXNG searches must always use GET /search with query parameters and `format=json` for machine-readable responses.
How to debug: If a test fails, inspect the mocked request URL first, then compare the error text with the expected operator guidance.
"""

from __future__ import annotations

import httpx
import pytest

from services.shared.agentic_lab.searxng_client import SearXNGClient, SearXNGClientError


@pytest.mark.asyncio
async def test_searxng_client_search_uses_official_json_request_shape() -> None:
    client = SearXNGClient(base_url="http://192.168.57.10:8087", timeout_seconds=20.0)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url) == (
            "http://192.168.57.10:8087/search"
            "?q=python+packaging+official+docs&format=json&categories=general&language=auto&safesearch=0"
        )
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Python Packaging",
                        "url": "https://packaging.python.org/",
                        "content": "Official packaging guide",
                        "engine": "searxng",
                        "category": "general",
                    }
                ]
            },
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as mock_client:
        response = await client.search("python packaging official docs", client=mock_client, limit=8)

    assert response.request_params == {
        "q": "python packaging official docs",
        "format": "json",
        "categories": "general",
        "language": "auto",
        "safesearch": 0,
    }
    assert response.results[0].url == "https://packaging.python.org/"


@pytest.mark.asyncio
async def test_searxng_client_health_check_distinguishes_html_and_json() -> None:
    client = SearXNGClient(base_url="http://192.168.57.10:8087", timeout_seconds=20.0)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("format") == "json":
            return httpx.Response(
                403,
                text="Forbidden",
                headers={"content-type": "text/html; charset=utf-8"},
                request=request,
            )
        return httpx.Response(
            200,
            text="<html><body>ok</body></html>",
            headers={"content-type": "text/html; charset=utf-8"},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as mock_client:
        report = await client.health_check(client=mock_client)

    assert report.html_check.ok is True
    assert report.json_check.ok is False
    assert report.api_ready is False
    assert report.json_check.http_status == 403
    assert "search.formats" in report.json_check.message


@pytest.mark.asyncio
async def test_searxng_client_rejects_invalid_json() -> None:
    client = SearXNGClient(base_url="http://192.168.57.10:8087", timeout_seconds=20.0)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="<html>still html</html>",
            headers={"content-type": "text/html; charset=utf-8"},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as mock_client:
        with pytest.raises(SearXNGClientError, match="kein gueltiges JSON"):
            await client.search("python packaging official docs", client=mock_client)


@pytest.mark.asyncio
async def test_searxng_client_reports_timeouts_clearly() -> None:
    client = SearXNGClient(base_url="http://192.168.57.10:8087", timeout_seconds=20.0)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow backend", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as mock_client:
        with pytest.raises(SearXNGClientError, match="zu langsam geantwortet"):
            await client.search("python packaging official docs", client=mock_client)


def test_searxng_client_rejects_empty_queries_early() -> None:
    client = SearXNGClient(base_url="http://192.168.57.10:8087", timeout_seconds=20.0)

    with pytest.raises(SearXNGClientError, match="Anfrage ist leer"):
        client.build_request_preview("   ")
