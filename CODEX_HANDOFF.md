# Codex Handoff — Autonomous Self-Debug & Self-Deploy

This document summarises the changes made to the Feberdin multi-agent system in the session ending 2026-04-12.
Read this before making further changes to auto-debug, self-improvement, or deploy-worker logic.

---

## What was built

Three new capabilities were added:

1. **Auto-Debug** — when a task fails, the system automatically analyses the error and launches a targeted fix task
2. **Autonomous Self-Deployment** — after a successful fix the system SSHes to the Unraid host and rebuilds its own Docker containers
3. **SSH smoke-test script** — helper to verify SSH connectivity before enabling autonomous deployment

---

## New file: `services/shared/agentic_lab/auto_debug.py`

Class `AutoDebugService`. Hooked into the orchestrator after every task completes.

### Flow

1. `maybe_debug(task_id, run_task_fn)` — called after task completion, returns early if task did not fail
2. **Recursion guards** — skips tasks that already have `auto_debug_parent_task_id` or `self_improvement_cycle_id` in metadata (prevents infinite fix loops)
3. `_extract_failure()` — walks `_PIPELINE_ORDER` to find the first failed worker and its error text; falls back to `task.latest_error`
4. `_generate_fix_goal()` — asks the LLM for a single concrete fix sentence (max 150 chars); deterministic fallback if LLM unavailable
5. Creates a new fix task via `task_service.create_task()` with:
   - `deployment_target: "self"` in metadata → deploy worker will SSH-update own containers
   - `auto_deploy_staging=True`
   - `allow_repository_modifications=True`
   - `auto_debug_parent_task_id` linking back to the failed task
6. `_monitor_fix()` polls every 30 s for up to 2 h and writes results back to the original task's metadata:
   - `auto_debug_status`: `fix_in_progress` → `fix_ready` / `fix_failed` / `fix_timeout`
   - `auto_debug_fix_task_id`, `auto_debug_fix_branch`, `auto_debug_fix_pr_url`

---

## Changed: `services/orchestrator/app.py`

- Imports and instantiates `AutoDebugService`
- In `_run_in_background()`, after the task completes:

```python
asyncio.create_task(auto_debug_service.maybe_debug(task_id, _run_workflow_task))
```

---

## Changed: `services/deploy_worker/app.py`

- When `request.metadata["deployment_target"] == "self"`, delegates to `_run_self_update()`
- `_run_self_update()` invokes `scripts/unraid/self-update.sh` with:
  1. `self_host_project_dir`
  2. `self_host_compose_file`
  3. `branch_name`
  4. `self_host_ssh_user`
  5. `self_host_ssh_host`
  6. `self_host_ssh_port`
  7. `self_host_health_url`
  8. `self_host_ssh_key_file` (path to SSH private key inside container)
- Returns a clear error response if `SELF_HOST_SSH_HOST` is not configured

---

## New file: `scripts/unraid/self-update.sh`

Executed on the Unraid host via SSH heredoc. Arguments:

```
PROJECT_DIR  COMPOSE_FILE  BRANCH_NAME  SSH_USER  SSH_HOST  SSH_PORT  [HEALTH_URL]  [SSH_KEY]
```

Steps:
1. Checks that a git repo exists at `PROJECT_DIR`
2. Saves current HEAD SHA to `.agentic-releases/previous.sha`
3. `git fetch && git checkout <branch> && git pull --ff-only`
4. `docker compose up -d --build`
5. Optional healthcheck: polls up to 12 × 5 s until orchestrator returns `200 OK`

SSH key is passed as `-i <path>` in `SSH_OPTS` when the file exists.

---

## New file: `scripts/unraid/test-ssh.sh`

Smoke-test for SSH connectivity. Reads `SELF_HOST_*` values from `.env` automatically; CLI arguments override:

```bash
sh scripts/unraid/test-ssh.sh [SSH_HOST] [SSH_PORT] [SSH_USER] [SSH_KEY]
```

---

## Changed: `services/shared/agentic_lab/config.py`

New settings added:

| Env variable | Field | Default |
|---|---|---|
| `AUTO_DEBUG_ENABLED` | `auto_debug_enabled` | `False` |
| `AUTO_DEBUG_MAX_ATTEMPTS` | `auto_debug_max_attempts` | `2` |
| `SELF_HOST_SSH_USER` | `self_host_ssh_user` | `"root"` |
| `SELF_HOST_SSH_HOST` | `self_host_ssh_host` | `""` |
| `SELF_HOST_SSH_PORT` | `self_host_ssh_port` | `22` |
| `SELF_HOST_PROJECT_DIR` | `self_host_project_dir` | `""` |
| `SELF_HOST_COMPOSE_FILE` | `self_host_compose_file` | `"docker-compose.yml"` |
| `SELF_HOST_HEALTH_URL` | `self_host_health_url` | `""` |
| `SELF_HOST_SSH_KEY_FILE` | `self_host_ssh_key_file` | `""` |

---

## Changed: `services/shared/agentic_lab/self_improvement.py`

Fix tasks created by the Self-Improvement cycle now also include `"deployment_target": "self"` in metadata.
This means a successful self-improvement fix automatically triggers `_run_self_update()` in the deploy worker.

---

## Changed: `.env.example`

New entries:

```
AUTO_DEBUG_ENABLED=false
AUTO_DEBUG_MAX_ATTEMPTS=2
SELF_HOST_SSH_USER=root
SELF_HOST_SSH_HOST=
SELF_HOST_SSH_PORT=22
SELF_HOST_PROJECT_DIR=
SELF_HOST_COMPOSE_FILE=docker-compose.yml
SELF_HOST_HEALTH_URL=http://localhost:18080/health
SELF_HOST_SSH_KEY_FILE=/run/project-secrets/unraid_ssh_key
```

---

## Runtime setup required on Unraid host

```bash
# Copy SSH key into the secrets directory so the deploy worker container can read it
cp /root/.ssh/unraid_agent /mnt/user/appdata/feberdin-agent-team/secrets/unraid_ssh_key
chmod 644 /mnt/user/appdata/feberdin-agent-team/secrets/unraid_ssh_key
```

Required `.env` values to enable full autonomous operation:

```
SELF_HOST_SSH_HOST=192.168.57.10
SELF_HOST_PROJECT_DIR=/mnt/user/appdata/feberdin-agent-team/repo
SELF_HOST_SSH_KEY_FILE=/run/project-secrets/unraid_ssh_key
SELF_IMPROVEMENT_DEPLOY_AFTER_SUCCESS=true
AUTO_DEBUG_ENABLED=true
```

After updating `.env`, rebuild:

```bash
docker compose build && docker compose up -d
```

---

## Known issues / watch out for

- **Duplicate keys in `.env`** crash the orchestrator on startup (`RuntimeError: Duplicate keys in .env detected`). Always use `sed -i '/^KEY=/d' .env` before appending a key that might already exist.
- **SSH `known_hosts` warning** (`Operation not permitted`) on Unraid is harmless — the read-only filesystem blocks the write but SSH still connects successfully.
- **`$(hostname)` not expanded** in `test-ssh.sh` output is a minor cosmetic issue (single-quote heredoc); SSH connectivity itself is confirmed.
