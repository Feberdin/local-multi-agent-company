# Branching Convention

- `feature/<slug>-<taskid>` für neue Features oder größere Verbesserungen.
- `fix/<slug>-<taskid>` für Fehlerbehebungen.
- `chore/<slug>-<taskid>` für Wartung, Doku oder Infrastrukturarbeiten.
- `main` bleibt geschützt und wird nie ungefragt durch den Agenten verändert.

Die Coding-Worker erzeugen standardmäßig `feature/...`-Branches. Andere Präfixe können später per Repo-Profil ergänzt werden.
