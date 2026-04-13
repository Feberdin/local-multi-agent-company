"""
Purpose: Persist durable self-update watchdog state so rollback monitoring survives orchestrator restarts.
Input/Output: The rollback worker writes JSON snapshots per task, and other services read them to decide
              whether a self-update finished, rolled back, or still needs observation.
Important invariants:
  - One watchdog state file exists per task id.
  - State writes are atomic enough for operator dashboards: write full JSON, then replace the previous view.
  - The state store lives under DATA_DIR so it survives container recreation on Unraid.
How to debug:
  - Inspect DATA_DIR/self-update-watchdogs/<task-id>.json to see the last known heartbeat, status, and error.
  - If a cycle is stuck after a restart, compare the persisted status here with the cycle metadata in the DB.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

from services.shared.agentic_lab.config import Settings, get_settings


class SelfUpdateWatchdogStatus(StrEnum):
    ARMED = "armed"
    MONITORING = "monitoring"
    HEALTHY = "healthy"
    ROLLBACK_RUNNING = "rollback_running"
    ROLLED_BACK = "rolled_back"
    ROLLBACK_FAILED = "rollback_failed"
    DISPATCH_FAILED = "dispatch_failed"
    TIMED_OUT = "timed_out"


def _utc_now() -> datetime:
    return datetime.now(UTC)


class SelfUpdateWatchdogState(BaseModel):
    task_id: str
    status: SelfUpdateWatchdogStatus
    branch_name: str
    target_commit_sha: str | None = None
    health_url: str
    project_dir: str
    compose_file: str
    ssh_host: str
    ssh_user: str
    ssh_port: int
    previous_sha: str | None = None
    current_sha: str | None = None
    observed_target_change: bool = False
    heartbeat_count: int = 0
    last_error: str | None = None
    notes: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)
    last_heartbeat_at: datetime = Field(default_factory=_utc_now)


def watchdog_state_dir(settings: Settings | None = None) -> Path:
    """Return the persistent directory used for self-update watchdog snapshots."""

    active_settings = settings or get_settings()
    return active_settings.data_dir / "self-update-watchdogs"


def watchdog_state_path(task_id: str, settings: Settings | None = None) -> Path:
    """Return the JSON file path for one watchdog task id."""

    return watchdog_state_dir(settings) / f"{task_id}.json"


def write_watchdog_state(state: SelfUpdateWatchdogState, settings: Settings | None = None) -> Path:
    """Persist one watchdog state snapshot to disk for later recovery and operator inspection."""

    target_path = watchdog_state_path(state.task_id, settings)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    payload = state.model_dump(mode="json")
    serialized = json.dumps(payload, indent=2, ensure_ascii=False)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=target_path.parent,
        prefix=f"{target_path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(serialized)
        temp_path = Path(handle.name)
    os.replace(temp_path, target_path)
    return target_path


def read_watchdog_state(task_id: str, settings: Settings | None = None) -> SelfUpdateWatchdogState | None:
    """Load one persisted watchdog snapshot and return None when it does not exist yet."""

    target_path = watchdog_state_path(task_id, settings)
    if not target_path.exists():
        return None
    try:
        raw = json.loads(target_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return SelfUpdateWatchdogState.model_validate(raw)
