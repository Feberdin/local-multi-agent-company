#!/bin/sh
# Purpose: Roll back a self-updating Feberdin stack on Unraid to the last known stable commit.
# Input/Output: SSHes to the target host, checks out the given commit, and rebuilds all services except
#               the rollback worker so monitoring stays available until the host is healthy again.
# Important invariants:
#   - The rollback target is a concrete git ref, not a guessed branch head.
#   - The rollback worker must stay up while the rest of the stack is restarted.
# How to debug:
#   - Run the emitted ssh and git commands manually on the host.
#   - Verify the target health URL from the rollback worker container, not only on the host shell.

set -eu

PROJECT_DIR="${1:?missing remote project dir}"
COMPOSE_FILE="${2:?missing compose file}"
GIT_REF="${3:?missing rollback git ref}"
SSH_USER="${4:?missing ssh user}"
SSH_HOST="${5:?missing ssh host}"
SSH_PORT="${6:?missing ssh port}"
HEALTH_URL="${7:-}"
SSH_KEY="${8:-}"

REMOTE="${SSH_USER}@${SSH_HOST}"

SSH_OPTS="-o StrictHostKeyChecking=no -o BatchMode=yes -p ${SSH_PORT}"
if [ -n "${SSH_KEY}" ] && [ -f "${SSH_KEY}" ]; then
  SSH_OPTS="${SSH_OPTS} -i ${SSH_KEY}"
fi

# shellcheck disable=SC2086
ssh ${SSH_OPTS} "${REMOTE}" /bin/sh <<EOF
set -eu

if [ ! -d "${PROJECT_DIR}/.git" ]; then
  echo "rollback-self-update: no git repo at ${PROJECT_DIR}." >&2
  exit 1
fi

services_without_rollback() {
  docker compose -f "${PROJECT_DIR}/${COMPOSE_FILE}" config --services | grep -v '^rollback-worker$' | tr '\n' ' '
}

SERVICES="\$(services_without_rollback)"

git -C "${PROJECT_DIR}" fetch origin || true
git -C "${PROJECT_DIR}" checkout "${GIT_REF}"

BUILD_COMMIT_SHA="\$(git -C "${PROJECT_DIR}" rev-parse --short=12 HEAD 2>/dev/null || echo "")"
BUILD_GIT_REF="${GIT_REF}"
BUILD_BUILT_AT_UTC="\$(date -u +%Y-%m-%dT%H:%M:%SZ)"
export BUILD_COMMIT_SHA BUILD_GIT_REF BUILD_BUILT_AT_UTC

if [ -n "\${SERVICES}" ]; then
  docker compose -f "${PROJECT_DIR}/${COMPOSE_FILE}" up -d --build \${SERVICES}
else
  docker compose -f "${PROJECT_DIR}/${COMPOSE_FILE}" up -d --build
fi
EOF

if [ -n "${HEALTH_URL}" ]; then
  ATTEMPTS=0
  while [ "${ATTEMPTS}" -lt 12 ]; do
    ATTEMPTS=$((ATTEMPTS + 1))
    STATUS=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "${HEALTH_URL}" 2>/dev/null || echo "000")
    if [ "${STATUS}" = "200" ]; then
      echo "rollback-self-update: target healthy after ${ATTEMPTS} attempt(s)."
      exit 0
    fi
    echo "rollback-self-update: attempt ${ATTEMPTS}/12 — status=${STATUS}, retrying in 5s..."
    sleep 5
  done
  echo "rollback-self-update: target did not become healthy within 60s." >&2
  exit 1
fi
