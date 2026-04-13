#!/bin/sh
# Purpose: Update one Unraid checkout to one exact commit and rebuild the stack with visible build metadata.
# Input/Output: Waits until the target commit exists on origin, fast-forwards local `main` exactly to that commit,
#               exports build metadata for Docker, and runs `docker compose up -d --build --force-recreate`.
# Important invariants:
#   - The working tree must stay clean so no local operator changes are overwritten.
#   - Local `main` is moved only via fast-forward to the requested commit, never to a guessed newer head.
#   - The exported build metadata becomes visible in the Web-UI badge after the rebuild.
# How to debug:
#   - Run `git fetch origin` and `git cat-file -e <sha>^{commit}` manually if waiting never finishes.
#   - Run `git merge --ff-only <sha>` manually if the script says the local branch cannot be fast-forwarded.

set -eu

TARGET_COMMIT="${1:?missing target commit sha}"
TARGET_BRANCH="${2:-main}"
WAIT_TIMEOUT_SECONDS="${WAIT_TIMEOUT_SECONDS:-300}"
POLL_INTERVAL_SECONDS="${POLL_INTERVAL_SECONDS:-5}"

if [ ! -d ".git" ]; then
  echo "update-to-commit: this script must run inside an existing git checkout." >&2
  exit 1
fi

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "update-to-commit: local changes detected. Bitte erst committen oder sichern." >&2
  exit 1
fi

wait_for_target_commit() {
  start_epoch="$(date +%s)"
  while :; do
    if git fetch origin "${TARGET_BRANCH}" >/dev/null 2>&1; then
      remote_ref="refs/remotes/origin/${TARGET_BRANCH}"
      if git show-ref --verify --quiet "${remote_ref}" \
        && git cat-file -e "${TARGET_COMMIT}^{commit}" 2>/dev/null \
        && git merge-base --is-ancestor "${TARGET_COMMIT}" "${remote_ref}" 2>/dev/null; then
        return 0
      fi
    fi

    now_epoch="$(date +%s)"
    elapsed="$((now_epoch - start_epoch))"
    if [ "${elapsed}" -ge "${WAIT_TIMEOUT_SECONDS}" ]; then
      echo "update-to-commit: target commit ${TARGET_COMMIT} wurde innerhalb von ${WAIT_TIMEOUT_SECONDS}s nicht auf origin/${TARGET_BRANCH} sichtbar." >&2
      exit 1
    fi

    echo "update-to-commit: warte auf ${TARGET_COMMIT} auf origin/${TARGET_BRANCH} (${elapsed}s/${WAIT_TIMEOUT_SECONDS}s) ..."
    sleep "${POLL_INTERVAL_SECONDS}"
  done
}

wait_for_target_commit

TARGET_FULL_SHA="$(git rev-parse "${TARGET_COMMIT}^{commit}")"

git checkout "${TARGET_BRANCH}"
git merge --ff-only "${TARGET_FULL_SHA}"

HEAD_FULL_SHA="$(git rev-parse HEAD)"
if [ "${HEAD_FULL_SHA}" != "${TARGET_FULL_SHA}" ]; then
  echo "update-to-commit: HEAD ist ${HEAD_FULL_SHA}, erwartet wurde ${TARGET_FULL_SHA}." >&2
  exit 1
fi

BUILD_COMMIT_SHA="$(git rev-parse --short=12 HEAD)"
BUILD_GIT_REF="${TARGET_BRANCH}"
BUILD_BUILT_AT_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
export BUILD_COMMIT_SHA BUILD_GIT_REF BUILD_BUILT_AT_UTC

echo "update-to-commit: baue Commit ${BUILD_COMMIT_SHA} von ${BUILD_GIT_REF} (Build-Zeit ${BUILD_BUILT_AT_UTC})."
docker compose up -d --build --force-recreate

echo "update-to-commit: fertig. Aktueller Commit: ${BUILD_COMMIT_SHA}"
