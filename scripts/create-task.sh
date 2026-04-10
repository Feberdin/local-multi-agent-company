#!/bin/sh
# Purpose: Submit a new task to the orchestrator API from the terminal.
# Input/Output: Sends a task creation request and prints the JSON response.
# Important invariants: Goal, repository, and local repo path are mandatory.
# How to debug: If the request fails, verify ORCHESTRATOR_URL and that the API is healthy.

set -eu

GOAL="${1:-}"
REPOSITORY="${2:-}"
LOCAL_REPO_PATH="${3:-}"
ORCHESTRATOR_URL="${ORCHESTRATOR_URL:-http://localhost:8080}"

if [ -z "$GOAL" ] || [ -z "$REPOSITORY" ] || [ -z "$LOCAL_REPO_PATH" ]; then
  echo "Usage: ./scripts/create-task.sh <goal> <owner/repo> <local_repo_path>" >&2
  exit 1
fi

curl -sS -X POST "${ORCHESTRATOR_URL}/api/tasks" \
  -H "Content-Type: application/json" \
  -d "{
    \"goal\": \"${GOAL}\",
    \"repository\": \"${REPOSITORY}\",
    \"local_repo_path\": \"${LOCAL_REPO_PATH}\"
  }"
