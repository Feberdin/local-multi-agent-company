"""
Purpose: Central runtime settings for all services in the multi-agent system.
Input/Output: Reads environment variables and exposes typed configuration values.
Important invariants: Paths must stay inside mounted persistent volumes, and secrets are loaded only from runtime configuration.
How to debug: If services point at the wrong URLs, repos, or volumes, inspect the resolved settings values here first.
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SecretFileProbe:
    """Small operator-facing state snapshot for one configured secret file path."""

    env_state: str
    state: str
    path: str | None
    configured: bool
    exists: bool
    readable: bool
    is_directory: bool
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation for readiness reports and tests."""

        return asdict(self)


def normalize_optional_path_value(value: Any) -> Path | None:
    """Treat unset, empty, or whitespace-only path values as absent instead of `Path('.')`."""

    if value is None:
        return None
    if isinstance(value, Path):
        return value
    if isinstance(value, os.PathLike):
        return Path(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        return Path(stripped)
    return Path(str(value))


def inspect_secret_file(secret_file: Path | None, *, raw_env_value: str | None = None) -> SecretFileProbe:
    """Classify one secret-file path without exposing any secret contents."""

    env_state = "not_set"
    if raw_env_value is not None:
        env_state = "configured" if raw_env_value.strip() else "empty_ignored"

    if secret_file is None:
        return SecretFileProbe(
            env_state=env_state,
            state=env_state,
            path=None,
            configured=False,
            exists=False,
            readable=False,
            is_directory=False,
            detail="Kein Secret-Dateipfad gesetzt." if env_state == "not_set" else "Leerer Secret-Dateipfad wird ignoriert.",
        )

    try:
        if not secret_file.exists():
            return SecretFileProbe(
                env_state=env_state,
                state="missing",
                path=str(secret_file),
                configured=True,
                exists=False,
                readable=False,
                is_directory=False,
                detail="Datei wurde konfiguriert, existiert aber nicht.",
            )
        if secret_file.is_dir():
            return SecretFileProbe(
                env_state=env_state,
                state="directory",
                path=str(secret_file),
                configured=True,
                exists=True,
                readable=False,
                is_directory=True,
                detail="Pfad zeigt auf ein Verzeichnis statt auf eine Datei.",
            )
        if not secret_file.is_file():
            return SecretFileProbe(
                env_state=env_state,
                state="invalid_path",
                path=str(secret_file),
                configured=True,
                exists=True,
                readable=False,
                is_directory=False,
                detail="Pfad ist kein regulaerer Dateipfad.",
            )
        with secret_file.open("r", encoding="utf-8") as handle:
            handle.read(0)
        return SecretFileProbe(
            env_state=env_state,
            state="ok",
            path=str(secret_file),
            configured=True,
            exists=True,
            readable=True,
            is_directory=False,
            detail="Datei ist vorhanden und lesbar.",
        )
    except PermissionError:
        return SecretFileProbe(
            env_state=env_state,
            state="not_readable",
            path=str(secret_file),
            configured=True,
            exists=True,
            readable=False,
            is_directory=False,
            detail="Datei ist fuer den aktuellen Service-User nicht lesbar.",
        )
    except OSError as exc:
        return SecretFileProbe(
            env_state=env_state,
            state="invalid_path",
            path=str(secret_file),
            configured=True,
            exists=True,
            readable=False,
            is_directory=False,
            detail=f"Pfad konnte nicht geprueft werden: {exc}",
        )


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

    @field_validator(
        "default_model_api_key_file",
        "mistral_api_key_file",
        "qwen_api_key_file",
        "web_search_api_key_file",
        "github_token_file",
        "self_improvement_smtp_password_file",
        mode="before",
    )
    @classmethod
    def _normalize_optional_secret_file_paths(cls, value: Any) -> Path | None:
        """Normalize optional secret-file env values so empty strings become `None` instead of `Path('.')`."""

        return normalize_optional_path_value(value)

    app_env: str = Field(default="development", alias="APP_ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    service_name: str = Field(default="agent-service", alias="SERVICE_NAME")
    service_port: int = Field(default=8080, alias="SERVICE_PORT")
    ui_timezone: str = Field(
        default="Europe/Berlin",
        validation_alias=AliasChoices("UI_TIMEZONE", "TZ"),
    )

    data_dir: Path = Field(default=Path("./data"), alias="DATA_DIR")
    reports_dir: Path = Field(default=Path("./reports"), alias="REPORTS_DIR")
    workspace_root: Path = Field(default=Path("./workspace"), alias="WORKSPACE_ROOT")
    staging_stack_root: Path = Field(default=Path("./staging-stacks"), alias="STAGING_STACK_ROOT")
    runtime_home_dir: Path = Field(default=Path("/tmp/agent-home"), alias="RUNTIME_HOME_DIR")
    task_workspace_root: Path | None = Field(default=None, alias="TASK_WORKSPACE_ROOT")
    orchestrator_db_path: Path = Field(
        default=Path("./data/orchestrator.db"),
        alias="ORCHESTRATOR_DB_PATH",
    )

    orchestrator_internal_url: str = Field(
        default="http://orchestrator:8080",
        alias="ORCHESTRATOR_INTERNAL_URL",
    )
    web_ui_internal_url: str = Field(default="http://web-ui:8088", alias="WEB_UI_INTERNAL_URL")
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
    rollback_worker_url: str = Field(default="http://rollback-worker:8107", alias="ROLLBACK_WORKER_URL")
    worker_connect_timeout_seconds: float = Field(
        default=30.0,
        validation_alias=AliasChoices("WORKER_CONNECT_TIMEOUT_SECONDS", "WORKER_TIMEOUT_CONNECT_SECONDS"),
    )
    worker_stage_timeout_seconds: float = Field(
        default=1800.0,
        validation_alias=AliasChoices("WORKER_STAGE_TIMEOUT_SECONDS", "WORKER_TIMEOUT_READ_SECONDS"),
    )
    worker_write_timeout_seconds: float = Field(
        default=60.0,
        validation_alias=AliasChoices("WORKER_WRITE_TIMEOUT_SECONDS", "WORKER_TIMEOUT_WRITE_SECONDS"),
    )
    worker_pool_timeout_seconds: float = Field(
        default=60.0,
        validation_alias=AliasChoices("WORKER_POOL_TIMEOUT_SECONDS", "WORKER_TIMEOUT_POOL_SECONDS"),
    )
    worker_retry_attempts: int = Field(default=3, alias="WORKER_RETRY_ATTEMPTS")
    stage_heartbeat_interval_seconds: float = Field(
        default=30.0,
        validation_alias=AliasChoices("STAGE_HEARTBEAT_INTERVAL_SECONDS", "WORKER_HEARTBEAT_INTERVAL_SECONDS"),
    )
    readiness_http_fast_timeout_seconds: float = Field(default=8.0, alias="READINESS_HTTP_FAST_TIMEOUT_SECONDS")
    readiness_http_deep_timeout_seconds: float = Field(default=45.0, alias="READINESS_HTTP_DEEP_TIMEOUT_SECONDS")
    readiness_llm_smoke_timeout_seconds: float = Field(default=240.0, alias="READINESS_LLM_SMOKE_TIMEOUT_SECONDS")
    readiness_worker_smoke_timeout_seconds: float = Field(default=20.0, alias="READINESS_WORKER_SMOKE_TIMEOUT_SECONDS")
    readiness_git_timeout_seconds: float = Field(default=25.0, alias="READINESS_GIT_TIMEOUT_SECONDS")
    readiness_slow_warning_seconds: float = Field(default=20.0, alias="READINESS_SLOW_WARNING_SECONDS")

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
    default_model_provider: str = Field(default="mistral", alias="DEFAULT_MODEL_PROVIDER")
    llm_connect_timeout_seconds: float = Field(
        default=30.0,
        validation_alias=AliasChoices("LLM_CONNECT_TIMEOUT_SECONDS", "LLM_TIMEOUT_CONNECT_SECONDS"),
    )
    llm_read_timeout_seconds: float = Field(
        default=1200.0,
        validation_alias=AliasChoices("LLM_READ_TIMEOUT_SECONDS", "LLM_TIMEOUT_READ_SECONDS"),
    )
    llm_write_timeout_seconds: float = Field(
        default=60.0,
        validation_alias=AliasChoices("LLM_WRITE_TIMEOUT_SECONDS", "LLM_TIMEOUT_WRITE_SECONDS"),
    )
    llm_pool_timeout_seconds: float = Field(
        default=60.0,
        validation_alias=AliasChoices("LLM_POOL_TIMEOUT_SECONDS", "LLM_TIMEOUT_POOL_SECONDS"),
    )
    llm_request_deadline_seconds: float = Field(default=1500.0, alias="LLM_REQUEST_DEADLINE_SECONDS")
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

    # ── Self-Improvement ──────────────────────────────────────────────────────
    self_improvement_enabled: bool = Field(default=False, alias="SELF_IMPROVEMENT_ENABLED")
    self_improvement_mode: str = Field(default="manual", alias="SELF_IMPROVEMENT_MODE")
    self_improvement_policy_path: Path = Field(
        default=Path("/app/config/self-improvement.policy.yaml"),
        alias="SELF_IMPROVEMENT_POLICY_PATH",
    )
    self_improvement_max_auto_fix_attempts: int = Field(
        default=3, alias="SELF_IMPROVEMENT_MAX_AUTO_FIX_ATTEMPTS"
    )
    self_improvement_max_cycles_per_day: int = Field(
        default=5, alias="SELF_IMPROVEMENT_MAX_CYCLES_PER_DAY"
    )
    self_improvement_max_session_cycles: int = Field(
        default=3, alias="SELF_IMPROVEMENT_MAX_SESSION_CYCLES"
    )
    self_improvement_deploy_after_success: bool = Field(
        default=False, alias="SELF_IMPROVEMENT_DEPLOY_AFTER_SUCCESS"
    )
    self_improvement_require_approval_for_risky: bool = Field(
        default=True, alias="SELF_IMPROVEMENT_REQUIRE_APPROVAL_FOR_RISKY"
    )
    self_improvement_preflight_required: bool = Field(
        default=True, alias="SELF_IMPROVEMENT_PREFLIGHT_REQUIRED"
    )
    self_improvement_auto_rollback: bool = Field(
        default=False, alias="SELF_IMPROVEMENT_AUTO_ROLLBACK"
    )
    self_improvement_target_repo: str = Field(
        default="Feberdin/local-multi-agent-company",
        alias="SELF_IMPROVEMENT_TARGET_REPO",
    )
    self_improvement_local_repo_path: str = Field(
        default="/workspace/local-multi-agent-company",
        alias="SELF_IMPROVEMENT_LOCAL_REPO_PATH",
    )
    self_improvement_email_enabled: bool = Field(default=False, alias="SELF_IMPROVEMENT_EMAIL_ENABLED")
    self_improvement_email_to: str = Field(default="", alias="SELF_IMPROVEMENT_EMAIL_TO")
    self_improvement_email_from: str = Field(default="", alias="SELF_IMPROVEMENT_EMAIL_FROM")
    self_improvement_email_reply_to: str = Field(default="", alias="SELF_IMPROVEMENT_EMAIL_REPLY_TO")
    self_improvement_smtp_host: str = Field(default="", alias="SELF_IMPROVEMENT_SMTP_HOST")
    self_improvement_smtp_port: int = Field(default=587, alias="SELF_IMPROVEMENT_SMTP_PORT")
    self_improvement_smtp_username: str = Field(default="", alias="SELF_IMPROVEMENT_SMTP_USERNAME")
    self_improvement_smtp_password: str = Field(default="", alias="SELF_IMPROVEMENT_SMTP_PASSWORD")
    self_improvement_smtp_password_file: Path | None = Field(
        default=None,
        alias="SELF_IMPROVEMENT_SMTP_PASSWORD_FILE",
    )
    self_improvement_smtp_use_starttls: bool = Field(
        default=True,
        alias="SELF_IMPROVEMENT_SMTP_USE_STARTTLS",
    )
    self_improvement_email_timeout_seconds: float = Field(
        default=20.0,
        alias="SELF_IMPROVEMENT_EMAIL_TIMEOUT_SECONDS",
    )
    self_update_watchdog_poll_seconds: float = Field(
        default=5.0,
        alias="SELF_UPDATE_WATCHDOG_POLL_SECONDS",
    )
    self_update_watchdog_timeout_seconds: float = Field(
        default=240.0,
        alias="SELF_UPDATE_WATCHDOG_TIMEOUT_SECONDS",
    )

    # ── Self-Host (autonomous self-update of the agent stack itself) ─────────
    self_host_ssh_user: str = Field(default="root", alias="SELF_HOST_SSH_USER")
    self_host_ssh_host: str = Field(default="", alias="SELF_HOST_SSH_HOST")
    self_host_ssh_port: int = Field(default=22, alias="SELF_HOST_SSH_PORT")
    self_host_project_dir: str = Field(default="", alias="SELF_HOST_PROJECT_DIR")
    self_host_compose_file: str = Field(default="docker-compose.yml", alias="SELF_HOST_COMPOSE_FILE")
    self_host_health_url: str = Field(default="", alias="SELF_HOST_HEALTH_URL")
    self_host_ssh_key_file: str = Field(default="", alias="SELF_HOST_SSH_KEY_FILE")

    # ── Auto-Debug ────────────────────────────────────────────────────────────
    auto_debug_enabled: bool = Field(default=False, alias="AUTO_DEBUG_ENABLED")
    auto_debug_max_attempts: int = Field(default=2, alias="AUTO_DEBUG_MAX_ATTEMPTS")
    github_auto_fix_enabled: bool = Field(default=True, alias="GITHUB_AUTO_FIX_ENABLED")
    github_auto_fix_poll_seconds: float = Field(default=120.0, alias="GITHUB_AUTO_FIX_POLL_SECONDS")
    github_auto_fix_max_attempts: int = Field(default=2, alias="GITHUB_AUTO_FIX_MAX_ATTEMPTS")

    def ensure_runtime_directories(self) -> None:
        """Create runtime directories early so services fail less often at first write."""

        directories = {
            "DATA_DIR": self.data_dir,
            "REPORTS_DIR": self.reports_dir,
            "WORKSPACE_ROOT": self.workspace_root,
            "TASK_WORKSPACE_ROOT": self.effective_task_workspace_root,
            "STAGING_STACK_ROOT": self.staging_stack_root,
            "RUNTIME_HOME_DIR": self.runtime_home_dir,
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

    @property
    def effective_task_workspace_root(self) -> Path:
        """Return the operator-configurable task workspace root or the safe default below WORKSPACE_ROOT."""

        return self.task_workspace_root or (self.workspace_root / ".task-workspaces")

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
        self.self_improvement_smtp_password = self._resolve_secret_value(
            self.self_improvement_smtp_password,
            self.self_improvement_smtp_password_file,
        )

    def _resolve_secret_value(self, current_value: str, secret_file: Path | None) -> str:
        """Prefer the explicit env value and otherwise read the first-line-like content from a mounted secret file."""

        normalized_value = current_value.strip()
        effective_value = normalized_value if normalized_value and "replace-me" not in normalized_value.lower() else ""
        if effective_value:
            return effective_value
        if secret_file is None:
            return effective_value

        probe = inspect_secret_file(secret_file)
        if probe.state in {"not_set", "empty_ignored", "missing"}:
            return effective_value
        if probe.state == "directory":
            LOGGER.warning(
                "Secret path '%s' points to a directory, not to a readable file. "
                "The service will continue without this optional secret until the path is corrected.",
                secret_file,
            )
            return effective_value
        if probe.state == "not_readable":
            LOGGER.warning(
                "Secret file '%s' is not readable for the current service user. "
                "The service will continue without this secret. "
                "Fix the host-side permissions or adjust PUID/PGID if the secret is required.",
                secret_file,
            )
            return effective_value
        if probe.state == "invalid_path":
            LOGGER.warning(
                "Secret path '%s' is not a valid readable file (%s). "
                "The service will continue without this optional secret.",
                secret_file,
                probe.detail,
            )
            return effective_value

        try:
            return secret_file.read_text(encoding="utf-8").rstrip("\r\n")
        except PermissionError:
            LOGGER.warning(
                "Secret file '%s' is not readable for the current service user. "
                "The service will continue without this secret. "
                "Fix the host-side permissions or adjust PUID/PGID if the secret is required.",
                secret_file,
            )
            return effective_value
        except OSError as exc:
            LOGGER.warning(
                "Secret file '%s' could not be read (%s). "
                "The service will continue without this secret because it is optional by default.",
                secret_file,
                exc,
            )
            return effective_value

    def llm_http_timeout(self) -> httpx.Timeout:
        """Return the shared HTTP timeout profile for model backend calls."""

        return httpx.Timeout(
            connect=self.llm_connect_timeout_seconds,
            read=self.llm_read_timeout_seconds,
            write=self.llm_write_timeout_seconds,
            pool=self.llm_pool_timeout_seconds,
        )

    def worker_http_timeout(self) -> httpx.Timeout:
        """Return the shared HTTP timeout profile for orchestrator-to-worker calls."""

        return httpx.Timeout(
            connect=self.worker_connect_timeout_seconds,
            read=self.worker_stage_timeout_seconds,
            write=self.worker_write_timeout_seconds,
            pool=self.worker_pool_timeout_seconds,
        )

    def llm_timeout_summary(self, *, request_deadline_seconds: float | None = None) -> str:
        """Return a compact timeout summary for operator-visible error messages."""

        deadline = request_deadline_seconds or self.llm_request_deadline_seconds
        return (
            f"connect={self.llm_connect_timeout_seconds}s, "
            f"read={self.llm_read_timeout_seconds}s, "
            f"write={self.llm_write_timeout_seconds}s, "
            f"pool={self.llm_pool_timeout_seconds}s, "
            f"deadline={deadline}s"
        )

    def worker_timeout_summary(self) -> str:
        """Return a compact timeout summary for worker transport diagnostics."""

        return (
            f"connect={self.worker_connect_timeout_seconds}s, "
            f"stage_read={self.worker_stage_timeout_seconds}s, "
            f"write={self.worker_write_timeout_seconds}s, "
            f"pool={self.worker_pool_timeout_seconds}s"
        )

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
