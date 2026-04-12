"""
Purpose: Asynchronous approval and information mail delivery for self-improvement cycles.
Input/Output: Builds operator-friendly mail payloads, writes them into an auditable outbox,
              and optionally sends them through SMTP if the environment is configured.
Important invariants:
  - Mails never include secret values or raw prompt contents.
  - The outbox is always written, even when SMTP is unavailable.
  - Delivery failure must never crash the self-improvement pipeline.
How to debug: Inspect DATA_DIR/self-improvement-email-outbox first, then the SMTP settings
              and the returned delivery status stored in cycle metadata.
"""

from __future__ import annotations

import asyncio
import json
import smtplib
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from uuid import uuid4

from services.shared.agentic_lab.config import Settings


@dataclass(slots=True)
class EmailDeliveryResult:
    status: str
    detail: str
    outbox_path: str
    message_id: str


class SelfImprovementEmailService:
    """Queue every approval/info mail to disk and optionally deliver it via SMTP."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def outbox_dir(self) -> Path:
        return self.settings.data_dir / "self-improvement-email-outbox"

    async def send_cycle_email(
        self,
        *,
        subject: str,
        body: str,
        kind: str,
        metadata: dict[str, Any],
    ) -> EmailDeliveryResult:
        """Write an outbox entry and try SMTP delivery without blocking the event loop for long."""

        self.outbox_dir.mkdir(parents=True, exist_ok=True)
        message_id = str(uuid4())
        record = {
            "id": message_id,
            "kind": kind,
            "created_at": datetime.now(UTC).isoformat(),
            "subject": subject,
            "body": body,
            "metadata": metadata,
            "from": self.settings.self_improvement_email_from,
            "to": self.settings.self_improvement_email_to,
        }
        outbox_path = self.outbox_dir / f"{message_id}.json"
        outbox_path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")

        if not self.settings.self_improvement_email_enabled:
            return EmailDeliveryResult(
                status="skipped",
                detail="E-Mail-Versand ist deaktiviert. Die Nachricht wurde nur im Outbox-Ordner abgelegt.",
                outbox_path=str(outbox_path),
                message_id=message_id,
            )

        if not (
            self.settings.self_improvement_smtp_host
            and self.settings.self_improvement_email_to
            and self.settings.self_improvement_email_from
        ):
            return EmailDeliveryResult(
                status="queued",
                detail="SMTP ist nicht vollstaendig konfiguriert. Die Nachricht liegt zur spaeteren Zustellung im Outbox-Ordner.",
                outbox_path=str(outbox_path),
                message_id=message_id,
            )

        try:
            await asyncio.to_thread(self._send_via_smtp, subject, body)
        except Exception as exc:  # noqa: BLE001 - delivery errors must become operator-visible state, not crashes
            return EmailDeliveryResult(
                status="failed",
                detail=f"SMTP-Zustellung fehlgeschlagen: {type(exc).__name__}: {exc}",
                outbox_path=str(outbox_path),
                message_id=message_id,
            )

        return EmailDeliveryResult(
            status="sent",
            detail="Nachricht wurde per SMTP versendet und zusaetzlich im Outbox-Ordner abgelegt.",
            outbox_path=str(outbox_path),
            message_id=message_id,
        )

    def _send_via_smtp(self, subject: str, body: str) -> None:
        """Synchronous SMTP send used via asyncio.to_thread to keep the service responsive."""

        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self.settings.self_improvement_email_from
        message["To"] = self.settings.self_improvement_email_to
        if self.settings.self_improvement_email_reply_to:
            message["Reply-To"] = self.settings.self_improvement_email_reply_to
        message.set_content(body)

        with smtplib.SMTP(
            host=self.settings.self_improvement_smtp_host,
            port=self.settings.self_improvement_smtp_port,
            timeout=self.settings.self_improvement_email_timeout_seconds,
        ) as client:
            if self.settings.self_improvement_smtp_use_starttls:
                client.starttls()
            if self.settings.self_improvement_smtp_username:
                client.login(
                    self.settings.self_improvement_smtp_username,
                    self.settings.self_improvement_smtp_password,
                )
            client.send_message(message)
