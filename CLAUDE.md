# Claude Code – Arbeitsregeln für dieses Repository

## Commit-Workflow

**Immer vor jedem Commit:**

1. `/Library/Frameworks/Python.framework/Versions/3.11/bin/ruff check . --fix` ausführen
2. Prüfen ob noch Fehler übrig sind: `ruff check .`
3. Verbleibende Fehler manuell beheben und Schritt 2 wiederholen bis `All checks passed!`
4. Erst dann `git commit` und `git push`

Der `.githooks/pre-commit` Hook erzwingt das automatisch. Trotzdem immer selbst
prüfen, damit Fehler nicht erst beim Hook auffallen.

## Allgemeine Regeln

- Änderungen minimal-invasiv halten: nur anfassen was für das Ziel nötig ist
- Keine Refactorings, Kommentare oder Docstrings an unverändertem Code
- Existierende Muster und Namenskonventionen beibehalten
- Kein automatischer Push ohne explizite Bestätigung des Users
