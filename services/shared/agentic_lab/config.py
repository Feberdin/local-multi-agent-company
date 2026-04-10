"""
Purpose: Central runtime settings for all services in the multi-agent system.
Input/Output: Reads environment variables and exposes typed configuration values.
Important invariants: Paths must stay inside mounted persistent volumes, and secrets are loaded only from runtime configuration.
How to debug: If services point at the wrong URLs, repos, or volumes, inspect the resolved settings values here first.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

LOGGER = logging.getLogger(__name__)


def validate_runtime_env_file(env_file: Path = Path(".env")) -> None:
    """Reject duplicate keys in `.env` because Compose and settings loaders otherwise override silently."""

    if not env_file.exists():
        return

    seen: dict[str, int] = {}
    duplicates: set[str] = set()
    for line_number, raw_line in enumerate(env_file.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in seen:
            duplicates.add(key)
            continue
        seen[key] = line_number

    if duplicates:
        duplicate_list = ", ".join(sorted(duplicates))
        raise RuntimeError(
            "Duplicate keys in .env detected: "
            f"{duplicate_list}. Remove duplicated keys because Docker Compose and the runtime would otherwise "
            "pick one value silently and make debugging much harder."
        )


class Settings(BaseSettings):
    """Typed environment-backed settings with safe defaults for local staging."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = Field(default="development", alias="APP_ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    service_name: str = Field(default="agent-service", alias="SERVICE_NAME")
    service_port: int = Field(default=8080, alias="SERVICE_PORT")

    data_dir: Path = Field(default=Path("./data"), alias="DATA_DIR")
    reports_dir: Path = Field(default=Path("./reports"), alias="REPORTS_DIR")
    workspace_root: Path = Field(default=Path("./workspace"), alias="WORKSPACE_ROOT")
    staging_stack_root: Path = Field(default=Path("./staging-stacks"), alias="STAGING_STACK_ROOT")
    orchestrator_db_path: Path = Field(
        default=Path("./data/orchestrator.db"),
        alias="ORCHESTRATOR_DB_PATH",
    )

    orchestrator_internal_url: str = Field(
        default="http://orchestrator:8080",
        alias="ORCHESTRATOR_INTERNAL_URL",
    )
    requirements_worker_url: str = Field(
        default="http://requirements-worker:8090",
        alias="REQUIREMENTS_WORKER_URL",
    )
    research_worker_url: str = Field(default="http://research-worker:8091", alias="RESEARCH_WORKER_URL")
    architecture_worker_url: str = Field(
        default="http://architecture-worker:8092",
        alias="ARCHITECTURE_WORKER_URL",
    )
    coding_worker_url: str = Field(default="http://coding-worker:8093", alias="CODING_WORKER_URL")
    reviewer_worker_url: str = Field(default="http://reviewer-worker:8094", alias="REVIEWER_WORKER_URL")
    test_worker_url: str = Field(default="http://test-worker:8095", alias="TEST_WORKER_URL")
    github_worker_url: str = Field(default="http://github-worker:8096", alias="GITHUB_WORKER_URL")
    deploy_worker_url: str = Field(default="http://deploy-worker:8097", alias="DEPLOY_WORKER_URL")
    qa_worker_url: str = Field(default="http://qa-worker:8098", alias="QA_WORKER_URL")
    security_worker_url: str = Field(default="http://security-worker:8099", alias="SECURITY_WORKER_URL")
    validation_worker_url: str = Field(
        default="http://validation-worker:8100",
        alias="VALIDATION_WORKER_URL",
    )
    documentation_worker_url: str = Field(
        default="http://documentation-worker:8101",
        alias="DOCUMENTATION_WORKER_URL",
    )
    memory_worker_url: str = Field(default="http://memory-worker:8102", alias="MEMORY_WORKER_URL")
    data_worker_url: str = Field(default="http://data-worker:8103", alias="DATA_WORKER_URL")
    ux_worker_url: str = Field(default="http://ux-worker:8104", alias="UX_WORKER_URL")
    cost_worker_url: str = Field(default="http://cost-worker:8105", alias="COST_WORKER_URL")
    human_resources_worker_url: str = Field(
        default="http://human-resources-worker:8106",
        alias="HUMAN_RESOURCES_WORKER_URL",
    )

    default_target_repo: str = Field(default="Feberdin/example-repo", alias="DEFAULT_TARGET_REPO")
    default_local_repo_path: str = Field(
        default="/workspace/example-repo",
        alias="DEFAULT_LOCAL_REPO_PATH",
    )
    default_base_branch: str = Field(default="main", alias="DEFAULT_BASE_BRANCH")

    model_routing_config: Path = Field(
        default=Path("/app/config/model-routing.example.yaml"),
        alias="MODEL_ROUTING_CONFIG",
    )
    default_model_api_key: str = Field(default="", alias="MODEL_API_KEY")
    default_model_api_key_file: Path | None = Field(default=None, alias="MODEL_API_KEY_FILE")
    default_model_provider: str = Field(default="qwen", alias="DEFAULT_MODEL_PROVIDER")
    mistral_base_url: str = Field(default="http://192.168.57.10:11434/v1", alias="MISTRAL_BASE_URL")
    qwen_base_url: str = Field(default="http://192.168.57.10:11434/v1", alias="QWEN_BASE_URL")
    mistral_model_name: str = Field(default="mistral-small3.2:latest", alias="MISTRAL_MODEL_NAME")
    qwen_model_name: str = Field(default="qwen3.5:35b-a3b", alias="QWEN_MODEL_NAME")
    mistral_api_key: str = Field(default="", alias="MISTRAL_API_KEY")
    mistral_api_key_file: Path | None = Field(default=None, alias="MISTRAL_API_KEY_FILE")
    qwen_api_key: str = Field(default="", alias="QWEN_API_KEY")
    qwen_api_key_file: Path | None = Field(default=None, alias="QWEN_API_KEY_FILE")

    coding_provider: str = Field(default="local_patch", alias="CODING_PROVIDER")
    openhands_enabled: bool = Field(default=False, alias="OPENHANDS_ENABLED")
    openhands_base_url: str = Field(default="http://openhands:3000", alias="OPENHANDS_BASE_URL")

    web_research_enabled: bool = Field(default=False, alias="WEB_RESEARCH_ENABLED")
    web_search_base_url: str = Field(default="", alias="WEB_SEARCH_BASE_URL")
    web_search_api_key: str = Field(default="", alias="WEB_SEARCH_API_KEY")
    web_search_api_key_file: Path | None = Field(default=None, alias="WEB_SEARCH_API_KEY_FILE")

    github_token: str = Field(default="", alias="GITHUB_TOKEN")
    github_token_file: Path | None = Field(default=None, alias="GITHUB_TOKEN_FILE")
    github_api_url: str = Field(default="https://api.github.com", alias="GITHUB_API_URL")
    github_mcp_enabled: bool = Field(default=False, alias="GITHUB_MCP_ENABLED")
    github_mcp_base_url: str = Field(default="http://github-mcp:8787", alias="GITHUB_MCP_BASE_URL")

    git_author_name: str = Field(default="Feberdin Agent Team", alias="GIT_AUTHOR_NAME")
    git_author_email: str = Field(default="agent-team@local.feberdin", alias="GIT_AUTHOR_EMAIL")
    git_commit_message_prefix: str = Field(
        default="feat(agent-team)",
        alias="GIT_COMMIT_MESSAGE_PREFIX",
    )

    auto_deploy_staging: bool = Field(default=True, alias="AUTO_DEPLOY_STAGING")
    staging_deploy_strategy: str = Field(default="docker_compose", alias="STAGING_DEPLOY_STRATEGY")
    staging_host: str = Field(default="unraid-staging.local", alias="STAGING_HOST")
    staging_ssh_port: int = Field(default=22, alias="STAGING_SSH_PORT")
    staging_ssh_user: str = Field(default="appuser", alias="STAGING_SSH_USER")
    staging_project_dir: str = Field(
        default="/mnt/user/appdata/feberdin-staging/example-app",
        alias="STAGING_PROJECT_DIR",
    )
    staging_compose_file: str = Field(default="docker-compose.staging.yml", alias="STAGING_COMPOSE_FILE")
    staging_healthcheck_url: str = Field(
        default="http://unraid-staging.local:9000/health",
        alias="STAGING_HEALTHCHECK_URL",
    )
    staging_git_branch: str = Field(default="main", alias="STAGING_GIT_BRANCH")
    self_hosted_runner_labels: str = Field(
        default="self-hosted,linux,unraid,staging",
        alias="SELF_HOSTED_RUNNER_LABELS",
    )

    def ensure_runtime_directories(self) -> None:
        """Create runtime directories early so services fail less often at first write."""

        directories = {
            "DATA_DIR": self.data_dir,
            "REPORTS_DIR": self.reports_dir,
            "WORKSPACE_ROOT": self.workspace_root,
            "STAGING_STACK_ROOT": self.staging_stack_root,
        }
        for env_name, directory in directories.items():
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except PermissionError as exc:
                raise RuntimeError(
                    f"Runtime path '{directory}' for {env_name} is not writable. "
                    "This usually means the Docker bind mount is missing or points to a path with wrong permissions. "
                    "Check the host path, then verify the final Compose model contains the expected mount."
                ) from exc

    def apply_secret_file_overrides(self) -> None:
        """Load secret values from *_FILE paths when the plain environment variables are empty."""

        self.default_model_api_key = self._resolve_secret_value(
            self.default_model_api_key,
            self.default_model_api_key_file,
        )
        self.mistral_api_key = self._resolve_secret_value(self.mistral_api_key, self.mistral_api_key_file)
        self.qwen_api_key = self._resolve_secret_value(self.qwen_api_key, self.qwen_api_key_file)
        self.web_search_api_key = self._resolve_secret_value(self.web_search_api_key, self.web_search_api_key_file)
        self.github_token = self._resolve_secret_value(self.github_token, self.github_token_file)

    def _resolve_secret_value(self, current_value: str, secret_file: Path | None) -> str:
        """Prefer the explicit env value and otherwise read the first-line-like content from a mounted secret file."""

        normalized_value = current_value.strip()
        effective_value = normalized_value if normalized_value and "replace-me" not in normalized_value.lower() else ""
        if effective_value:
            return effective_value
        if secret_file is None:
            return effective_value
        try:
            if not secret_file.exists():
                return effective_value
            return secret_file.read_text(encoding="utf-8").rstrip("\r\n")
        except PermissionError:
            LOGGER.warning(
                "Secret file '%s' is not readable for the current service user. "
                "The service will continue without this secret. "
                "Fix the host-side permissions or adjust PUID/PGID if the secret is required.",
                secret_file,
            )
            return effective_value

    @property
    def database_url(self) -> str:
        """Return a SQLite URL for SQLAlchemy."""

        return f"sqlite:///{self.orchestrator_db_path}"

    def task_report_dir(self, task_id: str) -> Path:
        """Return the report directory for a single task."""

        return self.reports_dir / task_id

    def has_llm_backend(self) -> bool:
        """Return True when the configured model backend looks usable."""

        return bool((self.mistral_base_url and self.mistral_model_name) or (self.qwen_base_url and self.qwen_model_name))

    def model_provider_configs(self) -> dict[str, dict[str, Any]]:
        """Return provider registry information for the model router."""

        return {
            "mistral": {
                "base_url": self.mistral_base_url,
                "model_name": self.mistral_model_name,
                "api_key": self.mistral_api_key or self.default_model_api_key,
            },
            "qwen": {
                "base_url": self.qwen_base_url,
                "model_name": self.qwen_model_name,
                "api_key": self.qwen_api_key or self.default_model_api_key,
            },
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings because every service resolves the same environment repeatedly."""

    validate_runtime_env_file()
    settings = Settings()
    settings.apply_secret_file_overrides()
    settings.ensure_runtime_directories()
    return settings
