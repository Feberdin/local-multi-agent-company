"""
Purpose: Defensive checks for risky file changes, unsafe commands, and untrusted external inputs.
Input/Output: Workers call these helpers before applying changes, running commands, or trusting external text.
Important invariants: Infrastructure, secret, and destructive changes must be detected early and escalated to approval.
How to debug: If a safe change is blocked or a risky change slips through, tune the patterns and tests in this module.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import yaml  # type: ignore[import-untyped]

DEFAULT_DANGEROUS_TOKENS = (
    "rm -rf",
    "curl | sh",
    "wget | sh",
    "drop table",
    "truncate table",
    "chmod 777",
    "mkfs",
)
PROMPT_INJECTION_HINTS = (
    "ignore previous instructions",
    "system prompt",
    "developer message",
    "reveal secrets",
    "run this command",
    "override the safety policy",
)

INFRA_HINTS = ("docker-compose", "Dockerfile", ".github/workflows", "infra/", "terraform", "ansible")
SECRET_HINTS = (".env", "secret", "token", "private_key", ".pem", ".key")


@dataclass(slots=True)
class PolicySet:
    allowed_command_prefixes: list[str]
    denied_path_prefixes: list[str]
    review_required_path_prefixes: list[str]
    dangerous_tokens: list[str]


def load_policy_file(policy_path: Path) -> PolicySet:
    """Load guardrail policy from YAML and fall back to conservative defaults."""

    if not policy_path.exists():
        return PolicySet(
            allowed_command_prefixes=["pytest", "ruff", "mypy", "npm", "python", "python3"],
            denied_path_prefixes=["/etc", "/var/run", "/root"],
            review_required_path_prefixes=["infra/", ".github/workflows", "docker/", "scripts/unraid"],
            dangerous_tokens=list(DEFAULT_DANGEROUS_TOKENS),
        )

    raw = yaml.safe_load(policy_path.read_text(encoding="utf-8")) or {}
    return PolicySet(
        allowed_command_prefixes=raw.get("allowed_command_prefixes", ["pytest", "ruff", "mypy"]),
        denied_path_prefixes=raw.get("denied_path_prefixes", []),
        review_required_path_prefixes=raw.get("review_required_path_prefixes", []),
        dangerous_tokens=raw.get("dangerous_tokens", list(DEFAULT_DANGEROUS_TOKENS)),
    )


def sanitize_untrusted_text(text: str, max_length: int = 6000) -> str:
    """Treat external content as untrusted and keep it short enough for safe prompting."""

    trimmed = text.strip()
    if len(trimmed) > max_length:
        return trimmed[:max_length] + "\n[TRUNCATED_UNTRUSTED_CONTENT]"
    return trimmed


def ensure_repo_path_is_safe(repo_path: Path, workspace_root: Path) -> None:
    """Reject repository paths that try to escape the mounted workspace."""

    resolved_repo = repo_path.resolve()
    resolved_workspace = workspace_root.resolve()
    if resolved_workspace not in resolved_repo.parents and resolved_repo != resolved_workspace:
        raise ValueError(f"Repository path {resolved_repo} is outside workspace root {resolved_workspace}.")


def detect_risk_flags(changed_files: Iterable[str], diff_text: str) -> list[str]:
    """Return explicit risk flags that the reviewer or orchestrator can escalate."""

    flags: set[str] = set()
    lowered_diff = diff_text.lower()

    for file_path in changed_files:
        lowered_file = file_path.lower()
        if any(hint in lowered_file for hint in INFRA_HINTS):
            flags.add("infrastructure_change")
        if any(hint in lowered_file for hint in SECRET_HINTS):
            flags.add("secret_or_credentials_change")

    for token in DEFAULT_DANGEROUS_TOKENS:
        if token in lowered_diff:
            flags.add("destructive_or_high_impact_command")

    if "sudo " in lowered_diff or "systemctl " in lowered_diff:
        flags.add("host_level_command")
    if "delete " in lowered_diff and " from " in lowered_diff:
        flags.add("destructive_data_operation")

    return sorted(flags)


def command_is_allowed(command: str, policy: PolicySet) -> bool:
    """Allow only known-safe command prefixes for test and analysis execution."""

    executable = command.strip().split()[0] if command.strip() else ""
    return executable in policy.allowed_command_prefixes


def detect_prompt_injection_signals(text: str) -> list[str]:
    """Return heuristic prompt-injection signals found in untrusted external content."""

    lowered = text.lower()
    return [hint for hint in PROMPT_INJECTION_HINTS if hint in lowered]


def assess_source_quality(source_url: str) -> str:
    """Return a simple source quality label to help research outputs stay explicit."""

    lowered = source_url.lower()
    if lowered.startswith("https://docs.") or "github.com" in lowered or lowered.endswith(".md"):
        return "high"
    if "blog" in lowered or "medium.com" in lowered:
        return "medium"
    return "unknown"
