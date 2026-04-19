#!/bin/sh
# Purpose: Run the focused local AI output contract suite for LLM routing, probe prompts, and worker fallback outputs.
# Input/Output: Executes py_compile, ruff, and pytest for the files that exercise AI-facing worker behavior.
# Important invariants: Stops on first failure so the first contract regression stays easy to diagnose.
# How to debug: If a step fails, re-run the printed command directly and inspect the corresponding worker or test file.

set -eu

PYTHON_BIN="${PYTHON_BIN:-./.venv/bin/python}"
RUFF_BIN="${RUFF_BIN:-./.venv/bin/ruff}"
PYTEST_BIN="${PYTEST_BIN:-./.venv/bin/pytest}"

if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="python3"
fi
if [ ! -x "$RUFF_BIN" ]; then
  RUFF_BIN="ruff"
fi
if [ ! -x "$PYTEST_BIN" ]; then
  PYTEST_BIN="pytest"
fi

"$PYTHON_BIN" -m py_compile \
  services/shared/agentic_lab/llm.py \
  services/shared/agentic_lab/worker_probe_service.py \
  services/reviewer_worker/app.py \
  services/security_worker/app.py \
  services/documentation_worker/app.py \
  tests/unit/test_llm.py \
  tests/unit/test_worker_probe_service.py \
  tests/unit/test_ai_output_contracts.py

"$RUFF_BIN" check \
  services/shared/agentic_lab/llm.py \
  services/shared/agentic_lab/worker_probe_service.py \
  services/reviewer_worker/app.py \
  services/security_worker/app.py \
  services/documentation_worker/app.py \
  tests/unit/test_llm.py \
  tests/unit/test_worker_probe_service.py \
  tests/unit/test_ai_output_contracts.py

"$PYTEST_BIN" -q \
  tests/unit/test_llm.py \
  tests/unit/test_worker_probe_service.py \
  tests/unit/test_ai_output_contracts.py
