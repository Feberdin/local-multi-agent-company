# Workflows

## Standard-Workflow

1. `requirements`
2. `cost`
3. `human_resources`
4. `research`
5. `architecture`
6. optional `data`
7. optional `ux`
8. `coding`
9. `reviewer`
10. `tester`
11. `security`
12. `validation`
13. `documentation`
14. `github`
15. optional `deploy`
16. optional `qa`
17. `memory`

## Research-Routing

Vor allgemeiner Websuche gilt jetzt:

1. Trusted official API / Registry
2. Trusted official documentation
3. SearXNG
4. Brave
5. transparenter Abbruch

Der Research-Worker dokumentiert:

- aktives Trusted-Source-Profil
- inferierten Fragetyp
- inferiertes Ökosystem
- gewählte Quellen
- Fallback-Grund
- verwendeten Provider, falls nötig

## Governance und Entscheidungstransparenz

- Vor jedem Worker-Lauf injiziert der Orchestrator die konfigurierte Worker-Guidance.
- Nach jedem Worker-Lauf ergänzt der Orchestrator:
  - `applied_guidance`
  - `decision_tree`
  - optionale `Mitarbeiterideen`
- Verbesserungsvorschläge laufen nie automatisch los, sondern erscheinen im Geschäftsführer-Dashboard.

## Resume-Verhalten

- Jeder Statuswechsel wird als Event und Snapshot gespeichert.
- `APPROVAL_REQUIRED` pausiert den Workflow.
- Nach Freigabe setzt der Orchestrator am `resume_target` fort.

## Approval-Strategie

- Riskante Review- oder Security-Funde stoppen vor GitHub-Push/PR.
- Deployments nach Staging sind optional.
- Produktion bleibt außerhalb des automatisierten Flows.

## GitHub-Flow

- branch-basiert
- Draft-PR als Standard
- GitHub Actions für CI und PR-Prüfung
- optionaler Self-hosted Runner für Staging auf Unraid
