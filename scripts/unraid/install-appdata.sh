#!/bin/sh
# Purpose: Create the standard Unraid appdata layout expected by this project.
# Input/Output: Reads `.env` or explicit arguments and creates the host-side persistence folders.
# Important invariants: The script only creates directories and never removes existing data.
# How to debug: If Unraid still cannot see the folders, confirm that `/mnt/user/appdata` is mounted as expected.

set -eu

validate_env_duplicates() {
  tmp_file="$(mktemp)"
  awk -F= '
    /^[[:space:]]*#/ || /^[[:space:]]*$/ || !/=/{next}
    {
      key=$1
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", key)
      seen[key]++
    }
    END {
      duplicates=""
      for (key in seen) {
        if (seen[key] > 1) {
          duplicates = duplicates key " "
        }
      }
      if (duplicates != "") {
        print duplicates
        exit 1
      }
    }
  ' ".env" >"${tmp_file}" 2>/dev/null || {
    duplicates="$(cat "${tmp_file}" 2>/dev/null || true)"
    rm -f "${tmp_file}"
    echo "Duplicate keys in .env detected: ${duplicates}" >&2
    exit 1
  }
  rm -f "${tmp_file}"
}

if [ -f ".env" ]; then
  validate_env_duplicates
  . ./.env
fi

mkdir -p "${HOST_DATA_DIR:-/mnt/user/appdata/feberdin-agent-team/data}"
mkdir -p "${HOST_REPORTS_DIR:-/mnt/user/appdata/feberdin-agent-team/reports}"
mkdir -p "${HOST_WORKSPACE_ROOT:-/mnt/user/appdata/feberdin-agent-team/workspace}"
mkdir -p "${HOST_STAGING_STACK_ROOT:-/mnt/user/appdata/feberdin-agent-team/staging-stacks}"
mkdir -p "${HOST_SECRETS_DIR:-/mnt/user/appdata/feberdin-agent-team/secrets}"

echo "Unraid appdata folders are ready."
