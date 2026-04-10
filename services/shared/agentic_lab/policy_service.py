"""
Purpose: Enforce project-local repository access policy and explicit repository modification rules.
Input/Output: Loads and stores the allowed GitHub repository list from persistent runtime storage.
Important invariants: Repository access is deny-by-default, names are normalized to owner/name form,
and policy is auditable outside the git repo.
How to debug: If a task is blocked unexpectedly, inspect the policy file and the normalized repository name resolved here.
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

from services.shared.agentic_lab.config import Settings
from services.shared.agentic_lab.schemas import RepositoryAccessSettings


class RepositoryPolicyError(ValueError):
    """Raised when a repository name or policy state is invalid for the requested action."""


class RepositoryPolicyService:
    """Persist and validate the deny-by-default repository allowlist."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.policy_path = settings.data_dir / "repository-access-policy.json"

    def load(self) -> RepositoryAccessSettings:
        """Return the persisted repository policy or an empty deny-by-default policy."""

        if not self.policy_path.exists():
            return RepositoryAccessSettings()
        raw = json.loads(self.policy_path.read_text(encoding="utf-8"))
        return RepositoryAccessSettings.model_validate(raw)

    def save(self, repositories: list[str]) -> RepositoryAccessSettings:
        """Normalize and persist the explicit allowlist."""

        normalized = sorted(
            {
                self.normalize_repository_name(repository)
                for repository in repositories
                if repository.strip()
            }
        )
        settings = RepositoryAccessSettings(allowed_repositories=normalized)
        self.policy_path.parent.mkdir(parents=True, exist_ok=True)
        self.policy_path.write_text(
            json.dumps(settings.model_dump(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return settings

    def repository_is_allowed(self, repository: str) -> bool:
        """Return True only when the repository is explicitly present in the allowlist."""

        normalized = self.normalize_repository_name(repository)
        allowed = {self.normalize_repository_name(item) for item in self.load().allowed_repositories}
        return normalized in allowed

    def assert_repository_allowed(self, repository: str) -> None:
        """Raise a helpful error when a task targets a repository outside the explicit allowlist."""

        normalized = self.normalize_repository_name(repository)
        if not self.repository_is_allowed(normalized):
            raise RepositoryPolicyError(
                f"Repository `{normalized}` ist nicht in der expliziten Allowlist. "
                "Bitte es zuerst im Webinterface unter Einstellungen freigeben."
            )

    def normalize_repository_name(self, repository: str) -> str:
        """Accept owner/name or GitHub URLs and return a normalized owner/name string."""

        value = repository.strip()
        if not value:
            raise RepositoryPolicyError("Repository name darf nicht leer sein.")

        if value.startswith(("https://", "http://")):
            parsed = urlparse(value)
            path = parsed.path.strip("/")
            if path.endswith(".git"):
                path = path[:-4]
            parts = [part for part in path.split("/") if part]
        else:
            cleaned = value[:-4] if value.endswith(".git") else value
            parts = [part for part in cleaned.strip("/").split("/") if part]

        if len(parts) != 2:
            raise RepositoryPolicyError(
                f"Repository `{repository}` ist ungültig. Erwartet wird `owner/name` oder eine GitHub-URL."
            )

        owner, name = parts
        return f"{owner}/{name}".lower()
