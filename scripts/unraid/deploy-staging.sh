#!/bin/sh
# Purpose: Update an existing staging checkout on Unraid to the requested branch and restart its compose stack.
# Input/Output: Validates the local branch, SSHes to the staging host, updates the remote checkout, and runs docker compose.
# Important invariants: The remote checkout must already exist, only staging is targeted, and the previous SHA is preserved for rollback.
# How to debug: If deployment fails, run the remote git and docker compose commands manually on the staging host.

set -eu

LOCAL_REPO_PATH="${1:?missing local repo path}"
PROJECT_DIR="${2:?missing remote project dir}"
COMPOSE_FILE="${3:?missing compose file}"
BRANCH_NAME="${4:?missing branch name}"
SSH_USER="${5:?missing ssh user}"
SSH_HOST="${6:?missing ssh host}"
SSH_PORT="${7:?missing ssh port}"

if [ ! -d "${LOCAL_REPO_PATH}/.git" ]; then
  echo "Expected a local git checkout at ${LOCAL_REPO_PATH}." >&2
  exit 1
fi

if ! git -C "${LOCAL_REPO_PATH}" rev-parse --verify "${BRANCH_NAME}" >/dev/null 2>&1; then
  echo "Local branch ${BRANCH_NAME} does not exist." >&2
  exit 1
fi

REMOTE="${SSH_USER}@${SSH_HOST}"

ssh -p "${SSH_PORT}" "${REMOTE}" /bin/sh <<EOF
set -eu

if [ ! -d "${PROJECT_DIR}/.git" ]; then
  echo "Expected an existing staging checkout at ${PROJECT_DIR}." >&2
  exit 1
fi

mkdir -p "${PROJECT_DIR}/.agentic-releases"
PREVIOUS_SHA=\$(git -C "${PROJECT_DIR}" rev-parse HEAD)
printf '%s\n' "\${PREVIOUS_SHA}" > "${PROJECT_DIR}/.agentic-releases/previous.sha"

git -C "${PROJECT_DIR}" fetch origin
git -C "${PROJECT_DIR}" checkout "${BRANCH_NAME}"
git -C "${PROJECT_DIR}" pull --ff-only origin "${BRANCH_NAME}"
docker compose -f "${PROJECT_DIR}/${COMPOSE_FILE}" up -d --build
EOF
