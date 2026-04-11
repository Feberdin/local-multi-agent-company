"""
Unit tests for the system readiness check module.
Tests cover status aggregation, secret file checks, env var checks, and duration formatting.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from services.shared.agentic_lab.readiness import (
    CheckStatus,
    ReadinessCheckItem,
    ReadinessSection,
    _check_env_var,
    _check_git_available,
    _check_path_readable,
    _check_secret_file,
    _overall_status,
    _section_status,
    _summarize,
    _workflow_message,
    ReadinessSummary,
)


# ---------------------------------------------------------------------------
# Status aggregation
# ---------------------------------------------------------------------------

def _make_check(status: CheckStatus) -> ReadinessCheckItem:
    return ReadinessCheckItem(name="x", label="x", status=status, message="x")


def _make_section(checks: list[ReadinessCheckItem]) -> ReadinessSection:
    return ReadinessSection(name="s", label="s", status=_section_status(checks), checks=checks)


class TestSectionStatus:
    def test_all_ok_returns_ok(self) -> None:
        checks = [_make_check(CheckStatus.OK)] * 3
        assert _section_status(checks) == CheckStatus.OK

    def test_one_warning_returns_warning(self) -> None:
        checks = [_make_check(CheckStatus.OK), _make_check(CheckStatus.WARNING)]
        assert _section_status(checks) == CheckStatus.WARNING

    def test_one_failed_returns_failed(self) -> None:
        checks = [_make_check(CheckStatus.OK), _make_check(CheckStatus.WARNING), _make_check(CheckStatus.FAILED)]
        assert _section_status(checks) == CheckStatus.FAILED

    def test_all_skipped_returns_skipped(self) -> None:
        checks = [_make_check(CheckStatus.SKIPPED)] * 2
        assert _section_status(checks) == CheckStatus.SKIPPED

    def test_skipped_mixed_with_ok_returns_ok(self) -> None:
        checks = [_make_check(CheckStatus.OK), _make_check(CheckStatus.SKIPPED)]
        assert _section_status(checks) == CheckStatus.OK

    def test_empty_returns_skipped(self) -> None:
        assert _section_status([]) == CheckStatus.SKIPPED


class TestOverallStatus:
    def test_all_sections_ok(self) -> None:
        sections = [_make_section([_make_check(CheckStatus.OK)]) for _ in range(3)]
        assert _overall_status(sections) == CheckStatus.OK

    def test_one_section_failed(self) -> None:
        sections = [
            _make_section([_make_check(CheckStatus.OK)]),
            _make_section([_make_check(CheckStatus.FAILED)]),
        ]
        assert _overall_status(sections) == CheckStatus.FAILED

    def test_degraded_when_only_warnings(self) -> None:
        sections = [
            _make_section([_make_check(CheckStatus.OK)]),
            _make_section([_make_check(CheckStatus.WARNING)]),
        ]
        assert _overall_status(sections) == CheckStatus.WARNING


class TestSummarize:
    def test_counts_correctly(self) -> None:
        sections = [
            _make_section([_make_check(CheckStatus.OK), _make_check(CheckStatus.OK)]),
            _make_section([_make_check(CheckStatus.WARNING), _make_check(CheckStatus.FAILED)]),
            _make_section([_make_check(CheckStatus.SKIPPED)]),
        ]
        summary = _summarize(sections)
        assert summary.checks_total == 5
        assert summary.checks_ok == 2
        assert summary.checks_warning == 1
        assert summary.checks_failed == 1
        assert summary.checks_skipped == 1


class TestWorkflowMessage:
    def test_ok_is_ready(self) -> None:
        summary = ReadinessSummary(checks_total=3, checks_ok=3)
        ready, msg = _workflow_message(CheckStatus.OK, summary)
        assert ready is True
        assert "einsatzbereit" in msg.lower()

    def test_warning_is_ready_but_degraded(self) -> None:
        summary = ReadinessSummary(checks_total=3, checks_ok=2, checks_warning=1)
        ready, msg = _workflow_message(CheckStatus.WARNING, summary)
        assert ready is True
        assert "eingeschraenkt" in msg.lower()

    def test_failed_is_not_ready(self) -> None:
        summary = ReadinessSummary(checks_total=3, checks_ok=1, checks_failed=2)
        ready, msg = _workflow_message(CheckStatus.FAILED, summary)
        assert ready is False
        assert "nicht gestartet" in msg.lower()


# ---------------------------------------------------------------------------
# Config checks
# ---------------------------------------------------------------------------

class TestCheckEnvVar:
    def test_set_value_returns_ok(self) -> None:
        item = _check_env_var("http://localhost:11434/v1", "MISTRAL_BASE_URL", "Base URL", "url")
        assert item.status == CheckStatus.OK

    def test_empty_value_required_returns_failed(self) -> None:
        item = _check_env_var("", "MISTRAL_BASE_URL", "Base URL", "url", required=True)
        assert item.status == CheckStatus.FAILED

    def test_empty_value_optional_returns_warning(self) -> None:
        item = _check_env_var("", "GITHUB_TOKEN", "Token", "tok", required=False)
        assert item.status == CheckStatus.WARNING

    def test_placeholder_treated_as_missing(self) -> None:
        item = _check_env_var("replace-me", "GITHUB_TOKEN", "Token", "tok", required=True)
        assert item.status == CheckStatus.FAILED


# ---------------------------------------------------------------------------
# Secret file checks
# ---------------------------------------------------------------------------

class TestCheckSecretFile:
    def test_none_path_returns_skipped(self) -> None:
        item = _check_secret_file(None, "Key", "key")
        assert item.status == CheckStatus.SKIPPED

    def test_missing_file_returns_warning(self, tmp_path: Path) -> None:
        missing = tmp_path / "nothere.txt"
        item = _check_secret_file(missing, "Key", "key")
        assert item.status == CheckStatus.WARNING

    def test_existing_readable_file_returns_ok(self, tmp_path: Path) -> None:
        f = tmp_path / "secret.txt"
        f.write_text("token-value", encoding="utf-8")
        item = _check_secret_file(f, "Key", "key")
        assert item.status == CheckStatus.OK

    def test_unreadable_file_returns_failed(self, tmp_path: Path) -> None:
        f = tmp_path / "secret.txt"
        f.write_text("token-value", encoding="utf-8")
        f.chmod(0o000)
        try:
            item = _check_secret_file(f, "Key", "key")
            assert item.status == CheckStatus.FAILED
        finally:
            f.chmod(0o644)


# ---------------------------------------------------------------------------
# Path readability
# ---------------------------------------------------------------------------

class TestCheckPathReadable:
    def test_nonexistent_path_returns_failed(self, tmp_path: Path) -> None:
        missing = tmp_path / "nope"
        item = _check_path_readable(missing, "Dir", "dir")
        assert item.status == CheckStatus.FAILED

    def test_existing_writable_path_returns_ok(self, tmp_path: Path) -> None:
        item = _check_path_readable(tmp_path, "Dir", "dir")
        assert item.status == CheckStatus.OK


# ---------------------------------------------------------------------------
# Git availability (non-destructive subprocess check)
# ---------------------------------------------------------------------------

class TestCheckGitAvailable:
    def test_returns_a_check_item(self) -> None:
        item = _check_git_available()
        # We don't assert OK/FAILED since git may or may not be available in CI.
        assert item.name == "git-available"
        assert item.status in {CheckStatus.OK, CheckStatus.FAILED, CheckStatus.WARNING}
