#!/bin/sh
# Purpose: Dispatch a self-improvement fix branch to the running Feberdin agent stack on Unraid.
# Input/Output: Receives branch name and SSH target, SSHes to the Unraid host, checks out the branch,
#               and starts a detached rollout process that rebuilds all services except the rollback worker.
# Important invariants: The repo on the Unraid host must already exist (cloned during initial setup).
#                       The rollback worker intentionally stays on the old image until the rollout is confirmed healthy.
# How to debug: Inspect `.agentic-releases/self-update/<task-id>/watch.log` on the Unraid host for the detached rollout.

set -eu

PROJECT_DIR="${1:?missing project dir on unraid host}"
COMPOSE_FILE="${2:?missing compose file}"
BRANCH_NAME="${3:?missing branch name}"
SSH_USER="${4:?missing ssh user}"
SSH_HOST="${5:?missing ssh host}"
SSH_PORT="${6:?missing ssh port}"
HEALTH_URL="${7:-}"
SSH_KEY="${8:-}"
TASK_ID="${9:-manual-self-update}"

REMOTE="${SSH_USER}@${SSH_HOST}"

# Build SSH options — use explicit key if provided, otherwise fall back to default agent/key
SSH_OPTS="-o StrictHostKeyChecking=no -o BatchMode=yes -p ${SSH_PORT}"
if [ -n "${SSH_KEY}" ] && [ -f "${SSH_KEY}" ]; then
  SSH_OPTS="${SSH_OPTS} -i ${SSH_KEY}"
fi

echo "self-update: connecting to ${REMOTE}:${SSH_PORT} to deploy branch ${BRANCH_NAME}"

# shellcheck disable=SC2086
ssh ${SSH_OPTS} "${REMOTE}" /bin/sh <<EOF
set -eu

if [ ! -d "${PROJECT_DIR}/.git" ]; then
  echo "self-update: no git repo at ${PROJECT_DIR} — run the initial install first." >&2
  exit 1
fi

mkdir -p "${PROJECT_DIR}/.agentic-releases"
PREVIOUS_SHA=\$(git -C "${PROJECT_DIR}" rev-parse HEAD 2>/dev/null || echo "unknown")
printf '%s\n' "\${PREVIOUS_SHA}" > "${PROJECT_DIR}/.agentic-releases/previous.sha"
echo "self-update: previous SHA saved: \${PREVIOUS_SHA}"

STATE_DIR="${PROJECT_DIR}/.agentic-releases/self-update/${TASK_ID}"
SCRIPT_PATH="\${STATE_DIR}/run.sh"
LOG_PATH="\${STATE_DIR}/watch.log"
mkdir -p "\${STATE_DIR}"

cat > "\${SCRIPT_PATH}" <<'REMOTE_SCRIPT'
#!/bin/sh
set -eu

PROJECT_DIR="\$1"
COMPOSE_FILE="\$2"
BRANCH_NAME="\$3"

services_without_rollback() {
  docker compose -f "${PROJECT_DIR}/${COMPOSE_FILE}" config --services | grep -v '^rollback-worker$' | tr '\n' ' '
}

SERVICES="\$(services_without_rollback)"

export_build_metadata() {
  BUILD_COMMIT_SHA="\$(git -C "\${PROJECT_DIR}" rev-parse --short=12 HEAD 2>/dev/null || echo "")"
  BUILD_GIT_REF="\${BRANCH_NAME}"
  BUILD_BUILT_AT_UTC="\$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  export BUILD_COMMIT_SHA BUILD_GIT_REF BUILD_BUILT_AT_UTC
}

git -C "\${PROJECT_DIR}" fetch origin
git -C "\${PROJECT_DIR}" checkout "\${BRANCH_NAME}"
git -C "\${PROJECT_DIR}" pull --ff-only origin "\${BRANCH_NAME}" || true
echo "self-update: checked out \${BRANCH_NAME}"
export_build_metadata

if [ -n "\${SERVICES}" ]; then
  # Why this exists: the rollback worker must stay alive while the rest of the stack restarts.
  # What happens here: rebuild every service except `rollback-worker`, so health monitoring survives the rollout.
  docker compose -f "\${PROJECT_DIR}/\${COMPOSE_FILE}" up -d --build \${SERVICES}
else
  docker compose -f "\${PROJECT_DIR}/\${COMPOSE_FILE}" up -d --build
fi
echo "self-update: rollout dispatched"
REMOTE_SCRIPT

chmod +x "\${SCRIPT_PATH}"
nohup "\${SCRIPT_PATH}" "${PROJECT_DIR}" "${COMPOSE_FILE}" "${BRANCH_NAME}" > "\${LOG_PATH}" 2>&1 < /dev/null &
printf '%s\n' "\$!" > "\${STATE_DIR}/pid"
echo "self-update: detached rollout pid \$(cat "\${STATE_DIR}/pid")"
EOF

echo "self-update: deploy command sent to ${SSH_HOST}"
