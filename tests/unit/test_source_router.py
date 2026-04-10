"""
Purpose: Verify trusted-source routing decisions and priority rules for coding research.
Input/Output: Routes representative questions through the router without performing network access.
Important invariants: Official structured sources outrank HTML docs, and fallback is only considered transparently.
How to debug: If expectations shift, inspect the ranking tables and ecosystem/question-type inference first.
"""

from __future__ import annotations

from services.shared.agentic_lab.config import get_settings
from services.shared.agentic_lab.schemas import ResearchEcosystem, ResearchQuestionType, SourceRoutingRequest
from services.shared.agentic_lab.search_providers import SearchProviderService
from services.shared.agentic_lab.source_router import SourceRouter
from services.shared.agentic_lab.trusted_sources import TrustedSourceService


def _router() -> SourceRouter:
    settings = get_settings()
    return SourceRouter(TrustedSourceService(settings), SearchProviderService(settings))


def test_python_version_query_prefers_pypi_registry() -> None:
    decision = _router().route(SourceRoutingRequest(query="What is the latest FastAPI package version on PyPI?"))

    assert decision.inferred_ecosystem is ResearchEcosystem.PYTHON
    assert decision.inferred_question_type is ResearchQuestionType.VERSION
    assert decision.trusted_matches[0].domain == "pypi.org"


def test_github_release_query_prefers_github_api() -> None:
    decision = _router().route(
        SourceRoutingRequest(query="Show the latest release tag for Feberdin/local-multi-agent-company on GitHub")
    )

    assert decision.inferred_ecosystem is ResearchEcosystem.GITHUB
    assert decision.inferred_question_type in {ResearchQuestionType.RELEASE, ResearchQuestionType.VERSION}
    assert decision.trusted_matches[0].domain == "api.github.com"


def test_npm_dependency_query_prefers_registry_over_html_docs() -> None:
    decision = _router().route(
        SourceRoutingRequest(query="Which npm package version and dist-tags should I use for express?")
    )

    assert decision.inferred_ecosystem is ResearchEcosystem.NODE
    assert decision.trusted_matches[0].domain == "registry.npmjs.org"
    assert decision.trusted_matches[0].preferred_access.value == "api"


def test_standard_query_prefers_rfc_editor_and_man7() -> None:
    decision = _router().route(SourceRoutingRequest(query="Which RFC defines HTTP status codes?"))

    assert decision.inferred_question_type is ResearchQuestionType.STANDARD
    assert decision.trusted_matches[0].domain == "www.rfc-editor.org"
