#!/bin/sh
# Purpose: Run the local validation suite for this repository.
# Input/Output: Executes shell syntax checks, ruff, pytest, and mypy in a deterministic order.
# Important invariants: The script stops on first failure so the earliest issue stays obvious.
# How to debug: If a step fails because a tool is missing, install the dev dependencies from CONTRIBUTING.md.

set -eu

bash -n scripts/bootstrap.sh
bash -n scripts/setup_unraid_ssh.sh
bash -n scripts/harden_unraid_authorized_key.sh
bash -n scripts/unraid/install-from-git.sh
python3 -c "import xml.etree.ElementTree as ET; ET.parse('infra/unraid/templates/feberdin-agent-bootstrap.xml')"
python3 -m ruff check .
python3 -m pytest -q
python3 -m mypy services
