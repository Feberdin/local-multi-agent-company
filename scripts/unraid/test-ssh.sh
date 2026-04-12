#!/bin/sh
# Purpose: Smoke-test SSH connectivity to the Unraid self-host target.
# Usage: sh scripts/unraid/test-ssh.sh [SSH_HOST] [SSH_PORT] [SSH_USER] [SSH_KEY]
# All arguments are optional — defaults are read from the .env file if present.

set -eu

# Load .env if it exists (strip comments and empty lines)
if [ -f ".env" ]; then
  # shellcheck disable=SC2046
  export $(grep -v '^#' .env | grep -v '^[[:space:]]*$' | xargs) 2>/dev/null || true
fi

SSH_HOST="${1:-${SELF_HOST_SSH_HOST:-}}"
SSH_PORT="${2:-${SELF_HOST_SSH_PORT:-22}}"
SSH_USER="${3:-${SELF_HOST_SSH_USER:-root}}"
SSH_KEY="${4:-${SELF_HOST_SSH_KEY_FILE:-}}"

if [ -z "${SSH_HOST}" ]; then
  echo "ERROR: No SSH host specified. Pass it as the first argument or set SELF_HOST_SSH_HOST in .env." >&2
  exit 1
fi

REMOTE="${SSH_USER}@${SSH_HOST}"
SSH_OPTS="-o StrictHostKeyChecking=no -o BatchMode=yes -o ConnectTimeout=10 -p ${SSH_PORT}"

if [ -n "${SSH_KEY}" ] && [ -f "${SSH_KEY}" ]; then
  SSH_OPTS="${SSH_OPTS} -i ${SSH_KEY}"
  echo "Using SSH key: ${SSH_KEY}"
else
  echo "Using default SSH key / agent"
fi

echo "Connecting to ${REMOTE}:${SSH_PORT} ..."

# shellcheck disable=SC2086
if ssh ${SSH_OPTS} "${REMOTE}" "echo 'SSH OK — hostname: \$(hostname), uptime: \$(uptime)'"; then
  echo ""
  echo "SUCCESS: SSH connection to ${SSH_HOST} works."
else
  echo ""
  echo "FAILED: Could not connect to ${SSH_HOST}." >&2
  exit 1
fi
