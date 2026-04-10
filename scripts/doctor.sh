#!/usr/bin/env bash
# Purpose: Run a preflight diagnosis for the local or Unraid runtime before starting the stack.
# Input/Output: Reads `.env`, validates host paths and the rendered Compose model, and exits non-zero on unsafe runtime issues.
# Important invariants: The script never mutates containers or host state; it only validates configuration and filesystem readiness.
# How to debug: Re-run with `bash -x`, then inspect `.env`, `docker compose config`, and the reported path or service name.

set -euo pipefail

readonly ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly ENV_FILE="${ROOT_DIR}/.env"
readonly SKIP_PORT_CHECK="${SKIP_PORT_CHECK:-false}"

COMPOSE_FILE_OUTPUT=""
COMPOSE_TOOL=()

readonly REQUIRED_RUNTIME_SERVICES=(
  orchestrator
  requirements-worker
  research-worker
  architecture-worker
  coding-worker
  reviewer-worker
  test-worker
  github-worker
  deploy-worker
  qa-worker
  security-worker
  validation-worker
  documentation-worker
  memory-worker
  data-worker
  ux-worker
  cost-worker
  human-resources-worker
  web-ui
)

readonly REQUIRED_MOUNT_TARGETS=(
  /data
  /reports
  /workspace
  /staging-stacks
  /app
)

cleanup() {
  if [[ -n "${COMPOSE_FILE_OUTPUT}" && -f "${COMPOSE_FILE_OUTPUT}" ]]; then
    rm -f "${COMPOSE_FILE_OUTPUT}"
  fi
}

trap cleanup EXIT

log_info() {
  printf '[INFO] %s\n' "$1"
}

log_error() {
  printf '[ERROR] %s\n' "$1" >&2
}

die() {
  log_error "$1"
  exit 1
}

validate_env_duplicates() {
  local duplicates

  duplicates="$(awk -F= '
    /^[[:space:]]*#/ || /^[[:space:]]*$/ || !/=/{next}
    {
      key=$1
      sub(/^[[:space:]]+/, "", key)
      sub(/[[:space:]]+$/, "", key)
      seen[key]++
    }
    END {
      for (key in seen) {
        if (seen[key] > 1) {
          print key
        }
      }
    }
  ' "${ENV_FILE}" | sort)"

  if [[ -n "${duplicates}" ]]; then
    die "Die .env enthält doppelte Schlüssel: $(printf '%s' "${duplicates}" | tr '\n' ' '). Bitte jeden Schlüssel nur einmal definieren."
  fi
}

load_env() {
  [[ -f "${ENV_FILE}" ]] || die "Es fehlt ${ENV_FILE}. Bitte zuerst .env.example nach .env kopieren."
  validate_env_duplicates
  set -a
  # shellcheck disable=SC1090
  . "${ENV_FILE}"
  set +a
}

require_directory_writable() {
  local variable_name="$1"
  local directory_path="$2"

  [[ -n "${directory_path}" ]] || die "${variable_name} ist leer. Bitte die .env prüfen."
  [[ -d "${directory_path}" ]] || die "${variable_name}=${directory_path} existiert nicht. Bitte zuerst ./scripts/bootstrap.sh ausführen."
  [[ -w "${directory_path}" ]] || die "${variable_name}=${directory_path} ist nicht beschreibbar. Bitte Host-Rechte und PUID/PGID prüfen."
}

discover_compose_tool() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    COMPOSE_TOOL=(docker compose)
    return
  fi

  if command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_TOOL=(docker-compose)
    return
  fi

  die "Weder 'docker compose' noch 'docker-compose' ist verfügbar. Bitte Docker/Compose auf dem Host prüfen."
}

render_compose_config() {
  COMPOSE_FILE_OUTPUT="$(mktemp)"
  "${COMPOSE_TOOL[@]}" \
    -f "${ROOT_DIR}/docker-compose.yml" \
    -f "${ROOT_DIR}/docker-compose.override.yml" \
    --env-file "${ENV_FILE}" \
    config >"${COMPOSE_FILE_OUTPUT}"
}

service_has_mount_target() {
  local service_name="$1"
  local target_path="$2"

  awk -v service="${service_name}" -v target="${target_path}" '
    $0 == "services:" {in_services=1; next}
    in_services && $0 == "  " service ":" {in_service=1; next}
    in_service && $0 ~ "^  [A-Za-z0-9_-]+:" {exit(found ? 0 : 1)}
    in_service && index($0, target) {found=1}
    END {exit(found ? 0 : 1)}
  ' "${COMPOSE_FILE_OUTPUT}"
}

assert_runtime_mounts() {
  local service_name target_path

  for service_name in "${REQUIRED_RUNTIME_SERVICES[@]}"; do
    for target_path in "${REQUIRED_MOUNT_TARGETS[@]}"; do
      if ! service_has_mount_target "${service_name}" "${target_path}"; then
        die "Im finalen Compose-Modell fehlt bei Service '${service_name}' der erwartete Mount für '${target_path}'. Bitte 'docker compose config' prüfen."
      fi
    done
  done
}

port_is_in_use() {
  local port="$1"

  if command -v ss >/dev/null 2>&1; then
    ss -ltn "( sport = :${port} )" 2>/dev/null | grep -q LISTEN
    return
  fi

  if command -v netstat >/dev/null 2>&1; then
    netstat -ltn 2>/dev/null | awk '{print $4}' | grep -Eq "(^|[:.])${port}$"
    return
  fi

  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"${port}" -sTCP:LISTEN >/dev/null 2>&1
    return
  fi

  if command -v nc >/dev/null 2>&1; then
    nc -z 127.0.0.1 "${port}" >/dev/null 2>&1
    return
  fi

  die "Kein Tool für den Port-Check gefunden. Installiere 'ss', 'netstat', 'lsof' oder 'nc', oder setze SKIP_PORT_CHECK=true."
}

assert_port_free() {
  local label="$1"
  local port="$2"

  if port_is_in_use "${port}"; then
    die "${label}=${port} ist bereits belegt. Bitte den Port ändern oder den bestehenden Dienst stoppen."
  fi
}

main() {
  load_env

  require_directory_writable "HOST_DATA_DIR" "${HOST_DATA_DIR:-}"
  require_directory_writable "HOST_REPORTS_DIR" "${HOST_REPORTS_DIR:-}"
  require_directory_writable "HOST_WORKSPACE_ROOT" "${HOST_WORKSPACE_ROOT:-}"
  require_directory_writable "HOST_STAGING_STACK_ROOT" "${HOST_STAGING_STACK_ROOT:-}"

  discover_compose_tool
  render_compose_config
  assert_runtime_mounts

  if [[ "${SKIP_PORT_CHECK}" != "true" ]]; then
    assert_port_free "ORCHESTRATOR_PORT" "${ORCHESTRATOR_PORT:-18080}"
    assert_port_free "WEB_UI_PORT" "${WEB_UI_PORT:-18088}"
  fi

  log_info "Preflight erfolgreich. Runtime-Pfade, Ports und Compose-Mounts sehen konsistent aus."
}

main "$@"
