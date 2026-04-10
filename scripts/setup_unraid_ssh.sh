#!/usr/bin/env bash
# Purpose: Bootstrap a dedicated local SSH key for the coding agent and install its public key on the Unraid root account.
# Input/Output: Creates ~/.ssh/unraid_agent when needed, appends ~/.ssh/unraid_agent.pub to /root/.ssh/authorized_keys on 192.168.57.10, and validates login.
# Important invariants: The private key never leaves the local machine, the remote authorized_keys entry is matched as one exact line, and repeated runs stay idempotent.
# How to debug: Re-run with `bash -x`, inspect ~/.ssh/unraid_agent*, and verify that SSH is enabled on the Unraid host.

set -euo pipefail

readonly UNRAID_TARGET="root@192.168.57.10"
readonly KEY_PATH="${HOME}/.ssh/unraid_agent"
readonly PUBKEY_PATH="${KEY_PATH}.pub"
readonly SSH_DIR="${HOME}/.ssh"
readonly SSH_TEST_COMMAND="echo unraid-agent-ssh-ok"

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
    die "Das Kommando '${command_name}' wurde nicht gefunden. Bitte installiere es und starte das Skript erneut."
  fi
}

ensure_prerequisites() {
  # Why this exists:
  # The bootstrap should fail fast with a useful message instead of half-configuring access.
  require_command bash
  require_command ssh
  require_command ssh-keygen
  require_command grep
  require_command chmod
  require_command mkdir
  require_command cat
}

ensure_local_ssh_dir() {
  # Security note:
  # OpenSSH rejects keys when ~/.ssh is too permissive, so we normalize the directory mode every run.
  mkdir -p "${SSH_DIR}"
  chmod 700 "${SSH_DIR}"
}

ensure_key_pair() {
  # Why this exists:
  # The agent must use one dedicated key pair and must never overwrite an existing private key.
  if [[ -f "${KEY_PATH}" && -f "${PUBKEY_PATH}" ]]; then
    log_info "Dedizierter Agent-Key ist bereits vorhanden: ${KEY_PATH}"
  elif [[ -f "${KEY_PATH}" && ! -f "${PUBKEY_PATH}" ]]; then
    log_warn "Private Key vorhanden, Public Key fehlt. Public Key wird lokal aus dem bestehenden Private Key rekonstruiert."
    ssh-keygen -y -f "${KEY_PATH}" > "${PUBKEY_PATH}"
  elif [[ ! -f "${KEY_PATH}" && -f "${PUBKEY_PATH}" ]]; then
    die "Es existiert nur ${PUBKEY_PATH}, aber nicht ${KEY_PATH}. Bitte prüfe den Schlüsselbestand, damit kein unvollständiger Zustand verwendet wird."
  else
    log_info "Kein dedizierter Agent-Key gefunden. Erzeuge neuen Ed25519-Key unter ${KEY_PATH}"
    ssh-keygen -t ed25519 -f "${KEY_PATH}" -N "" -C "unraid-agent@local" >/dev/null
  fi

  chmod 600 "${KEY_PATH}"
  chmod 644 "${PUBKEY_PATH}"
}

run_ssh_test() {
  # Why this exists:
  # A successful non-interactive login proves that the dedicated key works and keeps repeated runs idempotent.
  ssh \
    -i "${KEY_PATH}" \
    -o BatchMode=yes \
    -o StrictHostKeyChecking=accept-new \
    "${UNRAID_TARGET}" \
    "${SSH_TEST_COMMAND}"
}

install_with_ssh_copy_id() {
  # Why this exists:
  # ssh-copy-id is the safest default because it knows how to install public keys without touching the private key.
  log_info "ssh-copy-id gefunden. Verwende den bevorzugten Installationsweg."
  ssh-copy-id -i "${PUBKEY_PATH}" -o StrictHostKeyChecking=accept-new "${UNRAID_TARGET}"
}

install_with_manual_fallback() {
  local public_key

  public_key="$(cat "${PUBKEY_PATH}")"
  [[ -n "${public_key}" ]] || die "Der Public Key in ${PUBKEY_PATH} ist leer. Bitte prüfe die Datei."

  log_info "Verwende manuellen SSH-Fallback für die Installation des Public Keys."

  # Security note:
  # The remote script receives only the public key as an argument. The private key never leaves the local host.
  ssh -o StrictHostKeyChecking=accept-new "${UNRAID_TARGET}" /bin/bash -s -- "${public_key}" <<'EOF'
set -euo pipefail

PUBLIC_KEY="$1"
SSH_DIR="${HOME}/.ssh"
AUTHORIZED_KEYS="${SSH_DIR}/authorized_keys"

mkdir -p "${SSH_DIR}"
chmod 700 "${SSH_DIR}"
touch "${AUTHORIZED_KEYS}"
chmod 600 "${AUTHORIZED_KEYS}"

# Security note:
# grep -Fqx enforces an exact whole-line match so the key is never appended twice and near-matches do not count.
if grep -Fqx "${PUBLIC_KEY}" "${AUTHORIZED_KEYS}"; then
  printf '[INFO] %s\n' "Remote authorized_keys enthält den Agent-Key bereits."
else
  cat >> "${AUTHORIZED_KEYS}" <<KEY_EOF
${PUBLIC_KEY}
KEY_EOF
  chmod 600 "${AUTHORIZED_KEYS}"
  printf '[INFO] %s\n' "Remote authorized_keys wurde um den Agent-Key ergänzt."
fi
EOF
}

print_final_test_command() {
  printf '\n%s\n' "Finaler Testbefehl:"
  printf "%s\n" "ssh -i ~/.ssh/unraid_agent -o BatchMode=yes -o StrictHostKeyChecking=accept-new root@192.168.57.10 'echo unraid-agent-ssh-ok'"
}

main() {
  ensure_prerequisites
  ensure_local_ssh_dir
  ensure_key_pair

  log_info "Prüfe, ob der dedizierte Agent-Key bereits für ${UNRAID_TARGET} funktioniert."
  if run_ssh_test >/dev/null 2>&1; then
    log_info "SSH-Zugriff ist bereits korrekt eingerichtet. Es sind keine Änderungen erforderlich."
    print_final_test_command
    exit 0
  fi

  if command -v ssh-copy-id >/dev/null 2>&1; then
    if ! install_with_ssh_copy_id; then
      log_warn "ssh-copy-id ist fehlgeschlagen. Wechsle zum manuellen Fallback."
      install_with_manual_fallback || die "Der manuelle Fallback ist fehlgeschlagen. Für die Erstinstallation ist Passwort-Login oder ein bereits bestehender Zugang erforderlich."
    fi
  else
    log_info "ssh-copy-id ist nicht verfügbar. Nutze den manuellen Fallback."
    install_with_manual_fallback || die "Der manuelle Fallback ist fehlgeschlagen. Für die Erstinstallation ist Passwort-Login oder ein bereits bestehender Zugang erforderlich."
  fi

  log_info "Führe abschließenden nicht-interaktiven SSH-Test aus."
  if run_ssh_test >/dev/null 2>&1; then
    log_info "SSH-Bootstrap erfolgreich abgeschlossen."
    print_final_test_command
    exit 0
  fi

  die "Der SSH-Test nach der Installation ist fehlgeschlagen. Prüfe, ob SSH auf Unraid aktiviert ist und ob root@192.168.57.10 erreichbar ist."
}

main "$@"
