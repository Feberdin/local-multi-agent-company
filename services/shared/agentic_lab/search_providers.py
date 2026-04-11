"""
Purpose: Persist and execute controlled general-web search providers for research fallback.
Input/Output: Loads provider settings, validates safe provider URLs, performs searches, and normalizes results.
Important invariants: Trusted sources always stay first, provider secrets remain server-side, and no arbitrary URL fetching is exposed.
How to debug: If provider tests fail, inspect the stored base URL, host allowlist, auth ENV name, and returned HTTP status first.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from slugify import slugify

from services.shared.agentic_lab.config import Settings
from services.shared.agentic_lab.schemas import (
    SearchProvider,
    SearchProviderHealthStatus,
    SearchProviderProbeResult,
    SearchProviderSettings,
    SearchProviderTestRequest,
    SearchProviderTestResult,
    SearchProviderType,
    SearchResultItem,
    SourceAuthType,
    TrustedSourceProfile,
)
from services.shared.agentic_lab.searxng_client import SearXNGClient, SearXNGClientError
from services.shared.agentic_lab.trusted_sources import TrustedSourceService

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SEARCH_PROVIDER_SEED_PATH = PROJECT_ROOT / "config/web_search.providers.json"
BLOCKED_PROVIDER_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
BLOCKED_GENERAL_WEB_DOMAINS = {
    "medium.com",
    "stackoverflow.com",
    "reddit.com",
    "stackshare.io",
    "g2.com",
    "alternativeto.net",
}
LOGGER = logging.getLogger(__name__)


class SearchProviderError(ValueError):
    """Raised when a search provider configuration is invalid or unusable."""


class SearchProviderService:
    """Manage provider persistence and safe fallback web-search execution."""

    def __init__(
        self,
        settings: Settings,
        *,
        store_path: Path | None = None,
        seed_path: Path | None = None,
    ) -> None:
        self.settings = settings
        self.store_path = store_path or settings.data_dir / "web_search_providers.json"
        self.seed_path = seed_path or DEFAULT_SEARCH_PROVIDER_SEED_PATH

    def load_settings(self) -> SearchProviderSettings:
        """Load runtime provider settings or seed them from the checked-in config."""

        if self.store_path.exists():
            return self._normalize_settings(SearchProviderSettings.model_validate_json(self.store_path.read_text("utf-8")))

        settings = self._load_seed_settings()
        self._write_settings(settings)
        return settings

    def save_settings(self, payload: SearchProviderSettings) -> SearchProviderSettings:
        """Persist the full provider settings document after normalization."""

        normalized = self._normalize_settings(payload)
        self._write_settings(normalized)
        return normalized

    def upsert_provider(self, provider: SearchProvider) -> SearchProviderSettings:
        """Create or update one provider without forcing callers to rewrite the whole document."""

        settings = self.load_settings()
        normalized_provider = self._normalize_provider(provider, settings.provider_host_allowlist)
        existing_index = next((index for index, item in enumerate(settings.providers) if item.id == normalized_provider.id), None)
        if existing_index is None:
            settings.providers.append(normalized_provider)
        else:
            settings.providers[existing_index] = normalized_provider
        normalized_settings = self._normalize_settings(settings)
        self._write_settings(normalized_settings)
        return normalized_settings

    def delete_provider(self, provider_id: str) -> SearchProviderSettings:
        """Delete a provider entry while keeping the remaining routing settings intact."""

        settings = self.load_settings()
        remaining = [provider for provider in settings.providers if provider.id != provider_id]
        if len(remaining) == len(settings.providers):
            raise SearchProviderError(f"Search provider `{provider_id}` was not found.")
        settings.providers = remaining
        normalized_settings = self._normalize_settings(settings)
        self._write_settings(normalized_settings)
        return normalized_settings

    async def test_provider(
        self,
        request: SearchProviderTestRequest,
        trusted_source_service: TrustedSourceService,
        trusted_profile: TrustedSourceProfile,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> SearchProviderTestResult:
        """Run a live provider query with domain filtering so operators can verify fallback behavior."""

        settings = self.load_settings()
        provider = self._require_provider(settings, request.provider_id)
        request_preview = self._provider_request_preview(provider, request.query)
        checked_url = self._provider_endpoint(provider)
        if provider.provider_type is SearchProviderType.SEARXNG:
            searxng_client = self._searxng_client(provider)
            try:
                searxng_response = await searxng_client.search(
                    request.query,
                    client=client,
                    limit=provider.max_results,
                )
            except SearXNGClientError as exc:
                self._record_provider_health(provider.id, SearchProviderHealthStatus.FAILED)
                raise SearchProviderError(self._provider_error_message(provider, request.query, searxng_client.endpoint, exc)) from exc
            results = searxng_response.results
            request_preview = searxng_response.request_params
            checked_url = searxng_response.endpoint
        else:
            results = await self._search_provider(provider, request.query, client=client)
        filtered_results = self._filter_results(results, trusted_source_service, trusted_profile)
        status = SearchProviderHealthStatus.HEALTHY if filtered_results else SearchProviderHealthStatus.DEGRADED
        message = (
            f"Provider `{provider.name}` returned {len(filtered_results)} usable result(s) after trusted-domain filtering."
            if filtered_results
            else (
                f"Provider `{provider.name}` responded, but no result survived the trusted-domain and blocklist filters. "
                "This is expected when the trusted profile enforces whitelist-only fallback."
            )
        )
        self._record_provider_health(provider.id, status)
        return SearchProviderTestResult(
            provider_id=provider.id,
            status=status,
            message=message,
            results=filtered_results,
            checked_url=checked_url,
            base_url=provider.base_url,
            request_preview=request_preview,
            api_ready=True,
        )

    async def health_check(
        self,
        provider_id: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> SearchProviderTestResult:
        """Perform a minimal provider health probe without exposing secrets in the response."""

        settings = self.load_settings()
        provider = self._require_provider(settings, provider_id)
        if provider.provider_type is SearchProviderType.SEARXNG:
            searxng_client = self._searxng_client(provider)
            report = await searxng_client.health_check(client=client)
            status = SearchProviderHealthStatus.HEALTHY if report.api_ready else (
                SearchProviderHealthStatus.DEGRADED if report.html_check.ok else SearchProviderHealthStatus.FAILED
            )
            self._record_provider_health(provider.id, status)
            return SearchProviderTestResult(
                provider_id=provider.id,
                status=status,
                message=report.message,
                checked_url=report.json_check.url,
                base_url=provider.base_url,
                request_preview=searxng_client.build_request_preview("test"),
                health_checks=[
                    SearchProviderProbeResult(**report.html_check.__dict__),
                    SearchProviderProbeResult(**report.json_check.__dict__),
                ],
                api_ready=report.api_ready,
                technical_cause=(report.json_check.message if not report.api_ready else None),
            )
        try:
            health_request = SearchProviderTestRequest(provider_id=provider.id, query="official docs health check")
            test_result = await self.test_provider(
                health_request,
                trusted_source_service=TrustedSourceService(self.settings),
                trusted_profile=TrustedSourceService(self.settings).load_active_profile(),
                client=client,
            )
        except (httpx.HTTPError, SearchProviderError) as exc:
            self._record_provider_health(provider.id, SearchProviderHealthStatus.FAILED)
            return SearchProviderTestResult(
                provider_id=provider.id,
                status=SearchProviderHealthStatus.FAILED,
                message=(
                    f"Health check for `{provider.name}` failed: {exc}. "
                    "Check the provider URL, network reachability, or server-side API-key configuration."
                ),
                checked_url=self._provider_endpoint(provider),
                base_url=provider.base_url,
                api_ready=False,
                technical_cause=str(exc),
            )
        self._record_provider_health(provider.id, test_result.status)
        return SearchProviderTestResult(
            provider_id=provider.id,
            status=test_result.status,
            message=f"Health check for `{provider.name}` completed. {test_result.message}",
            results=test_result.results,
            checked_url=test_result.checked_url,
            base_url=provider.base_url,
            request_preview=test_result.request_preview,
            health_checks=test_result.health_checks,
            api_ready=test_result.api_ready,
            technical_cause=test_result.technical_cause,
        )

    async def search(
        self,
        query: str,
        trusted_source_service: TrustedSourceService,
        trusted_profile: TrustedSourceProfile,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> tuple[SearchProvider | None, list[SearchResultItem], list[str]]:
        """Execute provider routing: SearXNG first, then Brave, then a transparent stop."""

        settings = self.load_settings()
        notes: list[str] = []
        provider_sequence = self._provider_sequence(settings)
        if not settings.allow_general_web_search_fallback:
            return None, [], ["General web-search fallback is disabled in the provider settings."]

        for provider_type in provider_sequence:
            provider = self._provider_by_type(settings, provider_type)
            if provider is None or not provider.enabled:
                notes.append(f"Provider `{provider_type.value}` is not enabled.")
                continue
            try:
                raw_results = await self._search_provider(provider, query, client=client)
            except SearchProviderError as exc:
                notes.append(str(exc))
                continue

            filtered_results = self._filter_results(raw_results, trusted_source_service, trusted_profile)
            if filtered_results:
                notes.append(f"Provider `{provider.name}` returned trusted fallback results.")
                return provider, filtered_results, notes
            notes.append(
                f"Provider `{provider.name}` returned no usable results after trusted-domain filtering."
            )

        return None, [], notes

    def _load_seed_settings(self) -> SearchProviderSettings:
        if not self.seed_path.exists():
            raise SearchProviderError(
                f"Search-provider seed file `{self.seed_path}` is missing. "
                "Restore the checked-in configuration before booting the stack."
            )
        return self._normalize_settings(SearchProviderSettings.model_validate_json(self.seed_path.read_text("utf-8")))

    def _write_settings(self, settings: SearchProviderSettings) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self.store_path.write_text(json.dumps(settings.model_dump(mode="json"), indent=2, ensure_ascii=True), encoding="utf-8")

    def _normalize_settings(self, settings: SearchProviderSettings) -> SearchProviderSettings:
        normalized_providers = [
            self._normalize_provider(provider, settings.provider_host_allowlist) for provider in settings.providers
        ]
        seen_ids: set[str] = set()
        for provider in normalized_providers:
            if provider.id in seen_ids:
                raise SearchProviderError(f"Duplicate search provider id `{provider.id}` is not allowed.")
            seen_ids.add(provider.id)

        return SearchProviderSettings(
            primary_web_search_provider=settings.primary_web_search_provider,
            fallback_web_search_provider=settings.fallback_web_search_provider,
            require_trusted_sources_first=settings.require_trusted_sources_first,
            allow_general_web_search_fallback=settings.allow_general_web_search_fallback,
            provider_host_allowlist=sorted({host.strip().lower() for host in settings.provider_host_allowlist if host.strip()}),
            providers=sorted(normalized_providers, key=lambda item: (item.priority, item.name.lower())),
        )

    def _normalize_provider(self, provider: SearchProvider, host_allowlist: list[str]) -> SearchProvider:
        provider_id = slugify(provider.id or provider.name or provider.provider_type.value, separator="_") or provider.provider_type.value
        base_url = self._normalize_base_url(provider.base_url, host_allowlist)
        search_path = provider.search_path.strip() or "/search"
        if not search_path.startswith("/"):
            raise SearchProviderError(
                f"Search path `{provider.search_path}` for provider `{provider.name}` must start with `/`."
            )
        method = provider.method.strip().upper()
        if method not in {"GET", "POST"}:
            raise SearchProviderError(
                f"Provider `{provider.name}` uses unsupported method `{provider.method}`. Only GET and POST are allowed."
            )
        if provider.provider_type is SearchProviderType.SEARXNG:
            if search_path != "/search":
                raise SearchProviderError(
                    f"SearXNG provider `{provider.name}` must use the official `/search` endpoint."
                )
            method = "GET"

        created_at = provider.created_at
        return SearchProvider(
            id=provider_id,
            name=provider.name.strip(),
            provider_type=provider.provider_type,
            enabled=provider.enabled,
            priority=provider.priority,
            base_url=base_url,
            search_path=search_path,
            method=method,
            auth_type=provider.auth_type,
            auth_env_var=(provider.auth_env_var or "").strip() or None,
            timeout_seconds=provider.timeout_seconds,
            max_results=provider.max_results,
            default_language=(
                provider.default_language.strip()
                or ("auto" if provider.provider_type is SearchProviderType.SEARXNG else "en")
            ),
            default_categories=(
                [value.strip() for value in provider.default_categories if value.strip()]
                or (["general"] if provider.provider_type is SearchProviderType.SEARXNG else [])
            ),
            safe_search=provider.safe_search,
            health_status=provider.health_status,
            last_checked_at=provider.last_checked_at,
            created_at=created_at,
            updated_at=datetime.now(UTC),
        )

    def _normalize_base_url(self, value: str, host_allowlist: list[str]) -> str:
        candidate = value.strip()
        if not candidate:
            raise SearchProviderError("Search providers require a base URL.")
        parsed = urlparse(candidate if "://" in candidate else f"http://{candidate}")
        if parsed.scheme not in {"http", "https"}:
            raise SearchProviderError(f"Provider URL `{value}` must use http or https.")
        host = (parsed.hostname or "").lower()
        if not host:
            raise SearchProviderError(f"Provider URL `{value}` is missing a hostname.")
        if host in BLOCKED_PROVIDER_HOSTS:
            raise SearchProviderError(
                f"Provider host `{host}` is blocked. Use an explicit LAN or DNS hostname instead of loopback."
            )
        if host_allowlist and not any(host == allowed or host.endswith(f".{allowed}") for allowed in host_allowlist):
            raise SearchProviderError(
                f"Provider host `{host}` is not part of the configured provider host allowlist."
            )
        normalized = parsed._replace(path=parsed.path.rstrip("/"), params="", query="", fragment="")
        return normalized.geturl()

    def _provider_sequence(self, settings: SearchProviderSettings) -> list[SearchProviderType]:
        sequence = [settings.primary_web_search_provider]
        if settings.fallback_web_search_provider not in sequence:
            sequence.append(settings.fallback_web_search_provider)
        return sequence

    def _provider_by_type(
        self,
        settings: SearchProviderSettings,
        provider_type: SearchProviderType,
    ) -> SearchProvider | None:
        matching = [provider for provider in settings.providers if provider.provider_type is provider_type]
        if not matching:
            return None
        return sorted(matching, key=lambda item: (item.priority, item.name.lower()))[0]

    def _require_provider(self, settings: SearchProviderSettings, provider_id: str) -> SearchProvider:
        for provider in settings.providers:
            if provider.id == provider_id:
                return provider
        raise SearchProviderError(f"Search provider `{provider_id}` was not found.")

    async def _search_provider(
        self,
        provider: SearchProvider,
        query: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> list[SearchResultItem]:
        if provider.provider_type is SearchProviderType.SEARXNG:
            return await self._search_searxng(provider, query, client=client)
        if provider.provider_type is SearchProviderType.BRAVE:
            return await self._search_brave(provider, query, client=client)
        raise SearchProviderError(f"Unsupported search provider type `{provider.provider_type.value}`.")

    async def _search_searxng(
        self,
        provider: SearchProvider,
        query: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> list[SearchResultItem]:
        searxng_client = self._searxng_client(provider)
        try:
            provider_response = await searxng_client.search(query, client=client, limit=provider.max_results)
        except SearXNGClientError as exc:
            raise SearchProviderError(self._provider_error_message(provider, query, searxng_client.endpoint, exc)) from exc
        return provider_response.results

    async def _search_brave(
        self,
        provider: SearchProvider,
        query: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> list[SearchResultItem]:
        headers = self._provider_headers(provider)
        api_key = self._provider_secret(provider)
        if not api_key:
            raise SearchProviderError(
                f"Brave provider `{provider.name}` requires a server-side API key in ENV `{provider.auth_env_var}`. "
                "Brave is optional in this stack and should only be enabled if you explicitly want that external fallback."
            )
        headers["X-Subscription-Token"] = api_key
        params: dict[str, str | int] = {
            "q": query,
            "count": provider.max_results,
            "search_lang": provider.default_language,
        }
        owned_client = client is None
        async_client = client or httpx.AsyncClient(timeout=provider.timeout_seconds, follow_redirects=True)
        endpoint = self._provider_endpoint(provider)
        try:
            response = await async_client.get(endpoint, params=params, headers=headers)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise SearchProviderError(self._provider_error_message(provider, query, endpoint, exc)) from exc
        except ValueError as exc:
            raise SearchProviderError(
                f"Brave provider `{provider.name}` at `{endpoint}` returned a non-JSON response for query `{query}`."
            ) from exc
        finally:
            if owned_client:
                await async_client.aclose()

        items: list[SearchResultItem] = []
        web_results = payload.get("web", {}).get("results", [])
        for raw_item in web_results[: provider.max_results]:
            items.append(
                SearchResultItem(
                    title=(raw_item.get("title") or "").strip() or raw_item.get("url", "Untitled result"),
                    url=raw_item.get("url", ""),
                    snippet=(raw_item.get("description") or "").strip(),
                    engine="brave",
                    category=raw_item.get("type"),
                    result_type="general_web_search",
                )
            )
        return items

    def _provider_endpoint(self, provider: SearchProvider) -> str:
        return urljoin(f"{provider.base_url.rstrip('/')}/", provider.search_path.lstrip("/"))

    def _provider_headers(self, provider: SearchProvider) -> dict[str, str]:
        headers = {"User-Agent": "feberdin-agent-team/0.1"}
        secret = self._provider_secret(provider)
        if secret:
            if provider.auth_type is SourceAuthType.BEARER:
                headers["Authorization"] = f"Bearer {secret}"
            elif provider.auth_type in {SourceAuthType.HEADER, SourceAuthType.HEADER_TOKEN, SourceAuthType.TOKEN}:
                headers["Authorization"] = f"token {secret}"
        return headers

    def _provider_secret(self, provider: SearchProvider) -> str:
        if provider.auth_env_var is None:
            return ""
        direct_value = os.getenv(provider.auth_env_var, "").strip()
        if direct_value:
            return direct_value

        secret_file = os.getenv(f"{provider.auth_env_var}_FILE", "").strip()
        if secret_file:
            path = Path(secret_file)
            try:
                if path.exists():
                    return path.read_text(encoding="utf-8").rstrip("\r\n")
            except PermissionError:
                LOGGER.warning(
                    "Search provider secret file '%s' is not readable. "
                    "The provider will continue without this optional secret until you fix the file permissions.",
                    path,
                )
                return ""
            except OSError as exc:
                LOGGER.warning(
                    "Search provider secret file '%s' could not be read (%s). Continuing without that secret.",
                    path,
                    exc,
                )
                return ""
        return ""

    def _provider_error_message(
        self,
        provider: SearchProvider,
        query: str,
        endpoint: str,
        exc: Exception,
    ) -> str:
        """Translate low-level HTTP client failures into operator-facing diagnostics."""

        timeout_hint = f"timeout={provider.timeout_seconds}s"
        if isinstance(exc, SearXNGClientError):
            preview_suffix = f" Backend said: {exc.response_preview}" if exc.response_preview else ""
            return f"{exc}{preview_suffix}"
        if isinstance(exc, httpx.ReadTimeout):
            return (
                f"Provider `{provider.name}` timed out while reading results for query `{query}` at `{endpoint}` "
                f"({timeout_hint}). The service is reachable, but it answered too slowly. "
                "Increase the provider timeout for slow self-hosted search backends."
            )
        if isinstance(exc, httpx.ConnectTimeout):
            return (
                f"Provider `{provider.name}` could not be reached in time at `{endpoint}` ({timeout_hint}). "
                "Check the host, port, reverse proxy, and Docker network reachability."
            )
        if isinstance(exc, httpx.ConnectError):
            return (
                f"Provider `{provider.name}` is not reachable at `{endpoint}`. "
                "Check whether the host and port are correct and whether the remote service is running."
            )
        if isinstance(exc, httpx.HTTPStatusError):
            status_code = exc.response.status_code
            if provider.provider_type is SearchProviderType.SEARXNG and status_code == 404:
                return (
                    f"SearXNG provider `{provider.name}` returned HTTP 404 for `{endpoint}` while testing query `{query}`. "
                    "The host answered, but this path is not a valid SearXNG JSON search endpoint. "
                    "Check `base_url` and `search_path` (often `/search`). "
                    "Important: this Feberdin stack does not start a SearXNG container by default, "
                    "so the provider must point to an existing external SearXNG instance."
                )
            return (
                f"Provider `{provider.name}` returned HTTP {status_code} for `{endpoint}` while testing query `{query}`. "
                f"Backend said: {exc.response.text[:300]}"
            )
        return f"Provider `{provider.name}` failed for query `{query}` at `{endpoint}`: {exc}"

    def _record_provider_health(self, provider_id: str, status: SearchProviderHealthStatus) -> None:
        """Persist the last known provider health so the dashboard reflects reality between checks."""

        settings = self.load_settings()
        providers: list[SearchProvider] = []
        updated = False
        now = datetime.now(UTC)
        for provider in settings.providers:
            if provider.id == provider_id:
                providers.append(
                    provider.model_copy(
                        update={
                            "health_status": status,
                            "last_checked_at": now,
                            "updated_at": now,
                        }
                    )
                )
                updated = True
            else:
                providers.append(provider)
        if updated:
            self._write_settings(settings.model_copy(update={"providers": providers}))

    def _provider_request_preview(self, provider: SearchProvider, query: str) -> dict[str, str | int]:
        if provider.provider_type is SearchProviderType.SEARXNG:
            return self._searxng_client(provider).build_request_preview(query)
        if provider.provider_type is SearchProviderType.BRAVE:
            return {
                "q": query.strip(),
                "count": provider.max_results,
                "search_lang": provider.default_language,
            }
        return {"q": query.strip()}

    def _searxng_client(self, provider: SearchProvider) -> SearXNGClient:
        return SearXNGClient(
            base_url=provider.base_url,
            search_path=provider.search_path,
            timeout_seconds=provider.timeout_seconds,
            language=provider.default_language,
            categories=provider.default_categories,
            safe_search=provider.safe_search,
        )

    def _filter_results(
        self,
        results: list[SearchResultItem],
        trusted_source_service: TrustedSourceService,
        trusted_profile: TrustedSourceProfile,
    ) -> list[SearchResultItem]:
        filtered: list[SearchResultItem] = []
        for item in results:
            host = (urlparse(item.url).hostname or "").lower()
            if not host:
                continue
            if host in BLOCKED_GENERAL_WEB_DOMAINS or any(host.endswith(f".{blocked}") for blocked in BLOCKED_GENERAL_WEB_DOMAINS):
                continue
            if trusted_profile.require_whitelist_match and not trusted_source_service.domain_allowed(host, trusted_profile.id):
                continue
            filtered.append(item)
        return filtered
