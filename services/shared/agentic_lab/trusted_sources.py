"""
Purpose: Persistent trusted-source profiles for coding-focused research and controlled web lookups.
Input/Output: Loads seed profiles, validates operator edits, stores runtime changes, and offers source-level helpers.
Important invariants: Unknown domains stay blocked by default, profiles remain auditable JSON, and source access stays explicit.
How to debug: If a source is unexpectedly rejected, inspect the normalized domain/base URL and the active profile JSON first.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
from slugify import slugify

from services.shared.agentic_lab.config import Settings
from services.shared.agentic_lab.schemas import (
    PreferredAccess,
    SourceAuthType,
    SourceTestResult,
    TrustedSource,
    TrustedSourceImportPayload,
    TrustedSourceProfile,
    TrustedSourceRegistry,
    TrustedSourceType,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_TRUSTED_SOURCES_SEED_PATH = PROJECT_ROOT / "config/trusted_sources.coding_profile.json"
PRIVATE_HOST_PATTERN = re.compile(r"(^localhost$)|(^127\.)|(^::1$)", re.IGNORECASE)
PACKAGE_NAME_PATTERN = re.compile(r"(?:package|library|crate|module)\s+([a-z0-9_.@/-]+)", re.IGNORECASE)
REPOSITORY_PATTERN = re.compile(r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\b")


class TrustedSourceError(ValueError):
    """Raised when an operator-provided trusted source or profile is invalid."""


class TrustedSourceService:
    """Manage trusted source profiles with JSON persistence and conservative validation."""

    def __init__(
        self,
        settings: Settings,
        *,
        store_path: Path | None = None,
        seed_path: Path | None = None,
    ) -> None:
        self.settings = settings
        self.store_path = store_path or settings.data_dir / "trusted_sources.json"
        self.seed_path = seed_path or DEFAULT_TRUSTED_SOURCES_SEED_PATH

    def load_registry(self) -> TrustedSourceRegistry:
        """Load runtime data or seed the registry from the checked-in configuration."""

        if self.store_path.exists():
            return self._normalize_registry(TrustedSourceRegistry.model_validate_json(self.store_path.read_text("utf-8")))

        registry = self._load_seed_registry()
        self._write_registry(registry)
        return registry

    def load_active_profile(self) -> TrustedSourceProfile:
        """Return the active profile so workers can stay strict by default."""

        registry = self.load_registry()
        for profile in registry.profiles:
            if profile.id == registry.active_profile_id:
                return profile
        raise TrustedSourceError(
            f"Active trusted-source profile `{registry.active_profile_id}` is missing. "
            "Import a valid profile or reset the runtime JSON."
        )

    def save_profile(self, profile: TrustedSourceProfile) -> TrustedSourceProfile:
        """Create or update a profile while preserving the active-profile pointer."""

        registry = self.load_registry()
        normalized_profile = self._normalize_profile(profile)
        existing_index = next((index for index, item in enumerate(registry.profiles) if item.id == normalized_profile.id), None)
        if existing_index is None:
            registry.profiles.append(normalized_profile)
        else:
            registry.profiles[existing_index] = normalized_profile
        if not registry.active_profile_id:
            registry.active_profile_id = normalized_profile.id
        normalized_registry = self._normalize_registry(registry)
        self._write_registry(normalized_registry)
        return self.load_active_profile() if normalized_profile.id == normalized_registry.active_profile_id else normalized_profile

    def save_registry(self, registry: TrustedSourceRegistry) -> TrustedSourceRegistry:
        """Persist a full registry document after validation."""

        normalized = self._normalize_registry(registry)
        self._write_registry(normalized)
        return normalized

    def set_active_profile(self, profile_id: str) -> TrustedSourceRegistry:
        """Switch the active profile used by the research worker and dry-run logic."""

        registry = self.load_registry()
        if not any(profile.id == profile_id for profile in registry.profiles):
            raise TrustedSourceError(f"Trusted-source profile `{profile_id}` does not exist.")
        registry.active_profile_id = profile_id
        normalized_registry = self._normalize_registry(registry)
        self._write_registry(normalized_registry)
        return normalized_registry

    def upsert_source(self, source: TrustedSource, profile_id: str | None = None) -> TrustedSourceProfile:
        """Add or update one trusted source inside a profile."""

        registry = self.load_registry()
        target_profile_id = profile_id or registry.active_profile_id
        profile = self._require_profile(registry, target_profile_id)
        normalized_source = self._normalize_source(source, previous_source=self._find_source(profile, source.id))

        existing_index = next((index for index, item in enumerate(profile.sources) if item.id == normalized_source.id), None)
        if existing_index is None:
            profile.sources.append(normalized_source)
        else:
            profile.sources[existing_index] = normalized_source

        updated_profile = self._normalize_profile(profile)
        self._replace_profile(registry, updated_profile)
        self._write_registry(self._normalize_registry(registry))
        return updated_profile

    def delete_source(self, source_id: str, profile_id: str | None = None) -> TrustedSourceProfile:
        """Delete a source from a profile. Seed profiles can be trimmed without code changes."""

        registry = self.load_registry()
        target_profile_id = profile_id or registry.active_profile_id
        profile = self._require_profile(registry, target_profile_id)
        remaining = [source for source in profile.sources if source.id != source_id]
        if len(remaining) == len(profile.sources):
            raise TrustedSourceError(f"Trusted source `{source_id}` was not found in profile `{target_profile_id}`.")
        profile.sources = remaining
        updated_profile = self._normalize_profile(profile)
        self._replace_profile(registry, updated_profile)
        self._write_registry(self._normalize_registry(registry))
        return updated_profile

    def import_payload(self, payload: TrustedSourceImportPayload) -> TrustedSourceRegistry:
        """Import either a whole registry or a single profile JSON document."""

        try:
            raw = json.loads(payload.payload_json)
        except json.JSONDecodeError as exc:
            raise TrustedSourceError(f"Trusted-source JSON is invalid: {exc}") from exc

        if isinstance(raw, dict) and "profiles" in raw:
            registry = self._normalize_registry(TrustedSourceRegistry.model_validate(raw))
            self._write_registry(registry)
            return registry

        profile = self._normalize_profile(TrustedSourceProfile.model_validate(raw))
        registry = self.load_registry()
        self._replace_profile(registry, profile)
        if not registry.active_profile_id:
            registry.active_profile_id = profile.id
        normalized_registry = self._normalize_registry(registry)
        self._write_registry(normalized_registry)
        return normalized_registry

    def export_registry_json(self) -> str:
        """Return a stable JSON export that operators can back up or edit offline."""

        registry = self.load_registry()
        return json.dumps(registry.model_dump(mode="json"), indent=2, ensure_ascii=True)

    def list_enabled_sources(self, profile_id: str | None = None) -> list[TrustedSource]:
        """Return enabled trusted sources only, already normalized and priority-sorted."""

        profile = self.load_active_profile() if profile_id is None else self._require_profile(self.load_registry(), profile_id)
        return [source for source in profile.sources if source.enabled]

    def domain_allowed(self, domain: str, profile_id: str | None = None) -> bool:
        """Check whether a domain matches an enabled source in the selected profile."""

        normalized_domain = self.normalize_domain(domain)
        for source in self.list_enabled_sources(profile_id):
            if normalized_domain == source.domain or normalized_domain.endswith(f".{source.domain}"):
                return True
        return False

    async def test_source(
        self,
        source_id: str,
        query: str,
        profile_id: str | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> SourceTestResult:
        """Prepare a source-specific request preview and try a lightweight connectivity check."""

        profile = self.load_active_profile() if profile_id is None else self._require_profile(self.load_registry(), profile_id)
        source = self._find_source(profile, source_id)
        if source is None:
            raise TrustedSourceError(f"Trusted source `{source_id}` was not found in profile `{profile.id}`.")

        request_preview = self._build_request_preview(source, query)
        try:
            status_code = await self._connectivity_probe(source, client=client)
        except httpx.HTTPError as exc:
            return SourceTestResult(
                source_id=source.id,
                status="failed",
                message=(
                    f"Connectivity check for `{source.name}` failed: {exc}. "
                    "Check DNS, HTTPS reachability, or whether the source requires auth."
                ),
                request_preview=request_preview,
                connectivity_url=source.base_url,
                matched=bool(request_preview.get("recommended_endpoint")),
            )

        return SourceTestResult(
            source_id=source.id,
            status="ok",
            message=(
                f"Source `{source.name}` is reachable. "
                "The preview below shows how the worker will approach this source for the given query."
            ),
            request_preview=request_preview,
            connectivity_url=source.base_url,
            http_status=status_code,
            matched=bool(request_preview.get("recommended_endpoint")),
        )

    def _load_seed_registry(self) -> TrustedSourceRegistry:
        if not self.seed_path.exists():
            raise TrustedSourceError(
                f"Trusted-source seed file `{self.seed_path}` is missing. "
                "Restore the config file before starting the stack."
            )
        return self._normalize_registry(TrustedSourceRegistry.model_validate_json(self.seed_path.read_text("utf-8")))

    def _write_registry(self, registry: TrustedSourceRegistry) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self.store_path.write_text(json.dumps(registry.model_dump(mode="json"), indent=2, ensure_ascii=True), encoding="utf-8")

    def _normalize_registry(self, registry: TrustedSourceRegistry) -> TrustedSourceRegistry:
        normalized_profiles = [self._normalize_profile(profile) for profile in registry.profiles]
        if not normalized_profiles:
            raise TrustedSourceError("At least one trusted-source profile is required.")

        active_profile_id = registry.active_profile_id or normalized_profiles[0].id
        if active_profile_id not in {profile.id for profile in normalized_profiles}:
            raise TrustedSourceError(
                f"Active profile `{active_profile_id}` does not exist in the trusted-source registry."
            )
        return TrustedSourceRegistry(active_profile_id=active_profile_id, profiles=normalized_profiles)

    def _normalize_profile(self, profile: TrustedSourceProfile) -> TrustedSourceProfile:
        profile_id = slugify(profile.id or profile.name, separator="_") or "trusted_source_profile"
        normalized_sources: list[TrustedSource] = []
        seen_domains: set[tuple[str, str]] = set()
        for source in profile.sources:
            normalized = self._normalize_source(source, previous_source=None)
            signature = (normalized.domain, normalized.base_url)
            if signature in seen_domains:
                raise TrustedSourceError(
                    f"Duplicate trusted source detected for domain `{normalized.domain}` and base URL `{normalized.base_url}`."
                )
            seen_domains.add(signature)
            normalized_sources.append(normalized)

        return TrustedSourceProfile(
            id=profile_id,
            name=profile.name.strip(),
            description=profile.description.strip(),
            enabled=profile.enabled,
            fallback_to_general_web_search=profile.fallback_to_general_web_search,
            require_whitelist_match=profile.require_whitelist_match,
            minimum_source_count=profile.minimum_source_count,
            require_official_source_for_versions=profile.require_official_source_for_versions,
            require_official_source_for_dependencies=profile.require_official_source_for_dependencies,
            require_official_source_for_api_reference=profile.require_official_source_for_api_reference,
            sources=sorted(normalized_sources, key=lambda item: (item.priority, item.name.lower())),
        )

    def _normalize_source(self, source: TrustedSource, previous_source: TrustedSource | None) -> TrustedSource:
        domain = self.normalize_domain(source.domain or source.base_url)
        base_url = self.normalize_base_url(source.base_url)
        if self.normalize_domain(base_url) != domain:
            raise TrustedSourceError(
                f"Trusted source `{source.name}` uses domain `{domain}`, but its base URL resolves to `{self.normalize_domain(base_url)}`. "
                "Keep domain and base URL aligned to avoid ambiguous routing."
            )

        if any("*" in token for token in [domain, *source.allowed_paths, *source.deny_paths]):
            raise TrustedSourceError(
                f"Trusted source `{source.name}` contains a wildcard. "
                "Wildcards are blocked by default because they widen the trust boundary too much."
            )

        if PRIVATE_HOST_PATTERN.search(domain):
            raise TrustedSourceError(
                f"Trusted source `{source.name}` points to `{domain}`. "
                "Loopback hosts are not allowed as trusted coding sources."
            )

        normalized_allowed_paths = [self._normalize_path(value) for value in source.allowed_paths]
        normalized_deny_paths = [self._normalize_path(value) for value in source.deny_paths]
        created_at = previous_source.created_at if previous_source is not None else source.created_at
        source_id = slugify(source.id or source.name or domain, separator="_") or slugify(domain, separator="_")

        return TrustedSource(
            id=source_id,
            name=source.name.strip(),
            domain=domain,
            category=source.category,
            enabled=source.enabled,
            priority=source.priority,
            source_type=source.source_type,
            preferred_access=source.preferred_access,
            base_url=base_url,
            api_description=(source.api_description or "").strip() or None,
            auth_type=source.auth_type,
            auth_env_var=(source.auth_env_var or "").strip() or None,
            rate_limit_notes=(source.rate_limit_notes or "").strip() or None,
            usage_instructions=(source.usage_instructions or "").strip() or None,
            allowed_paths=normalized_allowed_paths,
            deny_paths=normalized_deny_paths,
            tags=sorted({tag.strip().lower() for tag in source.tags if tag.strip()}),
            created_at=created_at,
            updated_at=datetime.now(UTC),
        )

    def _replace_profile(self, registry: TrustedSourceRegistry, profile: TrustedSourceProfile) -> None:
        existing_index = next((index for index, item in enumerate(registry.profiles) if item.id == profile.id), None)
        if existing_index is None:
            registry.profiles.append(profile)
        else:
            registry.profiles[existing_index] = profile

    def _require_profile(self, registry: TrustedSourceRegistry, profile_id: str) -> TrustedSourceProfile:
        for profile in registry.profiles:
            if profile.id == profile_id:
                return profile
        raise TrustedSourceError(f"Trusted-source profile `{profile_id}` was not found.")

    def _find_source(self, profile: TrustedSourceProfile, source_id: str) -> TrustedSource | None:
        for source in profile.sources:
            if source.id == source_id:
                return source
        return None

    def _build_request_preview(self, source: TrustedSource, query: str) -> dict[str, str]:
        """Explain to the operator how the worker will access a source without leaking secrets."""

        query_lower = query.lower()
        preview: dict[str, str] = {
            "access_strategy": source.preferred_access.value,
            "base_url": source.base_url,
            "usage_instructions": source.usage_instructions or "Use this source as a trusted reference.",
        }

        repository_match = REPOSITORY_PATTERN.search(query)
        package_match = PACKAGE_NAME_PATTERN.search(query)
        guessed_subject = package_match.group(1) if package_match else ""

        if source.domain == "api.github.com":
            repo_name = repository_match.group(1) if repository_match else "OWNER/REPO"
            endpoint = f"{source.base_url}/repos/{repo_name}"
            if any(token in query_lower for token in ("release", "tag", "version")):
                endpoint = f"{source.base_url}/repos/{repo_name}/releases"
            preview["recommended_endpoint"] = endpoint
            preview["headers"] = "Accept: application/vnd.github+json; X-GitHub-Api-Version: 2022-11-28"
            preview["auth"] = (
                f"Optional token from ENV `{source.auth_env_var}`"
                if source.auth_env_var
                else "No auth configured"
            )
            return preview

        if source.domain == "pypi.org":
            package_name = guessed_subject or "example-package"
            preview["recommended_endpoint"] = f"https://pypi.org/pypi/{package_name}/json"
            preview["headers"] = "Accept: application/json"
            return preview

        if source.domain == "registry.npmjs.org":
            package_name = guessed_subject or "example-package"
            preview["recommended_endpoint"] = f"https://registry.npmjs.org/{package_name}"
            preview["headers"] = "Accept: application/json"
            return preview

        if source.source_type in {TrustedSourceType.API, TrustedSourceType.REGISTRY}:
            preview["recommended_endpoint"] = source.base_url
            preview["headers"] = "Use JSON or structured API responses when available."
            return preview

        if source.preferred_access is PreferredAccess.HTML:
            preview["recommended_endpoint"] = source.base_url
            preview["headers"] = "Parse canonical content only; ignore navigation, ads, and footer content."
            return preview

        preview["recommended_endpoint"] = source.base_url
        return preview

    async def _connectivity_probe(self, source: TrustedSource, client: httpx.AsyncClient | None = None) -> int:
        request_headers = {"User-Agent": "feberdin-agent-team/0.1"}
        auth_value = None
        if source.auth_env_var:
            auth_value = os.getenv(source.auth_env_var, "").strip()

        if auth_value:
            if source.auth_type in {SourceAuthType.BEARER, SourceAuthType.TOKEN}:
                request_headers["Authorization"] = f"Bearer {auth_value}"
            elif source.auth_type in {SourceAuthType.HEADER, SourceAuthType.HEADER_TOKEN}:
                request_headers["Authorization"] = f"token {auth_value}"

        owned_client = client is None
        async_client = client or httpx.AsyncClient(timeout=8.0, follow_redirects=True)
        try:
            response = await async_client.get(source.base_url, headers=request_headers)
            response.raise_for_status()
            return response.status_code
        finally:
            if owned_client:
                await async_client.aclose()

    @staticmethod
    def normalize_domain(value: str) -> str:
        candidate = value.strip().lower()
        if not candidate:
            raise TrustedSourceError("A trusted source requires a domain or base URL.")

        parsed = urlparse(candidate if "://" in candidate else f"https://{candidate}")
        hostname = (parsed.hostname or "").strip().lower()
        if not hostname:
            raise TrustedSourceError(f"`{value}` is not a valid domain or URL.")
        if "*" in hostname:
            raise TrustedSourceError("Wildcards are not allowed in trusted source domains.")
        return hostname

    @staticmethod
    def normalize_base_url(value: str) -> str:
        candidate = value.strip()
        if not candidate:
            raise TrustedSourceError("A trusted source requires a base URL.")
        parsed = urlparse(candidate if "://" in candidate else f"https://{candidate}")
        if parsed.scheme not in {"http", "https"}:
            raise TrustedSourceError(f"Trusted source URL `{value}` must use http or https.")
        if not parsed.netloc:
            raise TrustedSourceError(f"Trusted source URL `{value}` is missing a hostname.")
        cleaned_path = parsed.path.rstrip("/")
        normalized = parsed._replace(path=cleaned_path, params="", query="", fragment="")
        return normalized.geturl()

    @staticmethod
    def _normalize_path(value: str) -> str:
        normalized = value.strip()
        if not normalized:
            return normalized
        if not normalized.startswith("/"):
            raise TrustedSourceError(
                f"Path rule `{value}` must start with `/` so matching stays explicit and predictable."
            )
        return normalized.rstrip("/") or "/"
