#!/bin/sh
# Purpose: Run the opt-in live Ollama integration suite directly against the configured model API.
# Input/Output: Executes only the real API smoke tests and prints raw/normalized output previews to stdout.
# Important invariants: The suite stays tiny and must be enabled explicitly via RUN_OLLAMA_LIVE_TESTS=1.
# How to debug: If a model hangs or returns a weird shape, inspect the printed provider preview and re-run one test with -s.

set -eu

export PYTHONWARNINGS="${PYTHONWARNINGS:-ignore::DeprecationWarning}"

PYTEST_BIN="${PYTEST_BIN:-./.venv/bin/pytest}"
if [ ! -x "$PYTEST_BIN" ]; then
  PYTEST_BIN="pytest"
fi

RUN_OLLAMA_LIVE_TESTS=1 "$PYTEST_BIN" -q -s tests/integration/test_ollama_live_api.py
