"""
Purpose: Verify provider routing, trusted-domain filtering, and auth handling for general web-search fallback.
Input/Output: Exercises the search-provider service with mocked HTTP transports and temporary runtime settings.
Important invariants: Trusted sources stay first, unknown domains are filtered, and Brave auth remains server-side.
How to debug: If a provider test fails, inspect the normalized provider settings and mocked response payloads.
"""

from __future__ import annotations

import httpx
import pytest

from services.shared.agentic_lab.config import get_settings
from services.shared.agentic_lab.schemas import SearchProviderTestRequest, SearchProviderType
from services.shared.agentic_lab.search_providers import SearchProviderError, SearchProviderService
from services.shared.agentic_lab.trusted_sources import TrustedSourceService


def _configure_enabled_providers() -> tuple[SearchProviderService, TrustedSourceService]:
    settings = get_settings()
    trusted_source_service = TrustedSourceService(settings)
    provider_service = SearchProviderService(settings)
    provider_settings = provider_service.load_settings()
    providers = []
    for provider in provider_settings.providers:
        if provider.provider_type is SearchProviderType.SEARXNG:
            providers.append(provider.model_copy(update={"enabled": True}))
        elif provider.provider_type is SearchProviderType.BRAVE:
            providers.append(provider.model_copy(update={"enabled": True}))
        else:
            providers.append(provider)
    provider_service.save_settings(provider_settings.model_copy(update={"providers": providers}))
    return provider_service, trusted_source_service


@pytest.mark.asyncio
async def test_searxng_results_are_filtered_to_trusted_domains() -> None:
    provider_service, trusted_source_service = _configure_enabled_providers()
    trusted_profile = trusted_source_service.load_active_profile()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "192.168.57.10":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "Python Docs",
                            "url": "https://docs.python.org/3/library/json.html",
                            "content": "Official docs",
                            "engine": "searxng",
                            "category": "general",
                        },
                        {
                            "title": "Unknown Blog",
                            "url": "https://random-blog.example.org/python-json",
                            "content": "Third-party article",
                            "engine": "searxng",
                            "category": "general",
                        },
                    ]
                },
                request=request,
            )
        raise AssertionError(f"Unexpected request host: {request.url.host}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider, results, notes = await provider_service.search(
            "python json docs",
            trusted_source_service,
            trusted_profile,
            client=client,
        )

    assert provider is not None
    assert provider.provider_type is SearchProviderType.SEARXNG
    assert [item.url for item in results] == ["https://docs.python.org/3/library/json.html"]
    assert any("trusted fallback" in note.lower() for note in notes)


@pytest.mark.asyncio
async def test_brave_fallback_is_used_when_searxng_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    provider_service, trusted_source_service = _configure_enabled_providers()
    trusted_profile = trusted_source_service.load_active_profile()
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "test-brave-key")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "192.168.57.10":
            return httpx.Response(503, json={"error": "searxng unavailable"}, request=request)
        if request.url.host == "api.search.brave.com":
            assert request.headers["X-Subscription-Token"] == "test-brave-key"
            return httpx.Response(
                200,
                json={
                    "web": {
                        "results": [
                            {
                                "title": "GitHub REST Docs",
                                "url": "https://docs.github.com/en/rest",
                                "description": "Official GitHub REST API docs.",
                                "type": "search_result",
                            }
                        ]
                    }
                },
                request=request,
            )
        raise AssertionError(f"Unexpected request host: {request.url.host}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider, results, notes = await provider_service.search(
            "github rest api docs",
            trusted_source_service,
            trusted_profile,
            client=client,
        )

    assert provider is not None
    assert provider.provider_type is SearchProviderType.BRAVE
    assert results[0].url == "https://docs.github.com/en/rest"
    assert any("failed" in note.lower() for note in notes)


@pytest.mark.asyncio
async def test_brave_requires_server_side_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    provider_service, trusted_source_service = _configure_enabled_providers()
    trusted_profile = trusted_source_service.load_active_profile()
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)

    provider_settings = provider_service.load_settings()
    brave_provider = next(provider for provider in provider_settings.providers if provider.provider_type is SearchProviderType.BRAVE)

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}, request=request))) as client:
        with pytest.raises(SearchProviderError, match="requires a server-side API key"):
            await provider_service.test_provider(
                SearchProviderTestRequest(provider_id=brave_provider.id, query="official docs"),
                trusted_source_service,
                trusted_profile,
                client=client,
            )
