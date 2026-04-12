#!/bin/sh
# Purpose: Apply a self-improvement fix branch to the running Feberdin agent stack on Unraid.
# Input/Output: Receives branch name and SSH target, SSHes to the Unraid host, checks out the branch,
#               rebuilds all agent containers, and verifies the orchestrator is healthy again.
# Important invariants: The repo on the Unraid host must already exist (cloned during initial setup).
#                       Only the agent stack itself is updated — staging targets are untouched.
# How to debug: Run the ssh commands manually on the Unraid host to isolate git or compose failures.

set -eu

PROJECT_DIR="${1:?missing project dir on unraid host}"
COMPOSE_FILE="${2:?missing compose file}"
BRANCH_NAME="${3:?missing branch name}"
SSH_USER="${4:?missing ssh user}"
SSH_HOST="${5:?missing ssh host}"
SSH_PORT="${6:?missing ssh port}"
HEALTH_URL="${7:-}"
SSH_KEY="${8:-}"

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

git -C "${PROJECT_DIR}" fetch origin
git -C "${PROJECT_DIR}" checkout "${BRANCH_NAME}"
git -C "${PROJECT_DIR}" pull --ff-only origin "${BRANCH_NAME}" || true
echo "self-update: checked out ${BRANCH_NAME}"

docker compose -f "${PROJECT_DIR}/${COMPOSE_FILE}" up -d --build
echo "self-update: containers rebuilt and restarted"
EOF

echo "self-update: deploy command sent to ${SSH_HOST}"

# Optional healthcheck — wait up to 60s for the orchestrator to come back up
if [ -n "${HEALTH_URL}" ]; then
  echo "self-update: waiting for orchestrator health at ${HEALTH_URL}"
  ATTEMPTS=0
  while [ "${ATTEMPTS}" -lt 12 ]; do
    ATTEMPTS=$((ATTEMPTS + 1))
    STATUS=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "${HEALTH_URL}" 2>/dev/null || echo "000")
    if [ "${STATUS}" = "200" ]; then
      echo "self-update: orchestrator healthy after ${ATTEMPTS} attempt(s)."
      exit 0
    fi
    echo "self-update: attempt ${ATTEMPTS}/12 — status=${STATUS}, retrying in 5s..."
    sleep 5
  done
  echo "self-update: orchestrator did not become healthy within 60s." >&2
  exit 1
fi
