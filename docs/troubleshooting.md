# Troubleshooting

## Worker startet nicht

- `docker compose ps`
- `docker compose logs -f <service>`
- Healthcheck-URL des betroffenen Workers prüfen

## Modellaufrufe schlagen fehl

- `MISTRAL_BASE_URL` und `QWEN_BASE_URL` prüfen
- Routing-Datei und Worker-Zuordnung prüfen
- Erreichbarkeit des lokalen OpenAI-kompatiblen Endpoints verifizieren

## GitHub-PR wird nicht erstellt

- `GITHUB_TOKEN`
- Git-Remote
- Branch existiert und wurde gepusht

## Staging-Deploy scheitert

- SSH-Rechte
- `STAGING_PROJECT_DIR`
- Compose-Datei im Staging-Checkout

## Workflow bleibt auf `APPROVAL_REQUIRED`

- Dashboard öffnen
- Freigabegrund prüfen
- nur nach bewusster Entscheidung fortsetzen
