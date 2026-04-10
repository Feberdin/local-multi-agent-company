# Architektur

## Leitidee

Das System bildet ein lokales Feberdin-Entwicklungsunternehmen nach. Ein Orchestrator nimmt einen Auftrag entgegen, verteilt Arbeit an spezialisierte Worker und führt deren Ergebnisse kontrolliert zusammen. GitHub bleibt die zentrale Wahrheit, Unraid ist die Zielplattform für Staging und lokale Services.

## Pflichtkomponenten

- `orchestrator`
- `requirements-worker`
- `research-worker`
- `architecture-worker`
- `coding-worker`
- `reviewer-worker`
- `test-worker`
- `security-worker`
- `validation-worker`
- `github-worker`
- `documentation-worker`

## Erweiterbare Komponenten

- `deploy-worker`
- `qa-worker`
- `memory-worker`
- `data-worker`
- `ux-worker`
- `cost-worker`
- `human-resources-worker`
- `openhands`
- `github-mcp`

## Datenfluss

1. Auftrag kommt beim Orchestrator an.
2. Requirements werden strukturiert.
3. Modell-/Ressourcenrouting und Worker-Mix werden bewertet.
4. Research sammelt repo-interne und optional externe Grundlagen.
5. Architecture entwirft Komponenten, Schnittstellen und Implementierungsplan.
6. Optionale Spezialisten (`data`, `ux`) liefern Zusatzsicht.
7. Coding implementiert branch-basiert.
8. Review, Testing, Security und Validation prüfen das Ergebnis.
9. Documentation erzeugt verständliche Übergabe.
10. GitHub-Worker erstellt Commit, Push und Draft-PR.
11. Optional: Deploy nach Unraid-Staging plus QA.
12. Memory hält Entscheidungen dauerhaft fest.

## Zustände

- `NEW`
- `REQUIREMENTS`
- `RESOURCE_PLANNING`
- `RESEARCHING`
- `ARCHITECTING`
- `CODING`
- `REVIEWING`
- `TESTING`
- `SECURITY_REVIEW`
- `VALIDATING`
- `DOCUMENTING`
- `PR_CREATED`
- `STAGING_DEPLOYED`
- `QA_PENDING`
- `MEMORY_UPDATING`
- `APPROVAL_REQUIRED`
- `DONE`
- `FAILED`

## Modellrouting

- `Mistral`: leichte Extraktion, Doku, Klassifikation, Kosten-/Teamhinweise
- `Qwen`: Architektur, Coding, Review, Security, Validation
- Routing wird über [config/model-routing.example.yaml](/Users/joachim.stiegler/CodingFamily/config/model-routing.example.yaml) gesteuert

## Freigabepunkte

Pflicht vor:

- riskanten Secret- oder Infrastrukturänderungen
- destruktiven Änderungen
- Produktion
- Merge nach `main`

Im aktuellen Stand greift der Approval-Gate vor GitHub-Publikation, wenn Review oder Security entsprechende Risikoflags setzen.
