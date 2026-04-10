#!/usr/bin/env bash
# Purpose: Replace the plain agent public-key entry on the Unraid root account with a restricted authorized_keys rule.
# Input/Output: Looks for ~/.ssh/unraid_agent.pub locally, rewrites the matching remote /root/.ssh/authorized_keys line with forced-command restrictions, and stays idempotent.
# Important invariants: Hardening is never automatic, only the exact agent key is modified, and unknown option variants are rejected for manual review.
# How to debug: If hardening fails, inspect the remote authorized_keys entry for the agent key and compare it with the expected hardened prefix below.

set -euo pipefail

readonly UNRAID_TARGET="root@192.168.57.10"
readonly PUBKEY_PATH="${HOME}/.ssh/unraid_agent.pub"
readonly AUTHORIZED_KEY_PREFIX='command="/boot/config/custom/agent-deploy.sh",no-agent-forwarding,no-port-forwarding,no-X11-forwarding,no-pty'

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

require_command() {
  local command_name="$1"

  if ! command -v "${command_name}" >/dev/null 2>&1; then
    die "Das Kommando '${command_name}' wurde nicht gefunden. Bitte installiere es und starte das Skript erneut."
  fi
}

ensure_prerequisites() {
  require_command bash
  require_command ssh
  require_command grep
  require_command cat
}

ensure_public_key_exists() {
  [[ -f "${PUBKEY_PATH}" ]] || die "Public Key ${PUBKEY_PATH} wurde nicht gefunden. Bitte zuerst ./scripts/setup_unraid_ssh.sh ausführen."
}

main() {
  local public_key

  ensure_prerequisites
  ensure_public_key_exists

  public_key="$(cat "${PUBKEY_PATH}")"
  [[ -n "${public_key}" ]] || die "Der Public Key in ${PUBKEY_PATH} ist leer. Bitte prüfe die Datei."

  log_info "Härtung des Agent-Keys auf ${UNRAID_TARGET} wird vorbereitet."

  # Security note:
  # The remote script only rewrites the exact matching public-key line.
  # If the key already has unknown options, the script aborts instead of making assumptions.
  ssh -o StrictHostKeyChecking=accept-new "${UNRAID_TARGET}" /bin/bash -s -- "${public_key}" "${AUTHORIZED_KEY_PREFIX}" <<'EOF'
set -euo pipefail

PUBLIC_KEY="$1"
AUTHORIZED_KEY_PREFIX="$2"
SSH_DIR="${HOME}/.ssh"
AUTHORIZED_KEYS="${SSH_DIR}/authorized_keys"
HARDENED_LINE="${AUTHORIZED_KEY_PREFIX} ${PUBLIC_KEY}"

mkdir -p "${SSH_DIR}"
chmod 700 "${SSH_DIR}"
touch "${AUTHORIZED_KEYS}"
chmod 600 "${AUTHORIZED_KEYS}"

if grep -Fqx "${HARDENED_LINE}" "${AUTHORIZED_KEYS}"; then
  printf '[INFO] %s\n' "Der Agent-Key ist bereits mit Forced Command gehärtet."
  exit 0
fi

if ! grep -Fq "${PUBLIC_KEY}" "${AUTHORIZED_KEYS}"; then
  printf '[ERROR] %s\n' "Der Agent-Key wurde in authorized_keys nicht gefunden. Bitte zuerst das SSH-Bootstrap-Skript ausführen." >&2
  exit 1
fi

if ! grep -Fqx "${PUBLIC_KEY}" "${AUTHORIZED_KEYS}"; then
  printf '[ERROR] %s\n' "Der Agent-Key existiert bereits mit anderen Optionen. Automatische Umschreibung wird aus Sicherheitsgründen verweigert." >&2
  exit 1
fi

NEW_CONTENT=""
FOUND_MATCH="false"

# Why this exists:
# The file is rebuilt line by line so only the one exact agent-key line changes and all other entries remain untouched.
while IFS= read -r line || [[ -n "${line}" ]]; do
  if [[ "${line}" == "${PUBLIC_KEY}" ]]; then
    NEW_CONTENT+="${HARDENED_LINE}"$'\n'
    FOUND_MATCH="true"
  else
    NEW_CONTENT+="${line}"$'\n'
  fi
done < "${AUTHORIZED_KEYS}"

if [[ "${FOUND_MATCH}" != "true" ]]; then
  printf '[ERROR] %s\n' "Die erwartete Agent-Key-Zeile konnte nicht sicher ersetzt werden." >&2
  exit 1
fi

cat > "${AUTHORIZED_KEYS}" <<KEY_EOF
${NEW_CONTENT}
KEY_EOF
chmod 600 "${AUTHORIZED_KEYS}"

printf '[INFO] %s\n' "Der Agent-Key wurde mit Forced Command und Forwarding-Sperren gehärtet."
EOF

  log_info "Härtung abgeschlossen. Künftige SSH-Aufrufe mit diesem Key unterliegen dem Forced Command."
}

main "$@"
