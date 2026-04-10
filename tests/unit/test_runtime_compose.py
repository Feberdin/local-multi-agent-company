"""
Purpose: Catch Docker Compose regressions around mounts, ports, and `.env` validation before runtime.
Input/Output: Reads the checked-in Compose and example env files and validates the expected operational contract.
Important invariants: All runtime services need the shared persistent mounts, and the host defaults must avoid the old 8080/8088 conflicts.
How to debug: If a test fails, inspect docker-compose.yml, docker-compose.override.yml,
and .env.example together before touching the service code.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from services.shared.agentic_lab.config import validate_runtime_env_file

ROOT_DIR = Path(__file__).resolve().parents[2]
REQUIRED_RUNTIME_SERVICES = (
    "orchestrator",
    "requirements-worker",
    "research-worker",
    "architecture-worker",
    "coding-worker",
    "reviewer-worker",
    "test-worker",
    "github-worker",
    "deploy-worker",
    "qa-worker",
    "security-worker",
    "validation-worker",
    "documentation-worker",
    "memory-worker",
    "data-worker",
    "ux-worker",
    "cost-worker",
    "human-resources-worker",
    "web-ui",
)
REQUIRED_TARGETS = {"/data", "/reports", "/workspace", "/staging-stacks", "/app"}


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _volume_targets(service_definition: dict) -> set[str]:
    targets: set[str] = set()
    for volume in service_definition.get("volumes", []):
        if isinstance(volume, str):
            match = re.search(r":(?P<target>/[^:]+?)(?::[^:]+)?$", volume)
            if match:
                targets.add(match.group("target"))
            continue
        if isinstance(volume, dict) and "target" in volume:
            targets.add(volume["target"])
    return targets


def _parse_env_example(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def test_runtime_services_have_all_required_mount_targets() -> None:
    compose_data = _load_yaml(ROOT_DIR / "docker-compose.yml")
    services = compose_data["services"]

    for service_name in REQUIRED_RUNTIME_SERVICES:
        targets = _volume_targets(services[service_name])
        assert REQUIRED_TARGETS.issubset(targets), f"{service_name} is missing one of {sorted(REQUIRED_TARGETS - targets)}"


def test_override_does_not_override_ports_or_volumes() -> None:
    override_data = _load_yaml(ROOT_DIR / "docker-compose.override.yml")

    for service_name, service_definition in override_data["services"].items():
        assert "volumes" not in service_definition, f"{service_name} should not override runtime volumes in docker-compose.override.yml"
        assert "ports" not in service_definition, f"{service_name} should not override published ports in docker-compose.override.yml"


def test_env_example_uses_conflict_aware_ports_and_unique_keys() -> None:
    env_text = (ROOT_DIR / ".env.example").read_text(encoding="utf-8").splitlines()
    keys = [line.split("=", 1)[0].strip() for line in env_text if line.strip() and not line.strip().startswith("#") and "=" in line]

    assert len(keys) == len(set(keys)), ".env.example contains duplicate keys"

    env_values = _parse_env_example(ROOT_DIR / ".env.example")
    assert env_values["ORCHESTRATOR_PORT"] == "18080"
    assert env_values["WEB_UI_PORT"] == "18088"


def test_runtime_env_validation_rejects_duplicate_keys(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("ORCHESTRATOR_PORT=18080\nWEB_UI_PORT=18088\nORCHESTRATOR_PORT=19090\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Duplicate keys in .env detected"):
        validate_runtime_env_file(env_file)
