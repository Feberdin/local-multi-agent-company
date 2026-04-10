"""
Purpose: Verify the repository allowlist policy and its deny-by-default behavior.
Input/Output: Tests store allowed repositories, normalize names, and assert that blocked repositories raise a clear error.
Important invariants: Repository access is explicit, normalized, and never silently broadened.
How to debug: If these tests fail, inspect `policy_service.py` and the policy JSON written under the runtime data directory.
"""

from __future__ import annotations

import pytest

from services.shared.agentic_lab.config import get_settings
from services.shared.agentic_lab.policy_service import RepositoryPolicyError, RepositoryPolicyService


def test_policy_service_normalizes_and_saves_repositories() -> None:
    service = RepositoryPolicyService(get_settings())

    saved = service.save(
        [
            "Feberdin/example-repo",
            "https://github.com/Feberdin/local-multi-agent-company.git",
        ]
    )

    assert saved.allowed_repositories == [
        "feberdin/example-repo",
        "feberdin/local-multi-agent-company",
    ]


def test_policy_service_blocks_repositories_outside_allowlist() -> None:
    service = RepositoryPolicyService(get_settings())
    service.save(["Feberdin/example-repo"])

    with pytest.raises(RepositoryPolicyError):
        service.assert_repository_allowed("Feberdin/not-allowed")
