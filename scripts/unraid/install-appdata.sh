#!/bin/sh
# Purpose: Create the standard Unraid appdata layout expected by this project.
# Input/Output: Reads `.env` or explicit arguments and creates the host-side persistence folders.
# Important invariants: The script only creates directories and never removes existing data.
# How to debug: If Unraid still cannot see the folders, confirm that `/mnt/user/appdata` is mounted as expected.

set -eu

if [ -f ".env" ]; then
  . ./.env
fi

mkdir -p "${HOST_DATA_DIR:-/mnt/user/appdata/feberdin-agent-team/data}"
mkdir -p "${HOST_REPORTS_DIR:-/mnt/user/appdata/feberdin-agent-team/reports}"
mkdir -p "${HOST_WORKSPACE_ROOT:-/mnt/user/appdata/feberdin-agent-team/workspace}"
mkdir -p "${HOST_STAGING_STACK_ROOT:-/mnt/user/appdata/feberdin-agent-team/staging-stacks}"
mkdir -p "${HOST_SECRETS_DIR:-/mnt/user/appdata/feberdin-agent-team/secrets}"

echo "Unraid appdata folders are ready."
