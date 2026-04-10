"""
Purpose: Verify that shared logging stays stable even when third-party libraries emit plain log records.
Input/Output: Tests configure the project logger, emit records from internal and external loggers, and inspect the added context fields.
Important invariants: Every record must expose `service` and `task_id` so the shared formatter never crashes on httpx/httpcore output.
How to debug: If these tests fail, inspect `services/shared/agentic_lab/logging_utils.py` and the installed record factory first.
"""

from __future__ import annotations

import logging

from services.shared.agentic_lab.logging_utils import ContextAwareFormatter, LoggingContextDefaultsFilter, configure_logging


def test_configure_logging_backfills_service_and_task_for_foreign_loggers() -> None:
    configure_logging("orchestrator", "INFO")

    record = logging.LogRecord(
        "httpx",
        logging.INFO,
        __file__,
        123,
        "foreign logger message",
        (),
        None,
    )
    assert LoggingContextDefaultsFilter().filter(record) is True

    assert record.service == "httpx"
    assert record.task_id == "-"


def test_context_aware_formatter_handles_foreign_logger_without_extra_fields() -> None:
    formatter = ContextAwareFormatter("%(levelname)s [service=%(service)s task=%(task_id)s] %(message)s")
    record = logging.LogRecord(
        "httpcore",
        logging.WARNING,
        __file__,
        99,
        "plain foreign logger message",
        (),
        None,
    )

    formatted = formatter.format(record)

    assert "service=httpcore" in formatted
    assert "task=-" in formatted
    assert "plain foreign logger message" in formatted
