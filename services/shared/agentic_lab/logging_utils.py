"""
Purpose: Consistent logging setup with minimal sensitive-data masking for all services.
Input/Output: Services call `configure_logging` once and receive a logger adapter with structured context support.
Important invariants: Secrets must not appear in log lines, and every message should be attributable to a service and optionally a task.
How to debug: If logs are missing context fields, inspect the formatter and logger adapter behavior here.
"""

from __future__ import annotations

import logging
from collections.abc import MutableMapping
from logging import LoggerAdapter
from typing import Any

MASKED_MARKERS = ("token", "secret", "password", "key")
_DEFAULT_TASK_ID = "-"


class SensitiveDataFilter(logging.Filter):
    """Mask obvious sensitive fragments before they hit stdout or Docker logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        lowered = message.lower()
        if any(marker in lowered for marker in MASKED_MARKERS):
            record.msg = self._mask_message(message)
            record.args = ()
        return True

    @staticmethod
    def _mask_message(message: str) -> str:
        masked_message = message
        for marker in MASKED_MARKERS:
            masked_message = masked_message.replace(marker, f"{marker[:1]}***")
            masked_message = masked_message.replace(marker.upper(), f"{marker[:1].upper()}***")
        return masked_message


class LoggingContextDefaultsFilter(logging.Filter):
    """Backfill required formatter fields for handlers that see third-party loggers."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "service"):
            record.service = record.name
        if not hasattr(record, "task_id"):
            record.task_id = _DEFAULT_TASK_ID
        return True


class TaskLoggerAdapter(LoggerAdapter):
    """Attach service and task context to every log line without repeating boilerplate."""

    def process(self, msg: str, kwargs: MutableMapping[str, Any]) -> tuple[str, MutableMapping[str, Any]]:
        extra = kwargs.setdefault("extra", {})
        adapter_extra = self.extra or {}
        extra.setdefault("service", adapter_extra.get("service", "unknown"))
        extra.setdefault("task_id", adapter_extra.get("task_id", _DEFAULT_TASK_ID))
        return msg, kwargs


def configure_logging(service_name: str, log_level: str = "INFO") -> TaskLoggerAdapter:
    """Set up process-wide logging and return a context-aware logger adapter."""

    root_logger = logging.getLogger()
    if not root_logger.handlers:
        root_logger.addHandler(logging.StreamHandler())

    for handler in root_logger.handlers:
        if not any(isinstance(item, LoggingContextDefaultsFilter) for item in handler.filters):
            handler.addFilter(LoggingContextDefaultsFilter())
        if not any(isinstance(item, SensitiveDataFilter) for item in handler.filters):
            handler.addFilter(SensitiveDataFilter())
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s %(levelname)s [service=%(service)s task=%(task_id)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )

    root_logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    return TaskLoggerAdapter(logging.getLogger(service_name), {"service": service_name, "task_id": _DEFAULT_TASK_ID})
