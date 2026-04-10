#!/usr/bin/env bash
# Purpose: Install or update the Feberdin agent-team project into a dedicated Unraid appdata root from Git and optionally start the stack.
# Input/Output: Clones or fast-forwards the repository into /mnt/user/appdata/feberdin-agent-team/repo, prepares the standard project folders, and can run docker compose.
# Important invariants: All writes stay under one explicit project root, existing local repo changes are never overwritten silently, and stack startup is opt-in.
# How to debug: Re-run with `bash -x`, inspect the repository under the project root, and compare the configured REPO_URL/REPO_REF with the remote state.

set -euo pipefail

readonly DEFAULT_PROJECT_ROOT="/mnt/user/appdata/feberdin-agent-team"
readonly PROJECT_ROOT="${PROJECT_ROOT:-${DEFAULT_PROJECT_ROOT}}"
readonly REPO_URL="${REPO_URL:?Bitte REPO_URL setzen, zum Beispiel https://github.com/Feberdin/dein-repo.git}"
readonly REPO_REF="${REPO_REF:-main}"
readonly AUTO_START_STACK="${AUTO_START_STACK:-false}"

readonly REPO_DIR="${PROJECT_ROOT}/repo"
readonly DATA_DIR="${PROJECT_ROOT}/data"
readonly REPORTS_DIR="${PROJECT_ROOT}/reports"
readonly WORKSPACE_DIR="${PROJECT_ROOT}/workspace"
readonly STAGING_STACKS_DIR="${PROJECT_ROOT}/staging-stacks"
readonly SECRETS_DIR="${PROJECT_ROOT}/secrets"

log_info() {
  printf '[INFO] %s\n' "$1"
}

log_warn() {
  printf '[WARN] %s\n' "$1" >&2
}

log_error() {
  printf '[ERROR] %s\n' "$1" >&2
}

die() {
  log_error "$1"
  exit 1
}

require_command() {
  local command_name="$1"

  if ! command -v "${command_name}" >/dev/null 2>&1; then
    die "Das Kommando '${command_name}' wurde nicht gefunden."
  fi
}

ensure_prerequisites() {
  # Why this exists:
  # The bootstrap should fail before touching the project root if core tools are missing.
  require_command bash
  require_command git
  require_command mkdir
  require_command cat
  require_command cp
}

ensure_project_root() {
  # Security note:
  # This installer intentionally owns exactly one project root and all managed paths are derived from it.
  mkdir -p "${PROJECT_ROOT}" "${DATA_DIR}" "${REPORTS_DIR}" "${WORKSPACE_DIR}" "${STAGING_STACKS_DIR}" "${SECRETS_DIR}"
  log_info "Projektwurzel vorbereitet: ${PROJECT_ROOT}"
}

directory_is_empty() {
  local directory_path="$1"
  local entries=()

  # Why this exists:
  # We want to allow cloning into a pre-created empty directory, but we must still block accidental reuse of a populated non-git path.
  shopt -s nullglob dotglob
  entries=("${directory_path}"/*)
  shopt -u nullglob dotglob
  [[ ${#entries[@]} -eq 0 ]]
}

clone_or_update_repo() {
  local current_remote

  if [[ -d "${REPO_DIR}/.git" ]]; then
    current_remote="$(git -C "${REPO_DIR}" remote get-url origin 2>/dev/null || true)"
    [[ -n "${current_remote}" ]] || die "Im bestehenden Repo unter ${REPO_DIR} fehlt der Remote 'origin'."

    if [[ "${current_remote}" != "${REPO_URL}" ]]; then
      die "Das bestehende Repo unter ${REPO_DIR} verweist auf ${current_remote} statt auf ${REPO_URL}. Aus Sicherheitsgründen kein automatisches Umschalten."
    fi

    if [[ -n "$(git -C "${REPO_DIR}" status --porcelain)" ]]; then
      die "Das bestehende Repo unter ${REPO_DIR} enthält lokale Änderungen. Bitte erst committen oder sichern."
    fi

    log_info "Vorhandenes Repo gefunden. Aktualisiere ${REPO_REF} per Fast-Forward."
    git -C "${REPO_DIR}" fetch --prune origin "${REPO_REF}"
    git -C "${REPO_DIR}" checkout "${REPO_REF}"
    git -C "${REPO_DIR}" pull --ff-only origin "${REPO_REF}"
    return
  fi

  if [[ -e "${REPO_DIR}" ]]; then
    if [[ -d "${REPO_DIR}" ]] && directory_is_empty "${REPO_DIR}"; then
      log_info "Der Zielpfad ${REPO_DIR} existiert bereits als leerer Ordner. Verwende ihn als sicheres Klon-Ziel."
      git clone --branch "${REPO_REF}" --single-branch "${REPO_URL}" "${REPO_DIR}"
      return
    fi

    die "Der Zielpfad ${REPO_DIR} existiert bereits und ist kein leeres Git-Repo. Bitte den Inhalt prüfen, sichern oder den Ordner gezielt leeren."
  fi

  log_info "Klone ${REPO_URL} nach ${REPO_DIR}"
  git clone --branch "${REPO_REF}" --single-branch "${REPO_URL}" "${REPO_DIR}"
}

ensure_env_file() {
  local env_file env_example

  env_file="${REPO_DIR}/.env"
  env_example="${REPO_DIR}/.env.example"

  [[ -f "${env_example}" ]] || die "Im Repo fehlt ${env_example}. Das Projekt scheint unvollständig zu sein."

  if [[ -f "${env_file}" ]]; then
    log_info "Vorhandene .env gefunden. Es werden keine bestehenden Einstellungen überschrieben."
    return
  fi

  cp "${env_example}" "${env_file}"
  log_warn "Es wurde eine neue .env aus .env.example erzeugt: ${env_file}"
  log_warn "Bitte prüfe vor dem ersten produktiven Einsatz insbesondere GITHUB_TOKEN, Ports und Staging-Einstellungen."
}

compose_up() {
  # Why this exists:
  # Unraid installations differ; some provide `docker compose`, others still use `docker-compose`.
  if docker compose version >/dev/null 2>&1; then
    (cd "${REPO_DIR}" && docker compose up --build -d)
    return
  fi

  if command -v docker-compose >/dev/null 2>&1; then
    (cd "${REPO_DIR}" && docker-compose up --build -d)
    return
  fi

  die "Weder 'docker compose' noch 'docker-compose' ist verfügbar. Bitte Docker/Compose auf Unraid prüfen."
}

print_next_steps() {
  printf '\n%s\n' "Nächste Schritte:"
  printf '%s\n' "  Projekt: ${REPO_DIR}"
  printf '%s\n' "  Konfiguration: ${REPO_DIR}/.env"
  printf '%s\n' "  Secrets: ${SECRETS_DIR}"
  printf '%s\n' "  Start: cd ${REPO_DIR} && docker compose up --build -d"
}

main() {
  ensure_prerequisites
  ensure_project_root
  clone_or_update_repo
  ensure_env_file

  if [[ "${AUTO_START_STACK}" == "true" ]]; then
    require_command docker
    log_info "AUTO_START_STACK=true erkannt. Starte den Projekt-Stack."
    compose_up
    log_info "Projekt-Stack wurde gestartet."
  else
    log_info "AUTO_START_STACK=false. Der Stack wird nicht automatisch gestartet."
  fi

  print_next_steps
}

main "$@"
