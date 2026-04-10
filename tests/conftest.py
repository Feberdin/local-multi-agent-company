"""
Purpose: Shared pytest fixtures for temporary database configuration.
Input/Output: Tests call the fixture to run against an isolated SQLite file per test session.
Important invariants: Tests never use the operator's real runtime database.
How to debug: If tests leak data across runs, inspect the configured temporary DB path here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from services.shared.agentic_lab.config import get_settings
from services.shared.agentic_lab.db import Base, configure_database, get_session_factory


@pytest.fixture(autouse=True)
def isolated_runtime_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Force all tests to use temporary runtime paths instead of operator-specific .env locations."""

    data_dir = tmp_path / "data"
    reports_dir = tmp_path / "reports"
    workspace_dir = tmp_path / "workspace"
    staging_dir = tmp_path / "staging"
    db_path = data_dir / "orchestrator.db"

    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("REPORTS_DIR", str(reports_dir))
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace_dir))
    monkeypatch.setenv("STAGING_STACK_ROOT", str(staging_dir))
    monkeypatch.setenv("ORCHESTRATOR_DB_PATH", str(db_path))

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def isolated_session_factory(tmp_path: Path):
    database_path = tmp_path / "test.db"
    engine = configure_database(f"sqlite:///{database_path}")
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield get_session_factory()
