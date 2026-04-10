# Contributing

## Zweck

Diese Anleitung beschreibt, wie Änderungen am Multi-Agent-System nachvollziehbar, testbar und sicher eingereicht werden.

## Entwicklungsablauf

1. Repo klonen und `.env.example` nach `.env` kopieren.
2. Abhängigkeiten installieren:

   ```bash
   python3 -m pip install -e ".[dev]"
   ```

3. Tests lokal ausführen:

   ```bash
   ./scripts/run-local-tests.sh
   ```

4. Services lokal starten oder per Compose hochfahren.
5. Branch nach dem Muster `feature/<slug>`, `fix/<slug>` oder `chore/<slug>` verwenden.

## Code- und Review-Regeln

- Kleine, nachvollziehbare Commits statt Big-Bang-Änderungen.
- Kommentare erklären Absicht und Risikostellen.
- Neue Guardrails, Statusübergänge und Worker-Verträge bekommen Tests.
- Änderungen an Deploy-Skripten, Secrets oder Infra verlangen eine zusätzliche manuelle Prüfung.

## Style

- Python: `ruff check .`, `mypy services`, `pytest`.
- Shell: defensiv mit `set -euo pipefail`.
- YAML/Markdown: klare, kurze Kommentare nur dort, wo Betriebslogik sonst unklar wäre.
