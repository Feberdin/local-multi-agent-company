"""
Purpose: Verify that runtime settings can load secret values from mounted files without storing plaintext in the repo env file.
Input/Output: Tests construct settings with temporary secret files and inspect the resolved token values.
Important invariants: Plain env vars win when set, and *_FILE paths are a safe fallback for project-local secrets.
How to debug: If these tests fail, inspect `services/shared/agentic_lab/config.py` and the expected *_FILE environment names.
"""

from __future__ import annotations

from pathlib import Path

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
