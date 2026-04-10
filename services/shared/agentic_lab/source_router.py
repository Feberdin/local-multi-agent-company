"""
Purpose: Route coding-research questions to trusted official sources before any general web-search fallback.
Input/Output: Accepts a question plus optional hints, then returns the ranked trusted-source plan and fallback notes.
Important invariants: Structured official APIs and registries win over HTML docs.
Unknown domains stay blocked unless an operator deliberately relaxes policy.
How to debug: If routing feels off, inspect the inferred ecosystem/type and the category/access ranking tables below.
"""

from __future__ import annotations

import re

from services.shared.agentic_lab.schemas import (
    PreferredAccess,
    ResearchEcosystem,
    ResearchQuestionType,
    SourceRoutingDecision,
    SourceRoutingRequest,
    TrustedSource,
    TrustedSourceCategory,
    TrustedSourceProfile,
)
from services.shared.agentic_lab.search_providers import SearchProviderService
from services.shared.agentic_lab.trusted_sources import TrustedSourceService

QUERY_TYPE_HINTS: dict[ResearchQuestionType, tuple[str, ...]] = {
    ResearchQuestionType.VERSION: ("version", "latest", "release", "tag", "stable", "upgrade"),
    ResearchQuestionType.DEPENDENCY: ("dependency", "dependencies", "package", "library", "module", "crate"),
    ResearchQuestionType.API: ("api", "endpoint", "method", "schema", "request", "response", "sdk"),
    ResearchQuestionType.INSTALL: ("install", "setup", "configure", "quickstart", "pip install", "npm install"),
    ResearchQuestionType.STANDARD: ("rfc", "standard", "spec", "specification", "manpage", "posix"),
    ResearchQuestionType.RELEASE: ("release", "changelog", "tag", "milestone"),
    ResearchQuestionType.SECURITY: ("auth", "oauth", "token", "security", "rate limit", "permissions"),
    ResearchQuestionType.DOCS: ("documentation", "docs", "syntax", "reference", "guide", "tutorial"),
}

ECOSYSTEM_HINTS: dict[ResearchEcosystem, tuple[str, ...]] = {
    ResearchEcosystem.PYTHON: ("python", "pypi", "pip", "django", "fastapi", "pytest"),
    ResearchEcosystem.NODE: ("node", "npm", "package.json", "javascript", "typescript", "js"),
    ResearchEcosystem.GITHUB: ("github", "repository", "pull request", "issue", "release", "tag"),
    ResearchEcosystem.WEB: ("browser", "dom", "fetch", "html", "css", "web api", "mdn"),
    ResearchEcosystem.DOCKER: ("docker", "container", "compose", "image"),
    ResearchEcosystem.KUBERNETES: ("kubernetes", "kubectl", "helm", "deployment", "pod"),
    ResearchEcosystem.RUST: ("rust", "cargo", "crate"),
    ResearchEcosystem.GO: ("golang", "go ", "pkg.go.dev", "module"),
    ResearchEcosystem.LINUX: ("linux", "syscall", "man", "kernel", "glibc"),
    ResearchEcosystem.INFRA: ("ansible", "terraform", "iac", "infrastructure"),
}

ECOSYSTEM_TAG_ALIASES: dict[ResearchEcosystem, set[str]] = {
    ResearchEcosystem.PYTHON: {"python", "pypi"},
    ResearchEcosystem.NODE: {"node", "npm", "javascript", "js", "typescript"},
    ResearchEcosystem.GITHUB: {"github", "git", "repo"},
    ResearchEcosystem.WEB: {"web", "mdn", "javascript", "js", "browser"},
    ResearchEcosystem.DOCKER: {"docker", "containers", "container"},
    ResearchEcosystem.KUBERNETES: {"kubernetes", "k8s"},
    ResearchEcosystem.RUST: {"rust", "cargo"},
    ResearchEcosystem.GO: {"go", "golang"},
    ResearchEcosystem.LINUX: {"linux", "unix", "kernel"},
    ResearchEcosystem.INFRA: {"infra", "devops", "ansible"},
    ResearchEcosystem.GENERAL: {"general"},
}

CATEGORY_PRIORITY: dict[ResearchQuestionType, dict[TrustedSourceCategory, int]] = {
    ResearchQuestionType.VERSION: {
        TrustedSourceCategory.PACKAGE_REGISTRY: 0,
        TrustedSourceCategory.OFFICIAL_REGISTRY: 1,
        TrustedSourceCategory.OFFICIAL_API: 2,
        TrustedSourceCategory.OFFICIAL_DOCS: 3,
        TrustedSourceCategory.REPO_HOSTING: 4,
        TrustedSourceCategory.STANDARDS_DOCS: 5,
    },
    ResearchQuestionType.DEPENDENCY: {
        TrustedSourceCategory.PACKAGE_REGISTRY: 0,
        TrustedSourceCategory.OFFICIAL_REGISTRY: 1,
        TrustedSourceCategory.OFFICIAL_API: 2,
        TrustedSourceCategory.OFFICIAL_DOCS: 3,
        TrustedSourceCategory.REPO_HOSTING: 4,
        TrustedSourceCategory.STANDARDS_DOCS: 5,
    },
    ResearchQuestionType.API: {
        TrustedSourceCategory.OFFICIAL_API: 0,
        TrustedSourceCategory.OFFICIAL_DOCS: 1,
        TrustedSourceCategory.OFFICIAL_REGISTRY: 2,
        TrustedSourceCategory.REPO_HOSTING: 3,
        TrustedSourceCategory.STANDARDS_DOCS: 4,
        TrustedSourceCategory.PACKAGE_REGISTRY: 5,
    },
    ResearchQuestionType.INSTALL: {
        TrustedSourceCategory.OFFICIAL_API: 0,
        TrustedSourceCategory.PACKAGE_REGISTRY: 1,
        TrustedSourceCategory.OFFICIAL_REGISTRY: 2,
        TrustedSourceCategory.OFFICIAL_DOCS: 3,
        TrustedSourceCategory.REPO_HOSTING: 4,
        TrustedSourceCategory.STANDARDS_DOCS: 5,
    },
    ResearchQuestionType.STANDARD: {
        TrustedSourceCategory.STANDARDS_DOCS: 0,
        TrustedSourceCategory.OFFICIAL_DOCS: 1,
        TrustedSourceCategory.OFFICIAL_API: 2,
        TrustedSourceCategory.REPO_HOSTING: 3,
        TrustedSourceCategory.OFFICIAL_REGISTRY: 4,
        TrustedSourceCategory.PACKAGE_REGISTRY: 5,
    },
    ResearchQuestionType.RELEASE: {
        TrustedSourceCategory.OFFICIAL_API: 0,
        TrustedSourceCategory.OFFICIAL_REGISTRY: 1,
        TrustedSourceCategory.REPO_HOSTING: 2,
        TrustedSourceCategory.OFFICIAL_DOCS: 3,
        TrustedSourceCategory.PACKAGE_REGISTRY: 4,
        TrustedSourceCategory.STANDARDS_DOCS: 5,
    },
    ResearchQuestionType.SECURITY: {
        TrustedSourceCategory.OFFICIAL_DOCS: 0,
        TrustedSourceCategory.OFFICIAL_API: 1,
        TrustedSourceCategory.OFFICIAL_REGISTRY: 2,
        TrustedSourceCategory.STANDARDS_DOCS: 3,
        TrustedSourceCategory.REPO_HOSTING: 4,
        TrustedSourceCategory.PACKAGE_REGISTRY: 5,
    },
    ResearchQuestionType.DOCS: {
        TrustedSourceCategory.OFFICIAL_DOCS: 0,
        TrustedSourceCategory.STANDARDS_DOCS: 1,
        TrustedSourceCategory.OFFICIAL_API: 2,
        TrustedSourceCategory.REPO_HOSTING: 3,
        TrustedSourceCategory.OFFICIAL_REGISTRY: 4,
        TrustedSourceCategory.PACKAGE_REGISTRY: 5,
    },
    ResearchQuestionType.GENERAL: {
        TrustedSourceCategory.OFFICIAL_DOCS: 0,
        TrustedSourceCategory.OFFICIAL_API: 1,
        TrustedSourceCategory.REPO_HOSTING: 2,
        TrustedSourceCategory.STANDARDS_DOCS: 3,
        TrustedSourceCategory.OFFICIAL_REGISTRY: 4,
        TrustedSourceCategory.PACKAGE_REGISTRY: 5,
    },
}

OFFICIAL_ONLY_TYPES = {
    ResearchQuestionType.VERSION,
    ResearchQuestionType.DEPENDENCY,
    ResearchQuestionType.API,
    ResearchQuestionType.INSTALL,
    ResearchQuestionType.SECURITY,
}

OFFICIAL_CATEGORIES = {
    TrustedSourceCategory.OFFICIAL_DOCS,
    TrustedSourceCategory.OFFICIAL_API,
    TrustedSourceCategory.OFFICIAL_REGISTRY,
    TrustedSourceCategory.PACKAGE_REGISTRY,
    TrustedSourceCategory.STANDARDS_DOCS,
}


class SourceRouter:
    """Trusted-source-first routing with transparent fallback notes."""

    def __init__(
        self,
        trusted_source_service: TrustedSourceService,
        search_provider_service: SearchProviderService,
    ) -> None:
        self.trusted_source_service = trusted_source_service
        self.search_provider_service = search_provider_service

    def route(self, request: SourceRoutingRequest) -> SourceRoutingDecision:
        """Choose the best sources for a research question and note whether fallback is allowed."""

        profile = self.trusted_source_service.load_active_profile()
        provider_settings = self.search_provider_service.load_settings()
        question_type = request.question_type or self.infer_question_type(request.query)
        ecosystem = request.ecosystem or self.infer_ecosystem(request.query)
        ranked_sources = self._rank_sources(profile, ecosystem, question_type) if profile.enabled else []
        general_web_allowed = (
            profile.enabled
            and profile.fallback_to_general_web_search
            and provider_settings.allow_general_web_search_fallback
        )
        provider_sequence = list(dict.fromkeys([
            provider_settings.primary_web_search_provider,
            provider_settings.fallback_web_search_provider,
        ]))

        notes = [
            "Structured official APIs and registries are preferred over HTML documentation.",
            "Unknown domains remain blocked unless the active profile allows general web fallback.",
        ]
        if provider_settings.require_trusted_sources_first:
            notes.append("Trusted-source-first mode is enabled before any general web-search provider is considered.")
        fallback_reason = None
        if not ranked_sources:
            fallback_reason = "No enabled trusted source matched the inferred ecosystem and question type."
            notes.append(fallback_reason)
        elif len(ranked_sources) < profile.minimum_source_count:
            fallback_reason = (
                f"Only {len(ranked_sources)} trusted source(s) matched, which is below the profile minimum of "
                f"{profile.minimum_source_count}."
            )
            notes.append(fallback_reason)

        if question_type is ResearchQuestionType.VERSION and profile.require_official_source_for_versions:
            notes.append("Version questions require official sources.")
        if question_type is ResearchQuestionType.DEPENDENCY and profile.require_official_source_for_dependencies:
            notes.append("Dependency questions require official package registries or official documentation.")
        if question_type is ResearchQuestionType.API and profile.require_official_source_for_api_reference:
            notes.append("API questions require official API or documentation sources.")
        if profile.require_whitelist_match:
            notes.append("Whitelist matching is enabled, so unknown domains are blocked even during fallback.")

        return SourceRoutingDecision(
            query=request.query,
            inferred_question_type=question_type,
            inferred_ecosystem=ecosystem,
            active_profile_id=profile.id,
            trusted_matches=ranked_sources,
            general_web_provider_sequence=provider_sequence,
            general_web_allowed=general_web_allowed,
            fallback_reason=fallback_reason,
            notes=notes,
        )

    def infer_question_type(self, query: str) -> ResearchQuestionType:
        lowered = query.lower()
        best_match = ResearchQuestionType.GENERAL
        best_score = 0
        for question_type, hints in QUERY_TYPE_HINTS.items():
            score = sum(1 for hint in hints if hint in lowered)
            if score > best_score:
                best_match = question_type
                best_score = score
        return best_match

    def infer_ecosystem(self, query: str) -> ResearchEcosystem:
        lowered = query.lower()
        best_match = ResearchEcosystem.GENERAL
        best_score = 0
        for ecosystem, hints in ECOSYSTEM_HINTS.items():
            score = sum(1 for hint in hints if hint in lowered)
            if score > best_score:
                best_match = ecosystem
                best_score = score
        return best_match

    def _rank_sources(
        self,
        profile: TrustedSourceProfile,
        ecosystem: ResearchEcosystem,
        question_type: ResearchQuestionType,
    ) -> list[TrustedSource]:
        category_table = CATEGORY_PRIORITY[question_type]
        candidates: list[tuple[tuple[int, int, int, str], TrustedSource]] = []

        for source in profile.sources:
            if not source.enabled:
                continue
            if not self._matches_ecosystem(source, ecosystem):
                continue
            if question_type in OFFICIAL_ONLY_TYPES and source.category not in OFFICIAL_CATEGORIES:
                continue

            sort_key = (
                category_table.get(source.category, 999),
                self._preferred_access_rank(source.preferred_access),
                source.priority,
                source.name.lower(),
            )
            candidates.append((sort_key, source))

        return [item for _, item in sorted(candidates, key=lambda entry: entry[0])]

    def _matches_ecosystem(self, source: TrustedSource, ecosystem: ResearchEcosystem) -> bool:
        if ecosystem is ResearchEcosystem.GENERAL:
            return True

        tags = {tag.lower() for tag in source.tags}
        aliases = ECOSYSTEM_TAG_ALIASES[ecosystem]
        if tags & aliases:
            return True

        host = source.domain.lower()
        if ecosystem is ResearchEcosystem.PYTHON:
            return bool(re.search(r"(python|pypi)", host))
        if ecosystem is ResearchEcosystem.NODE:
            return bool(re.search(r"(npm|nodejs)", host))
        if ecosystem is ResearchEcosystem.GITHUB:
            return "github" in host
        if ecosystem is ResearchEcosystem.WEB:
            return "mozilla" in host or "developer.mozilla.org" in host
        if ecosystem is ResearchEcosystem.DOCKER:
            return "docker" in host
        if ecosystem is ResearchEcosystem.KUBERNETES:
            return "kubernetes" in host
        if ecosystem is ResearchEcosystem.RUST:
            return "rust" in host or "crates.io" in host
        if ecosystem is ResearchEcosystem.GO:
            return "go.dev" in host or "pkg.go.dev" in host
        if ecosystem is ResearchEcosystem.LINUX:
            return "man7.org" in host or "rfc-editor.org" in host
        if ecosystem is ResearchEcosystem.INFRA:
            return "ansible" in host or "docker" in host or "kubernetes" in host
        return False

    @staticmethod
    def _preferred_access_rank(access: PreferredAccess) -> int:
        if access is PreferredAccess.API:
            return 0
        if access is PreferredAccess.MIXED:
            return 1
        return 2
