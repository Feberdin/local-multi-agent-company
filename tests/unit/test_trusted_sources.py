"""
Purpose: Validate trusted-source persistence, normalization, and import/export behavior.
Input/Output: Exercises the JSON-backed trusted-source service with isolated runtime paths.
Important invariants: Unknown domains stay blocked, wildcards are rejected, and exports round-trip cleanly.
How to debug: If a test fails, inspect the runtime JSON under the temporary data directory for normalization issues.
"""

from __future__ import annotations

import json

import httpx
import pytest

from services.shared.agentic_lab.config import get_settings
from services.shared.agentic_lab.schemas import TrustedSource, TrustedSourceCategory, TrustedSourceType
from services.shared.agentic_lab.trusted_sources import TrustedSourceError, TrustedSourceImportPayload, TrustedSourceService


def test_seed_registry_loads_and_allows_known_domains() -> None:
    service = TrustedSourceService(get_settings())

    registry = service.load_registry()

    assert registry.active_profile_id == "trusted_coding"
    assert service.domain_allowed("docs.github.com") is True
    assert service.domain_allowed("unknown.example.org") is False


def test_export_and_import_round_trip_preserves_profile() -> None:
    service = TrustedSourceService(get_settings())

    exported = service.export_registry_json()
    registry = service.import_payload(TrustedSourceImportPayload(payload_json=exported))

    assert registry.active_profile_id == "trusted_coding"
    exported_payload = json.loads(exported)
    imported_payload = json.loads(service.export_registry_json())
    assert exported_payload["active_profile_id"] == imported_payload["active_profile_id"]


def test_wildcard_domain_is_rejected() -> None:
    service = TrustedSourceService(get_settings())

    with pytest.raises(TrustedSourceError, match="Wildcards are not allowed"):
        service.upsert_source(
            TrustedSource(
                id="wildcard",
                name="Wildcard Source",
                domain="*.example.org",
                category=TrustedSourceCategory.OFFICIAL_DOCS,
                enabled=True,
                priority=100,
                source_type=TrustedSourceType.DOCS,
                preferred_access="html",
                base_url="https://example.org",
            )
        )


@pytest.mark.asyncio
async def test_source_test_preview_for_github_api_uses_repo_endpoint() -> None:
    service = TrustedSourceService(get_settings())
    registry = service.load_registry()
    active_profile = next(profile for profile in registry.profiles if profile.id == registry.active_profile_id)
    github_api = next(source for source in active_profile.sources if source.domain == "api.github.com")

    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}, request=request))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await service.test_source(
            github_api.id,
            "Show the latest release for Feberdin/local-multi-agent-company",
            client=client,
        )

    assert "/repos/Feberdin/local-multi-agent-company/releases" in result.request_preview["recommended_endpoint"]
    assert result.http_status == 200
