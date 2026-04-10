#!/bin/sh
# Purpose: Prepare host directories for a GitHub self-hosted runner on Unraid.
# Input/Output: Creates persistent directories for runner configuration and work data.
# Important invariants: The script only creates directories and does not register the runner automatically.
# How to debug: If the runner container cannot start, verify the mounted config path created by this script.

set -eu

RUNNER_ROOT="${1:-/mnt/user/appdata/feberdin-agent-team/github-runner}"
mkdir -p "${RUNNER_ROOT}/config"
mkdir -p "${RUNNER_ROOT}/work"

echo "Runner directories prepared under ${RUNNER_ROOT}."
