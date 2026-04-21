"""
Purpose: Validate the dedicated rollback worker for deterministic git reverts and self-update watchdog behavior.
Input/Output: Tests drive the worker against temporary git repositories and persisted watchdog state files.
Important invariants:
  - Rollback tasks stay deterministic via `git revert`.
  - The self-update watchdog only reports success after it observes a target SHA change and stable health.
How to debug: If these tests fail, inspect services/rollback_worker/app.py together with
services/shared/agentic_lab/self_update_watchdog.py.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

from services.shared.agentic_lab import config as config_module
from services.shared.agentic_lab.config import Settings
from services.shared.agentic_lab.schemas import WorkerRequest
from services.shared.agentic_lab.self_update_watchdog import (
    SelfUpdateWatchdogState,
    SelfUpdateWatchdogStatus,
    read_watchdog_state,
    write_watchdog_state,
)


def _run_git(repo_path: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo_path, check=True, text=True, capture_output=True)


def _git_output(repo_path: Path, *args: str) -> str:
    completed = subprocess.run(["git", *args], cwd=repo_path, check=True, text=True, capture_output=True)
    return completed.stdout.strip()


def _rollback_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("RUNTIME_HOME_DIR", str(tmp_path / "runtime-home"))
    monkeypatch.setenv("SELF_UPDATE_WATCHDOG_POLL_SECONDS", "0.01")
    monkeypatch.setenv("SELF_UPDATE_WATCHDOG_TIMEOUT_SECONDS", "0.08")
    return Settings()


def _create_repo(tmp_path: Path) -> Path:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / "worker_target.py").write_text(
        "def target_function():\n"
        "    return 'old-value'\n",
        encoding="utf-8",
    )
    _run_git(repo_path, "init", "-b", "main")
    _run_git(repo_path, "config", "user.email", "test@example.com")
    _run_git(repo_path, "config", "user.name", "Test User")
    _run_git(repo_path, "add", "worker_target.py")
    _run_git(repo_path, "commit", "-m", "initial")
    return repo_path


def _load_rollback_module() -> object:
    config_module.get_settings.cache_clear()
    module_name = "services.rollback_worker.app"
    if module_name in sys.modules:
        return importlib.reload(sys.modules[module_name])
    return importlib.import_module(module_name)


def test_rollback_worker_prepares_a_deterministic_git_revert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _rollback_settings(tmp_path, monkeypatch)
    rollback_app = _load_rollback_module()
    repo_path = _create_repo(tmp_path)
    target_file = repo_path / "worker_target.py"
    target_file.write_text(
        "def target_function():\n"
        "    return 'newer-value'\n",
        encoding="utf-8",
    )
    _run_git(repo_path, "add", "worker_target.py")
    _run_git(repo_path, "commit", "-m", "introduce regression")
    reverted_commit = _git_output(repo_path, "rev-parse", "HEAD")
    _run_git(repo_path, "checkout", "-b", "feature/test-rollback-worker")

    monkeypatch.setattr(rollback_app, "settings", settings)

    request = WorkerRequest(
        task_id="task-rollback-worker",
        goal="Revertiere den fehlerhaften Commit ueber den neuen Rollback-Worker.",
        repository="Feberdin/local-multi-agent-company",
        local_repo_path=str(repo_path),
        base_branch="main",
        branch_name="feature/test-rollback-worker",
        metadata={"rollback_commit_sha": reverted_commit},
        prior_results={},
    )

    response = rollback_app._run_git_revert_backend(  # pyright: ignore[reportPrivateUsage]
        request,
        repo_path,
        "feature/test-rollback-worker",
        reverted_commit,
    )

    assert response.success is True
    assert response.worker == "rollback"
    assert response.outputs["backend"] == "git_revert"
    assert response.outputs["rollback_commit_sha"] == reverted_commit
    assert response.outputs["changed_files"] == ["worker_target.py"]
    assert target_file.read_text(encoding="utf-8") == (
        "def target_function():\n"
        "    return 'old-value'\n"
    )


@pytest.mark.asyncio
async def test_self_update_watchdog_marks_healthy_after_sha_change_and_health_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _rollback_settings(tmp_path, monkeypatch)
    rollback_app = _load_rollback_module()
    monkeypatch.setattr(rollback_app, "settings", settings)

    observed_heads = iter(["aaa111", "bbb222", "bbb222"])
    monkeypatch.setattr(rollback_app, "_read_remote_head", lambda request: next(observed_heads, "bbb222"))

    async def fake_health(_url: str) -> bool:
        return True

    rollback_called = {"value": False}

    async def fake_rollback(_request, _state) -> None:
        rollback_called["value"] = True

    monkeypatch.setattr(rollback_app, "_healthcheck_ok", fake_health)
    monkeypatch.setattr(rollback_app, "_run_host_rollback", fake_rollback)

    request = rollback_app.SelfUpdateWatchdogStartRequest(
        task_id="watch-healthy",
        branch_name="feature/self-update",
        target_commit_sha="bbb222",
        ssh_user="root",
        ssh_host="tower.local",
        ssh_port=22,
        ssh_key_file="",
        project_dir="/mnt/user/appdata/feberdin-agent-team/repo",
        compose_file="docker-compose.yml",
        health_url="http://tower.local:18080/health",
        poll_seconds=0.01,
        timeout_seconds=0.08,
    )
    state = SelfUpdateWatchdogState(
        task_id=request.task_id,
        status=SelfUpdateWatchdogStatus.ARMED,
        branch_name=request.branch_name,
        health_url=request.health_url,
        project_dir=request.project_dir,
        compose_file=request.compose_file,
        ssh_host=request.ssh_host,
        ssh_user=request.ssh_user,
        ssh_port=request.ssh_port,
        previous_sha="aaa111",
    )
    write_watchdog_state(state, settings)

    await rollback_app._monitor_self_update(request, state)  # pyright: ignore[reportPrivateUsage]

    persisted = read_watchdog_state(request.task_id, settings)
    assert persisted is not None
    assert persisted.status == SelfUpdateWatchdogStatus.HEALTHY
    assert persisted.observed_target_change is True
    assert persisted.current_sha == "bbb222"
    assert rollback_called["value"] is False


@pytest.mark.asyncio
async def test_self_update_watchdog_triggers_host_rollback_after_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _rollback_settings(tmp_path, monkeypatch)
    rollback_app = _load_rollback_module()
    monkeypatch.setattr(rollback_app, "settings", settings)
    monkeypatch.setattr(rollback_app, "_read_remote_head", lambda request: "aaa111")

    async def fake_health(_url: str) -> bool:
        return False

    rollback_called = {"value": False}

    async def fake_rollback(_request, state) -> None:
        rollback_called["value"] = True
        state.status = SelfUpdateWatchdogStatus.ROLLED_BACK
        state.last_error = None
        write_watchdog_state(state, settings)

    monkeypatch.setattr(rollback_app, "_healthcheck_ok", fake_health)
    monkeypatch.setattr(rollback_app, "_run_host_rollback", fake_rollback)

    request = rollback_app.SelfUpdateWatchdogStartRequest(
        task_id="watch-timeout",
        branch_name="feature/self-update",
        target_commit_sha="bbb222",
        ssh_user="root",
        ssh_host="tower.local",
        ssh_port=22,
        ssh_key_file="",
        project_dir="/mnt/user/appdata/feberdin-agent-team/repo",
        compose_file="docker-compose.yml",
        health_url="http://tower.local:18080/health",
        poll_seconds=0.01,
        timeout_seconds=0.05,
    )
    state = SelfUpdateWatchdogState(
        task_id=request.task_id,
        status=SelfUpdateWatchdogStatus.ARMED,
        branch_name=request.branch_name,
        health_url=request.health_url,
        project_dir=request.project_dir,
        compose_file=request.compose_file,
        ssh_host=request.ssh_host,
        ssh_user=request.ssh_user,
        ssh_port=request.ssh_port,
        previous_sha="aaa111",
    )

    await rollback_app._monitor_self_update(request, state)  # pyright: ignore[reportPrivateUsage]

    persisted = read_watchdog_state(request.task_id, settings)
    assert persisted is not None
    assert persisted.status == SelfUpdateWatchdogStatus.ROLLED_BACK
    assert rollback_called["value"] is True


@pytest.mark.asyncio
async def test_self_update_watchdog_does_not_accept_the_wrong_new_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _rollback_settings(tmp_path, monkeypatch)
    rollback_app = _load_rollback_module()
    monkeypatch.setattr(rollback_app, "settings", settings)

    observed_heads = iter(["aaa111", "ccc333", "ccc333", "ccc333"])
    monkeypatch.setattr(rollback_app, "_read_remote_head", lambda request: next(observed_heads, "ccc333"))

    async def fake_health(_url: str) -> bool:
        return True

    rollback_called = {"value": False}

    async def fake_rollback(_request, state) -> None:
        rollback_called["value"] = True
        state.status = SelfUpdateWatchdogStatus.ROLLED_BACK
        state.last_error = None
        write_watchdog_state(state, settings)

    monkeypatch.setattr(rollback_app, "_healthcheck_ok", fake_health)
    monkeypatch.setattr(rollback_app, "_run_host_rollback", fake_rollback)

    request = rollback_app.SelfUpdateWatchdogStartRequest(
        task_id="watch-wrong-commit",
        branch_name="feature/self-update",
        target_commit_sha="bbb222",
        ssh_user="root",
        ssh_host="tower.local",
        ssh_port=22,
        ssh_key_file="",
        project_dir="/mnt/user/appdata/feberdin-agent-team/repo",
        compose_file="docker-compose.yml",
        health_url="http://tower.local:18080/health",
        poll_seconds=0.01,
        timeout_seconds=0.05,
    )
    state = SelfUpdateWatchdogState(
        task_id=request.task_id,
        status=SelfUpdateWatchdogStatus.ARMED,
        branch_name=request.branch_name,
        target_commit_sha=request.target_commit_sha,
        health_url=request.health_url,
        project_dir=request.project_dir,
        compose_file=request.compose_file,
        ssh_host=request.ssh_host,
        ssh_user=request.ssh_user,
        ssh_port=request.ssh_port,
        previous_sha="aaa111",
    )

    await rollback_app._monitor_self_update(request, state)  # pyright: ignore[reportPrivateUsage]

    persisted = read_watchdog_state(request.task_id, settings)
    assert persisted is not None
    assert persisted.status == SelfUpdateWatchdogStatus.ROLLED_BACK
    assert persisted.current_sha == "ccc333"
    assert persisted.observed_target_change is False
    assert rollback_called["value"] is True


@pytest.mark.asyncio
async def test_rollback_worker_can_restore_the_self_host_stack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SELF_HOST_SSH_HOST", "tower.local")
    monkeypatch.setenv("SELF_HOST_SSH_USER", "root")
    monkeypatch.setenv("SELF_HOST_SSH_PORT", "22")
    monkeypatch.setenv("SELF_HOST_PROJECT_DIR", "/mnt/user/appdata/feberdin-agent-team/repo")
    monkeypatch.setenv("SELF_HOST_COMPOSE_FILE", "docker-compose.yml")
    monkeypatch.setenv("SELF_HOST_HEALTH_URL", "http://tower.local:18088/health")
    settings = _rollback_settings(tmp_path, monkeypatch)
    rollback_app = _load_rollback_module()
    monkeypatch.setattr(rollback_app, "settings", settings)

    async def fake_capture(**kwargs):  # type: ignore[no-untyped-def]
        assert kwargs["previous_sha"] == "stable123"
        return str(tmp_path / "rollback-debug-report.json")

    def fake_run_command(command, timeout: int = 300):  # type: ignore[no-untyped-def]
        assert "rollback-self-update.sh" in " ".join(str(part) for part in command)
        return subprocess.CompletedProcess(command, 0, stdout="rollback ok", stderr="")

    monkeypatch.setattr(rollback_app, "_capture_self_host_debug_report", fake_capture)
    monkeypatch.setattr(rollback_app, "run_command", fake_run_command)
    monkeypatch.setattr(rollback_app, "write_report", lambda *args, **kwargs: tmp_path / "rollback-host-report.json")

    request = WorkerRequest(
        task_id="task-self-host-rollback",
        goal="Setze den Self-Host auf den letzten stabilen Commit zurueck.",
        repository="Feberdin/local-multi-agent-company",
        local_repo_path=str(tmp_path / "workspace" / "local-multi-agent-company"),
        base_branch="main",
        metadata={"rollback_host_previous_sha": "stable123", "deployment_target_commit_sha": "bad999"},
        prior_results={},
    )

    response = await rollback_app.run(request)

    assert response.success is True
    assert response.outputs["backend"] == "self_host_restore"
    assert response.outputs["rollback_host_previous_sha"] == "stable123"
    assert response.outputs["debug_report_path"].endswith("rollback-debug-report.json")
