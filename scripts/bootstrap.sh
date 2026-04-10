#!/bin/sh
# Purpose: Prepare a local or Unraid-backed working directory for the agent team stack.
# Input/Output: Reads `.env`, creates persistent directories, and can optionally bootstrap SSH access for Unraid.
# Important invariants: The script never writes secrets, does not start containers automatically, and SSH bootstrap remains opt-in.
# How to debug: If directories are missing after this script, inspect the sourced `.env` values first. If SSH bootstrap fails, run `./scripts/setup_unraid_ssh.sh` directly.

set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

# shellcheck source=./lib/env.sh
. "${SCRIPT_DIR}/lib/env.sh"

if [ ! -f ".env" ]; then
  echo "Missing .env. Copy .env.example to .env and fill in your values first." >&2
  exit 1
fi

load_env_file ".env"

mkdir -p "${HOST_DATA_DIR:-./data}"
mkdir -p "${HOST_REPORTS_DIR:-./reports}"
mkdir -p "${HOST_WORKSPACE_ROOT:-./workspace}"
mkdir -p "${HOST_STAGING_STACK_ROOT:-./staging-stacks}"
mkdir -p "${HOST_SECRETS_DIR:-./secrets}"

if [ "${BOOTSTRAP_SKIP_DOCTOR:-false}" != "true" ]; then
  bash ./scripts/doctor.sh
fi

if [ "${BOOTSTRAP_UNRAID_SSH:-false}" = "true" ]; then
  echo "BOOTSTRAP_UNRAID_SSH=true erkannt."
  echo "Starte optionales SSH-Bootstrap für root@192.168.57.10 ..."
  ./scripts/setup_unraid_ssh.sh
fi

echo "Bootstrap complete."
echo "Next steps:"
echo "  1. Review .env"
echo "  2. docker compose up --build -d"
echo "  3. Open http://localhost:${WEB_UI_PORT:-18088}"
