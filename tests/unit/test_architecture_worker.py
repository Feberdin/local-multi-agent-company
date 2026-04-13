"""
Purpose: Verify that architecture outputs keep touched_areas grounded in real repository files.
Input/Output: Tests call the normalization helper with a temporary repo and inspect the resulting file list.
Important invariants: Non-existent touched_areas must not propagate to downstream workers when better real files exist.
How to debug: If this fails, inspect services/architecture_worker/app.py and the research candidate_files used for recovery.
"""

from __future__ import annotations

import importlib
from pathlib import Path

from services.shared.agentic_lab.config import get_settings


def _load_architecture_module(tmp_path: Path, monkeypatch) -> object:
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("STAGING_STACK_ROOT", str(tmp_path / "staging-stacks"))
    get_settings.cache_clear()
    module = importlib.import_module("services.architecture_worker.app")
    return importlib.reload(module)


def test_normalize_architecture_outputs_filters_missing_paths_and_uses_research_candidates(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app_module = _load_architecture_module(tmp_path, monkeypatch)
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    existing_file = repo_path / "services" / "coding_worker" / "app.py"
    existing_file.parent.mkdir(parents=True, exist_ok=True)
    existing_file.write_text("print('ok')\n", encoding="utf-8")
    fallback_file = repo_path / "services" / "shared" / "agentic_lab" / "repo_tools.py"
    fallback_file.parent.mkdir(parents=True, exist_ok=True)
    fallback_file.write_text("print('fallback')\n", encoding="utf-8")

    outputs = {
        "summary": "Generic architecture plan.",
        "touched_areas": [
            "services/coding_worker/app.py",
            "services/coding_worker/task_dispatcher.py",
            "services/coding_worker/result_handler.py",
        ],
    }
    research = {
        "candidate_files": [
            "services/shared/agentic_lab/repo_tools.py",
            "README.md",
        ]
    }

    normalized = app_module._normalize_architecture_outputs(outputs, repo_path, research)  # pyright: ignore[reportPrivateUsage]

    assert normalized["touched_areas"] == [
        "services/coding_worker/app.py",
        "services/shared/agentic_lab/repo_tools.py",
    ]
