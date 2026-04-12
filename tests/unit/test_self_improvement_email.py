"""
Purpose: Test the self-improvement mail layer without depending on a live SMTP server.
Input/Output: The tests trigger approval/info mail creation and inspect the durable outbox
              plus the operator-facing delivery status returned by the service.
Important invariants:
  - Every mail attempt writes an auditable JSON file to disk.
  - Missing SMTP configuration becomes a readable queued/skipped state, not a crash.
How to debug: Inspect DATA_DIR/self-improvement-email-outbox and the returned status fields.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.shared.agentic_lab.config import Settings
from services.shared.agentic_lab.self_improvement_email import SelfImprovementEmailService


def _email_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **overrides: str) -> Settings:
    """Build isolated settings for one mail delivery scenario."""

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    for key, value in overrides.items():
        monkeypatch.setenv(key, value)
    return Settings()


@pytest.mark.asyncio
async def test_send_cycle_email_writes_outbox_when_email_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _email_settings(tmp_path, monkeypatch, SELF_IMPROVEMENT_EMAIL_ENABLED="false")
    service = SelfImprovementEmailService(settings)

    result = await service.send_cycle_email(
        subject="Self-Improvement Test",
        body="Nur eine Outbox-Datei soll entstehen.",
        kind="info",
        metadata={"cycle_id": "cycle-1"},
    )

    assert result.status == "skipped"
    outbox_path = Path(result.outbox_path)
    assert outbox_path.exists() is True
    payload = json.loads(outbox_path.read_text(encoding="utf-8"))
    assert payload["kind"] == "info"
    assert payload["metadata"]["cycle_id"] == "cycle-1"


@pytest.mark.asyncio
async def test_send_cycle_email_is_queued_when_smtp_is_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _email_settings(
        tmp_path,
        monkeypatch,
        SELF_IMPROVEMENT_EMAIL_ENABLED="true",
        SELF_IMPROVEMENT_EMAIL_TO="operator@example.com",
        SELF_IMPROVEMENT_EMAIL_FROM="feberdin@example.com",
    )
    service = SelfImprovementEmailService(settings)

    result = await service.send_cycle_email(
        subject="Freigabe noetig",
        body="SMTP ist absichtlich nicht vollstaendig konfiguriert.",
        kind="approval",
        metadata={"cycle_id": "cycle-2"},
    )

    assert result.status == "queued"
    assert "SMTP" in result.detail
    assert Path(result.outbox_path).exists() is True
