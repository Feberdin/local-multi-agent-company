"""
Purpose: Detect tiny, low-risk task shapes that deserve a faster and more deterministic workflow path.
Input/Output: Consumes the original task goal plus optional metadata and returns a small profile dict or None.
Important invariants: Profiles stay intentionally narrow so we only skip heavy stages when the requested change is truly tiny.
How to debug: If a task unexpectedly took the slow path, inspect the normalized goal text and the inferred task_profile in task metadata.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from typing import Any

README_SMILEY_PROFILE_NAME = "readme_prefix_smiley_fix"
README_SMILEY_TARGET_FILES: tuple[str, ...] = ("README.md",)
README_SMILEY_CODING_STRATEGY = "prepend_ascii_smiley_to_readme_first_line"
WORKER_STAGE_TIMEOUT_PROFILE_NAME = "worker_stage_timeout_config_fix"
WORKER_STAGE_TIMEOUT_TARGET_FILES: tuple[str, ...] = (
    "services/shared/agentic_lab/config.py",
    "README.md",
    "docs/configuration.md",
    "docs/troubleshooting.md",
)
WORKER_STAGE_TIMEOUT_CODING_STRATEGY = "set_worker_stage_timeout_seconds"


def infer_task_profile(goal: str, metadata: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    """
    Return one narrow execution profile when the task clearly matches a tiny, deterministic fix shape.

    Example:
      Input goal:
        "Fuege am Anfang der Readme einen Smiley ein."
      Output profile:
        {
          "name": "readme_prefix_smiley_fix",
          "target_files": ["README.md"],
          "skip_research": True,
          "skip_architecture": True,
          "deterministic_coding_strategy": "prepend_ascii_smiley_to_readme_first_line",
        }
    """

    normalized_goal = _normalize_text(goal)
    metadata_map = metadata or {}

    probe_mode = str(metadata_map.get("probe_mode") or "").strip().lower()
    if probe_mode == "micro_fix":
        return _readme_smiley_profile(
            "Der explizite Probe-Modus `micro_fix` fordert bereits den minimalen README-Live-Patch an."
        )

    if _looks_like_readme_smiley_fix(normalized_goal):
        return _readme_smiley_profile(
            "Das Ziel beschreibt einen sehr kleinen README-Einzeilenfix mit Smiley-Praefix."
        )

    worker_stage_timeout_target = _extract_worker_stage_timeout_target_seconds(normalized_goal)
    if worker_stage_timeout_target is not None and _looks_like_worker_stage_timeout_fix(
        normalized_goal,
        metadata_map,
    ):
        return _worker_stage_timeout_profile(
            worker_stage_timeout_target,
            "Das Ziel nennt explizit WORKER_STAGE_TIMEOUT_SECONDS und kann deterministisch ueber die echte Config-Datei aufgeloest werden.",
        )

    return None


def get_task_profile(metadata: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return the stored task profile only when it has the expected dictionary shape."""

    raw_profile = (metadata or {}).get("task_profile")
    if isinstance(raw_profile, dict) and raw_profile:
        return raw_profile
    return None


def has_profile_name(metadata: Mapping[str, Any] | None, profile_name: str) -> bool:
    """Check one stored profile name without forcing every caller to handle dict edge cases."""

    profile = get_task_profile(metadata)
    return bool(profile and profile.get("name") == profile_name)


def is_readme_smiley_profile(metadata: Mapping[str, Any] | None) -> bool:
    """Shortcut for the deterministic README smiley fast path."""

    return has_profile_name(metadata, README_SMILEY_PROFILE_NAME)


def is_worker_stage_timeout_profile(metadata: Mapping[str, Any] | None) -> bool:
    """Shortcut for the deterministic worker-stage-timeout config fast path."""

    return has_profile_name(metadata, WORKER_STAGE_TIMEOUT_PROFILE_NAME)


def profile_flag(metadata: Mapping[str, Any] | None, flag_name: str) -> bool:
    """Read one boolean flag from the stored task profile without leaking dict edge cases everywhere."""

    profile = get_task_profile(metadata)
    return bool(profile and profile.get(flag_name))


def profile_route_target(metadata: Mapping[str, Any] | None, stage_name: str) -> str | None:
    """Return one profile-specific route override like `route_after_coding` when present."""

    profile = get_task_profile(metadata)
    if not profile:
        return None
    raw_target = profile.get(f"route_after_{stage_name}")
    if isinstance(raw_target, str) and raw_target.strip():
        return raw_target.strip()
    return None


def profile_target_files(metadata: Mapping[str, Any] | None) -> list[str]:
    """Expose the stored target file list in one normalized place for workers and tests."""

    profile = get_task_profile(metadata)
    if not profile:
        return []
    raw_files = profile.get("target_files")
    if not isinstance(raw_files, list):
        return []
    return [item for item in raw_files if isinstance(item, str) and item.strip()]


def profile_target_timeout_seconds(metadata: Mapping[str, Any] | None) -> float | None:
    """Return the configured timeout target for deterministic timeout-fix profiles."""

    profile = get_task_profile(metadata)
    if not profile:
        return None
    raw_value = profile.get("target_timeout_seconds")
    if isinstance(raw_value, (int, float)):
        return float(raw_value)
    return None


def _readme_smiley_profile(reason: str) -> dict[str, Any]:
    """Return one stable metadata payload that every worker can consume consistently."""

    return {
        "name": README_SMILEY_PROFILE_NAME,
        "label": "README-Mini-Fix",
        "reason": reason,
        "target_files": list(README_SMILEY_TARGET_FILES),
        "skip_research": True,
        "skip_architecture": True,
        "skip_review": True,
        "skip_testing": True,
        "skip_security": True,
        "skip_documentation": True,
        "route_after_coding": "validation",
        "route_after_validation": "github",
        "route_after_github": "memory",
        "deterministic_requirements": True,
        "deterministic_research": True,
        "deterministic_architecture": True,
        "deterministic_validation": True,
        "deterministic_coding_strategy": README_SMILEY_CODING_STRATEGY,
    }


def _worker_stage_timeout_profile(target_timeout_seconds: float, reason: str) -> dict[str, Any]:
    """Return one stable fast-path profile for the known worker-stage-timeout config fix."""

    return {
        "name": WORKER_STAGE_TIMEOUT_PROFILE_NAME,
        "label": "Timeout-Config-Fix",
        "reason": reason,
        "target_timeout_seconds": float(target_timeout_seconds),
        "target_files": list(WORKER_STAGE_TIMEOUT_TARGET_FILES),
        "skip_research": True,
        "skip_architecture": True,
        "skip_review": True,
        "skip_testing": True,
        "skip_security": True,
        "skip_documentation": True,
        "route_after_coding": "validation",
        "route_after_validation": "github",
        "route_after_github": "memory",
        "deterministic_requirements": True,
        "deterministic_research": True,
        "deterministic_architecture": True,
        "deterministic_validation": True,
        "deterministic_coding_strategy": WORKER_STAGE_TIMEOUT_CODING_STRATEGY,
    }


def _looks_like_readme_smiley_fix(normalized_goal: str) -> bool:
    """Keep the heuristic strict so only obviously tiny README smiley tasks use the fast lane."""

    mentions_readme = "readme" in normalized_goal
    mentions_smiley = any(
        token in normalized_goal
        for token in ("smiley", "smilie", "emoji", "emoticon", " :) ", " :-)", " smile ")
    )
    mentions_first_line = any(
        token in normalized_goal
        for token in (
            "erste zeile",
            "anfang",
            "am anfang",
            "beginning",
            "first line",
            "prefix",
            "prepend",
        )
    )
    mentions_change = any(
        token in normalized_goal
        for token in ("add", "change", "update", "fix", "fuge", "fuege", "setze", "aendere")
    )
    return mentions_readme and mentions_smiley and mentions_first_line and mentions_change


def _looks_like_worker_stage_timeout_fix(normalized_goal: str, metadata: Mapping[str, Any]) -> bool:
    """Keep the timeout-config heuristic narrow so unrelated timeout chatter does not skip the full workflow."""

    mentions_variable = "worker_stage_timeout_seconds" in normalized_goal
    mentions_change = any(
        token in normalized_goal
        for token in ("change", "update", "set", "increase", "raise", "fix", "aendere", "erhoehe")
    )
    problem_class = str(metadata.get("problem_class") or "").strip().lower()
    timeout_context = "timeout" in normalized_goal or problem_class == "timeout"
    return mentions_variable and mentions_change and timeout_context


def _extract_worker_stage_timeout_target_seconds(normalized_goal: str) -> float | None:
    """Extract one concrete timeout target from goals like `Change WORKER_STAGE_TIMEOUT_SECONDS to 3600 ...`."""

    match = re.search(r"worker_stage_timeout_seconds[^0-9]{0,40}([0-9]{3,5}(?:\.[0-9]+)?)", normalized_goal)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _normalize_text(value: str) -> str:
    """Lowercase and de-accent text so German and English task wording match the same profile rules."""

    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    compact = re.sub(r"\s+", " ", normalized.lower())
    return f" {compact.strip()} "
