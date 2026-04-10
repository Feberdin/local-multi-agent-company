#!/bin/sh
# Purpose: Shared `.env` helpers for project scripts that must work with Docker-style env files.
# Input/Output: Reads key-value lines from an env file and exports them for POSIX shell scripts.
# Important invariants: Duplicate keys are rejected, invalid variable names are blocked, and values may contain spaces after `=`.
# How to debug: Run the calling script with `sh -x` and inspect the exact `.env` line reported by these helpers.

validate_env_duplicates() {
  env_file_path="$1"
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
  ' "${env_file_path}" >"${tmp_file}" 2>/dev/null || {
    duplicates="$(cat "${tmp_file}" 2>/dev/null || true)"
    rm -f "${tmp_file}"
    echo "Duplicate keys in ${env_file_path} detected: ${duplicates}" >&2
    return 1
  }

  rm -f "${tmp_file}"
}

load_env_file() {
  env_file_path="$1"

  [ -f "${env_file_path}" ] || {
    echo "Missing env file: ${env_file_path}" >&2
    return 1
  }

  validate_env_duplicates "${env_file_path}" || return 1

  while IFS= read -r raw_line || [ -n "${raw_line}" ]; do
    line="$(printf '%s' "${raw_line}" | tr -d '\r')"

    case "${line}" in
      ''|'#'*)
        continue
        ;;
    esac

    case "${line}" in
      *=*)
        key="${line%%=*}"
        value="${line#*=}"
        ;;
      *)
        continue
        ;;
    esac

    key="$(printf '%s' "${key}" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"

    case "${key}" in
      ''|*[!A-Za-z0-9_]*)
        echo "Invalid env key '${key}' in ${env_file_path}" >&2
        return 1
        ;;
      [0-9]*)
        echo "Invalid env key '${key}' in ${env_file_path}" >&2
        return 1
        ;;
    esac

    case "${value}" in
      \"*\")
        value="${value#\"}"
        value="${value%\"}"
        ;;
      \'*\')
        value="${value#\'}"
        value="${value%\'}"
        ;;
    esac

    export "${key}=${value}"
  done < "${env_file_path}"
}
