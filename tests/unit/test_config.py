"""
Purpose: Verify that runtime settings can load secret values from mounted files without storing plaintext in the repo env file.
Input/Output: Tests construct settings with temporary secret files and inspect the resolved token values.
Important invariants: Plain env vars win when set, and *_FILE paths are a safe fallback for project-local secrets.
How to debug: If these tests fail, inspect `services/shared/agentic_lab/config.py` and the expected *_FILE environment names.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from services.shared.agentic_lab.config import Settings


def test_settings_load_secret_value_from_file(monkeypatch, tmp_path: Path) -> None:
    github_token_file = tmp_path / "github_token"
    github_token_file.write_text("ghp_test_value\n", encoding="utf-8")

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN_FILE", str(github_token_file))

    settings = Settings()
    settings.apply_secret_file_overrides()

    assert settings.github_token == "ghp_test_value"


def test_settings_prefers_explicit_env_over_secret_file(monkeypatch, tmp_path: Path) -> None:
    github_token_file = tmp_path / "github_token"
    github_token_file.write_text("ghp_from_file\n", encoding="utf-8")

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_from_env")
    monkeypatch.setenv("GITHUB_TOKEN_FILE", str(github_token_file))

    settings = Settings()
    settings.apply_secret_file_overrides()

    assert settings.github_token == "ghp_from_env"


def test_settings_ignore_unreadable_secret_file(monkeypatch, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    unreadable_path = tmp_path / "unreadable_token"
    caplog.set_level("WARNING")

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN_FILE", str(unreadable_path))

    original_exists = Path.exists

    def fake_exists(path: Path) -> bool:
        if path == unreadable_path:
            raise PermissionError("permission denied")
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", fake_exists)

    settings = Settings()
    settings.apply_secret_file_overrides()

    assert settings.github_token == ""
    assert "not readable" in caplog.text


def test_settings_ignore_permission_error_while_reading_secret(
    monkeypatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    unreadable_path = tmp_path / "github_token"
    unreadable_path.write_text("should-not-be-readable", encoding="utf-8")
    caplog.set_level("WARNING")

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN_FILE", str(unreadable_path))

    original_read_text = Path.read_text

    def fake_read_text(path: Path, *args, **kwargs) -> str:
        if path == unreadable_path:
            raise PermissionError("permission denied")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)

    settings = Settings()
    settings.apply_secret_file_overrides()

    assert settings.github_token == ""
    assert "not readable" in caplog.text


def test_settings_load_slow_runtime_timeout_aliases(monkeypatch) -> None:
    monkeypatch.setenv("LLM_CONNECT_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("LLM_READ_TIMEOUT_SECONDS", "1500")
    monkeypatch.setenv("LLM_WRITE_TIMEOUT_SECONDS", "60")
    monkeypatch.setenv("LLM_POOL_TIMEOUT_SECONDS", "60")
    monkeypatch.setenv("WORKER_STAGE_TIMEOUT_SECONDS", "1800")
    monkeypatch.setenv("STAGE_HEARTBEAT_INTERVAL_SECONDS", "25")

    settings = Settings()

    assert settings.llm_connect_timeout_seconds == 30
    assert settings.llm_read_timeout_seconds == 1500
    assert settings.llm_write_timeout_seconds == 60
    assert settings.llm_pool_timeout_seconds == 60
    assert settings.worker_stage_timeout_seconds == 1800
    assert settings.stage_heartbeat_interval_seconds == 25
