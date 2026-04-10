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
