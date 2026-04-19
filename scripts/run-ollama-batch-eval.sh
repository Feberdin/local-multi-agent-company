#!/bin/sh
# Purpose: Run a 100-probe live batch evaluation directly against the configured Ollama API endpoints.
# Input/Output: Executes `scripts/ollama_batch_eval.py` and prints a compact provider summary plus report path.
# Important invariants: This script is intentionally live and should only be used when the Ollama host is reachable.
# How to debug: If it fails, inspect the saved JSON report under `reports/ollama-batch-eval-latest.json`.

set -eu

export PYTHONWARNINGS="${PYTHONWARNINGS:-ignore::DeprecationWarning}"

PYTHON_BIN="${PYTHON_BIN:-./.venv/bin/python}"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="python3"
fi

"$PYTHON_BIN" -u scripts/ollama_batch_eval.py
