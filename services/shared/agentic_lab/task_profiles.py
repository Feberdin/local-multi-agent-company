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


def _normalize_text(value: str) -> str:
    """Lowercase and de-accent text so German and English task wording match the same profile rules."""

    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    compact = re.sub(r"\s+", " ", normalized.lower())
    return f" {compact.strip()} "
