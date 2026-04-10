#!/bin/sh
# Purpose: Roll back a staging checkout on Unraid to a known git ref and restart its compose stack.
# Input/Output: SSHes to the staging host, checks out the requested ref, and restarts the compose deployment.
# Important invariants: The target ref must already exist on the staging host, and rollback stays limited to staging.
# How to debug: If rollback fails, inspect the remote git ref and whether the compose file exists at the expected path.

set -eu

PROJECT_DIR="${1:?missing remote project dir}"
COMPOSE_FILE="${2:?missing compose file}"
GIT_REF="${3:?missing git ref}"
SSH_USER="${4:?missing ssh user}"
SSH_HOST="${5:?missing ssh host}"
SSH_PORT="${6:?missing ssh port}"

REMOTE="${SSH_USER}@${SSH_HOST}"

ssh -p "${SSH_PORT}" "${REMOTE}" /bin/sh <<EOF
set -eu

git -C "${PROJECT_DIR}" fetch origin
git -C "${PROJECT_DIR}" checkout "${GIT_REF}"
docker compose -f "${PROJECT_DIR}/${COMPOSE_FILE}" up -d --build
EOF
