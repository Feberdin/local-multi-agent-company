"""
Purpose: Validate that the deploy worker uses exact target commits for self-updates instead of moving branch heads.
Input/Output: Tests drive the self-update branch of the deploy worker with mocked watchdog and shell execution.
Important invariants:
  - Self-updates must fail fast when no verified commit SHA is available.
  - The rollback watchdog and remote shell wrapper must receive the exact target commit.
How to debug: If this fails, inspect `services/deploy_worker/app.py` and `scripts/unraid/self-update.sh`.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.shared.agentic_lab import config as config_module
from services.shared.agentic_lab.config import Settings
from services.shared.agentic_lab.schemas import WorkerRequest
from services.shared.agentic_lab.self_update_watchdog import SelfUpdateWatchdogState, SelfUpdateWatchdogStatus


def _deploy_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("RUNTIME_HOME_DIR", str(tmp_path / "runtime-home"))
    monkeypatch.setenv("SELF_HOST_SSH_HOST", "tower.local")
    monkeypatch.setenv("SELF_HOST_SSH_USER", "root")
    monkeypatch.setenv("SELF_HOST_SSH_PORT", "22")
    monkeypatch.setenv("SELF_HOST_PROJECT_DIR", "/mnt/user/appdata/feberdin-agent-team/repo")
    monkeypatch.setenv("SELF_HOST_COMPOSE_FILE", "docker-compose.yml")
    monkeypatch.setenv("SELF_HOST_HEALTH_URL", "http://tower.local:18080/health")
    monkeypatch.setenv("ROLLBACK_WORKER_URL", "http://rollback-worker:8107")
    return Settings()


def _load_deploy_module() -> object:
    config_module.get_settings.cache_clear()
    module_name = "services.deploy_worker.app"
    if module_name in sys.modules:
        return importlib.reload(sys.modules[module_name])
    return importlib.import_module(module_name)


@pytest.mark.asyncio
async def test_self_update_requires_a_verified_target_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _deploy_settings(tmp_path, monkeypatch)
    deploy_app = _load_deploy_module()
    monkeypatch.setattr(deploy_app, "settings", settings)

    request = WorkerRequest(
        task_id="task-self-update-missing-commit",
        goal="Deploy the stack to Unraid.",
        repository="Feberdin/local-multi-agent-company",
        local_repo_path="/workspace/local-multi-agent-company",
        base_branch="main",
        branch_name="feature/self-update",
        metadata={"deployment_target": "self"},
        prior_results={},
    )

    response = await deploy_app._run_self_update(request, SimpleNamespace(info=lambda *args, **kwargs: None), "feature/self-update")  # pyright: ignore[reportPrivateUsage]

    assert response.success is False
    assert "commit" in response.summary.lower()


@pytest.mark.asyncio
async def test_self_update_passes_exact_target_commit_to_watchdog_and_shell_wrapper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _deploy_settings(tmp_path, monkeypatch)
    deploy_app = _load_deploy_module()
    monkeypatch.setattr(deploy_app, "settings", settings)

    captured: dict[str, object] = {}

    async def fake_start_watchdog(task_id: str, branch_name: str, target_commit_sha: str):
        captured["watchdog"] = {
            "task_id": task_id,
            "branch_name": branch_name,
            "target_commit_sha": target_commit_sha,
        }
        return SelfUpdateWatchdogState(
            task_id=task_id,
            status=SelfUpdateWatchdogStatus.ARMED,
            branch_name=branch_name,
            target_commit_sha=target_commit_sha,
            health_url=settings.self_host_health_url,
            project_dir=settings.self_host_project_dir,
            compose_file=settings.self_host_compose_file,
            ssh_host=settings.self_host_ssh_host,
            ssh_user=settings.self_host_ssh_user,
            ssh_port=settings.self_host_ssh_port,
            previous_sha="aaa111aaa111",
        )

    def fake_run_command(command, timeout: int):  # noqa: ANN001
        captured["command"] = {"command": command, "timeout": timeout}
        return SimpleNamespace(stdout="ok", stderr="")

    monkeypatch.setattr(deploy_app, "_start_self_update_watchdog", fake_start_watchdog)
    monkeypatch.setattr(deploy_app, "run_command", fake_run_command)

    request = WorkerRequest(
        task_id="task-self-update-pinned",
        goal="Deploy the stack to Unraid.",
        repository="Feberdin/local-multi-agent-company",
        local_repo_path="/workspace/local-multi-agent-company",
        base_branch="main",
        branch_name="feature/self-update",
        metadata={"deployment_target": "self"},
        prior_results={"github": {"outputs": {"commit_sha": "b7458d9ae30c"}}},
    )

    response = await deploy_app._run_self_update(request, SimpleNamespace(info=lambda *args, **kwargs: None), "feature/self-update")  # pyright: ignore[reportPrivateUsage]

    assert response.success is True
    assert response.outputs["target_commit_sha"] == "b7458d9ae30c"
    assert captured["watchdog"] == {
        "task_id": "task-self-update-pinned",
        "branch_name": "feature/self-update",
        "target_commit_sha": "b7458d9ae30c",
    }
    command = captured["command"]["command"]
    assert command[0:2] == ["/bin/sh", "/app/scripts/unraid/self-update.sh"]
    assert command[5] == "b7458d9ae30c"
