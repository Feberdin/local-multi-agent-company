"""
Purpose: Provide a clear, typed client for the official SearXNG JSON Search API plus targeted health checks.
Input/Output: Builds GET /search requests, validates queries, returns normalized search results, and reports HTML-vs-JSON readiness.
Important invariants: We always use query parameters, never scrape HTML, and JSON mode always requires `format=json`.
How to debug: Start with the JSON health check; if HTML works but JSON fails, inspect `search.formats` in SearXNG `settings.yml`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urljoin

import httpx

from services.shared.agentic_lab.schemas import SearchResultItem

SEARXNG_REQUIRED_SEARCH_PATH = "/search"
SEARXNG_DEFAULT_LANGUAGE = "auto"
SEARXNG_DEFAULT_CATEGORIES = ("general",)
SEARXNG_DEFAULT_SAFESEARCH = 0
SEARXNG_HEALTH_QUERY = "test"
SEARXNG_SETTINGS_HINT = (
    "SearXNG erreichbar, aber JSON-API nicht aktiv. "
    "Bitte `json` unter `search.formats` in der SearXNG settings.yml aktivieren:\n"
    "search:\n"
    "  formats:\n"
    "    - html\n"
    "    - json"
)


class SearXNGClientError(RuntimeError):
    """Raised when a SearXNG request cannot be completed or interpreted safely."""

    def __init__(
        self,
        message: str,
        *,
        url: str,
        http_status: int | None = None,
        response_preview: str | None = None,
        technical_cause: str | None = None,
    ) -> None:
        super().__init__(message)
        self.url = url
        self.http_status = http_status
        self.response_preview = response_preview
        self.technical_cause = technical_cause


@dataclass(frozen=True)
class SearXNGProbeResult:
    """One concrete reachability probe so operators can see where integration stops working."""

    name: str
    ok: bool
    message: str
    url: str
    http_status: int | None = None
    response_format: str | None = None
    response_preview: str | None = None


@dataclass(frozen=True)
class SearXNGHealthReport:
    """Summarize both HTML/base reachability and JSON API readiness."""

    base_url: str
    html_check: SearXNGProbeResult
    json_check: SearXNGProbeResult

    @property
    def api_ready(self) -> bool:
        return self.json_check.ok

    @property
    def message(self) -> str:
        if self.html_check.ok and self.json_check.ok:
            return (
                "SearXNG ist erreichbar und die JSON-API antwortet korrekt. "
                "Die Worker koennen strukturierte Suchergebnisse verwenden."
            )
        if self.html_check.ok and not self.json_check.ok:
            return (
                "SearXNG ist erreichbar, aber die JSON-API ist nicht produktiv nutzbar. "
                f"{self.json_check.message}"
            )
        return (
            "SearXNG ist nicht voll erreichbar. "
            f"Basis-Check: {self.html_check.message}"
        )


@dataclass(frozen=True)
class SearXNGSearchResponse:
    """Normalized API response used by the provider layer and tests."""

    endpoint: str
    request_params: dict[str, str | int]
    results: list[SearchResultItem]
    raw_payload: dict[str, Any]


class SearXNGClient:
    """Small, explicit wrapper around the official SearXNG Search API."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        search_path: str = SEARXNG_REQUIRED_SEARCH_PATH,
        language: str = SEARXNG_DEFAULT_LANGUAGE,
        categories: list[str] | tuple[str, ...] | None = None,
        safe_search: int = SEARXNG_DEFAULT_SAFESEARCH,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.search_path = (search_path or SEARXNG_REQUIRED_SEARCH_PATH).strip() or SEARXNG_REQUIRED_SEARCH_PATH
        self.language = language.strip() or SEARXNG_DEFAULT_LANGUAGE
        self.categories = tuple(categories or SEARXNG_DEFAULT_CATEGORIES)
        self.safe_search = safe_search
        self.timeout = httpx.Timeout(
            connect=min(timeout_seconds, 10.0),
            read=timeout_seconds,
            write=min(timeout_seconds, 10.0),
            pool=min(timeout_seconds, 10.0),
        )
        if self.search_path != SEARXNG_REQUIRED_SEARCH_PATH:
            raise SearXNGClientError(
                "Die offizielle SearXNG-API verwendet den Endpunkt `/search`. "
                "Bitte trage die Instanz als Base-URL ein und lasse den Search Path auf `/search`.",
                url=self.endpoint,
            )

    @property
    def endpoint(self) -> str:
        return urljoin(f"{self.base_url}/", self.search_path.lstrip("/"))

    async def search(
        self,
        query: str,
        *,
        client: httpx.AsyncClient | None = None,
        time_range: str | None = None,
        categories: list[str] | tuple[str, ...] | None = None,
        language: str | None = None,
        safe_search: int | None = None,
        limit: int = 10,
    ) -> SearXNGSearchResponse:
        """Run one JSON API search and return normalized internal result items."""

        params = self._build_search_params(
            query,
            categories=categories,
            language=language,
            safe_search=safe_search,
            time_range=time_range,
        )
        response = await self._perform_request(params, client=client)
        payload = self._parse_json_payload(response)

        items: list[SearchResultItem] = []
        raw_results = payload.get("results", [])
        if not isinstance(raw_results, list):
            raise SearXNGClientError(
                "SearXNG hat JSON geliefert, aber das Feld `results` hat ein unerwartetes Format. "
                "Bitte pruefe Proxy-Rewrites oder eine inkompatible Instanz-Konfiguration.",
                url=str(response.request.url),
                http_status=response.status_code,
                response_preview=_response_preview(response.text),
            )

        for raw_item in raw_results[:limit]:
            if not isinstance(raw_item, dict):
                continue
            items.append(
                SearchResultItem(
                    title=(str(raw_item.get("title") or "").strip() or str(raw_item.get("url") or "Untitled result")),
                    url=str(raw_item.get("url") or ""),
                    snippet=str(raw_item.get("content") or "").strip(),
                    engine=str(raw_item.get("engine")) if raw_item.get("engine") is not None else None,
                    category=str(raw_item.get("category")) if raw_item.get("category") is not None else None,
                    result_type="general_web_search",
                )
            )

        return SearXNGSearchResponse(
            endpoint=str(response.request.url),
            request_params=params,
            results=items,
            raw_payload=payload,
        )

    async def health_check(self, *, client: httpx.AsyncClient | None = None) -> SearXNGHealthReport:
        """Probe HTML reachability and JSON API readiness separately."""

        html_check = await self._probe(expect_json=False, client=client)
        json_check = await self._probe(expect_json=True, client=client)
        return SearXNGHealthReport(base_url=self.base_url, html_check=html_check, json_check=json_check)

    def build_request_preview(
        self,
        query: str,
        *,
        time_range: str | None = None,
        categories: list[str] | tuple[str, ...] | None = None,
        language: str | None = None,
        safe_search: int | None = None,
    ) -> dict[str, str | int]:
        """Expose the exact request shape for logs, tests, and UI diagnostics."""

        return self._build_search_params(
            query,
            categories=categories,
            language=language,
            safe_search=safe_search,
            time_range=time_range,
        )

    def request_url(self, params: dict[str, str | int]) -> str:
        return f"{self.endpoint}?{urlencode(params, doseq=False)}"

    def _build_search_params(
        self,
        query: str,
        *,
        categories: list[str] | tuple[str, ...] | None = None,
        language: str | None = None,
        safe_search: int | None = None,
        time_range: str | None = None,
    ) -> dict[str, str | int]:
        clean_query = query.strip()
        if not clean_query:
            raise SearXNGClientError(
                "Die SearXNG-Anfrage ist leer. Bitte gib einen Suchbegriff ein, bevor du den Provider testest.",
                url=self.endpoint,
            )

        active_categories = [value.strip() for value in (categories or self.categories) if value.strip()]
        if not active_categories:
            active_categories = list(SEARXNG_DEFAULT_CATEGORIES)

        params: dict[str, str | int] = {
            "q": clean_query,
            "format": "json",
            "categories": ",".join(active_categories),
            "language": (language or self.language).strip() or SEARXNG_DEFAULT_LANGUAGE,
            "safesearch": self.safe_search if safe_search is None else safe_search,
        }
        if time_range:
            params["time_range"] = time_range
        return params

    async def _probe(
        self,
        *,
        expect_json: bool,
        client: httpx.AsyncClient | None = None,
    ) -> SearXNGProbeResult:
        params: dict[str, str | int] = {"q": SEARXNG_HEALTH_QUERY}
        if expect_json:
            params.update(
                {
                    "format": "json",
                    "categories": ",".join(SEARXNG_DEFAULT_CATEGORIES),
                    "language": SEARXNG_DEFAULT_LANGUAGE,
                    "safesearch": SEARXNG_DEFAULT_SAFESEARCH,
                }
            )

        try:
            response = await self._perform_request(params, client=client)
        except SearXNGClientError as exc:
            return SearXNGProbeResult(
                name="JSON API" if expect_json else "Basis-Erreichbarkeit",
                ok=False,
                message=str(exc),
                url=exc.url,
                http_status=exc.http_status,
                response_preview=exc.response_preview,
            )

        response_format = response.headers.get("content-type", "").split(";", 1)[0].strip() or None
        if expect_json:
            try:
                self._parse_json_payload(response)
            except SearXNGClientError as exc:
                return SearXNGProbeResult(
                    name="JSON API",
                    ok=False,
                    message=str(exc),
                    url=exc.url,
                    http_status=exc.http_status,
                    response_format=response_format,
                    response_preview=exc.response_preview,
                )
            return SearXNGProbeResult(
                name="JSON API",
                ok=True,
                message="SearXNG JSON-API antwortet mit gueltigem JSON.",
                url=str(response.request.url),
                http_status=response.status_code,
                response_format=response_format,
            )

        return SearXNGProbeResult(
            name="Basis-Erreichbarkeit",
            ok=True,
            message="SearXNG antwortet auf eine normale Suchanfrage.",
            url=str(response.request.url),
            http_status=response.status_code,
            response_format=response_format,
        )

    async def _perform_request(
        self,
        params: dict[str, str | int],
        *,
        client: httpx.AsyncClient | None = None,
    ) -> httpx.Response:
        owned_client = client is None
        async_client = client or httpx.AsyncClient(timeout=self.timeout, follow_redirects=True)
        try:
            response = await async_client.get(self.endpoint, params=params, headers={"User-Agent": "feberdin-agent-team/0.1"})
        except httpx.ReadTimeout as exc:
            raise SearXNGClientError(
                "SearXNG hat zu langsam geantwortet. Bitte Timeout erhoehen oder die Instanz-Leistung pruefen.",
                url=self.request_url(params),
                technical_cause=str(exc),
            ) from exc
        except httpx.ConnectTimeout as exc:
            raise SearXNGClientError(
                "SearXNG konnte nicht rechtzeitig erreicht werden. Bitte Host, Port und Reverse Proxy pruefen.",
                url=self.request_url(params),
                technical_cause=str(exc),
            ) from exc
        except httpx.ConnectError as exc:
            raise SearXNGClientError(
                "SearXNG ist unter dieser Base-URL nicht erreichbar. Bitte Host, Port und Netzwerkrouting pruefen.",
                url=self.request_url(params),
                technical_cause=str(exc),
            ) from exc
        finally:
            if owned_client:
                await async_client.aclose()

        if response.status_code >= 400:
            raise self._http_status_error(response)
        return response

    def _parse_json_payload(self, response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise SearXNGClientError(
                "SearXNG hat geantwortet, aber kein gueltiges JSON geliefert. "
                "Bitte pruefe, ob `format=json` serverseitig aktiviert ist und nicht durch einen Proxy ueberschrieben wird.",
                url=str(response.request.url),
                http_status=response.status_code,
                response_preview=_response_preview(response.text),
                technical_cause=str(exc),
            ) from exc
        if not isinstance(payload, dict):
            raise SearXNGClientError(
                "SearXNG hat JSON geliefert, aber nicht im erwarteten Objektformat. "
                "Bitte die Instanz- oder Proxy-Konfiguration pruefen.",
                url=str(response.request.url),
                http_status=response.status_code,
                response_preview=_response_preview(response.text),
            )
        return payload

    def _http_status_error(self, response: httpx.Response) -> SearXNGClientError:
        preview = _response_preview(response.text)
        request_url = str(response.request.url)
        status_code = response.status_code
        if status_code == 403 and "format=json" in request_url:
            return SearXNGClientError(
                f"{SEARXNG_SETTINGS_HINT} Alternativ kann auch eine serverseitige Zugriffsbeschraenkung auf API-Aufrufe aktiv sein.",
                url=request_url,
                http_status=status_code,
                response_preview=preview,
            )
        if status_code == 404:
            return SearXNGClientError(
                "SearXNG antwortete mit HTTP 404. Bitte pruefe, ob die Base-URL auf die Instanz zeigt "
                "und der Search Path `/search` ist. Dieser Feberdin-Stack startet standardmaessig "
                "keinen eigenen SearXNG-Container.",
                url=request_url,
                http_status=status_code,
                response_preview=preview,
            )
        return SearXNGClientError(
            f"SearXNG antwortete mit HTTP {status_code}. Bitte Instanz-Konfiguration, Reverse Proxy und Zugriffsrechte pruefen.",
            url=request_url,
            http_status=status_code,
            response_preview=preview,
        )


def _response_preview(text: str, *, limit: int = 300) -> str | None:
    compact = " ".join(text.split()).strip()
    if not compact:
        return None
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 3]}..."
