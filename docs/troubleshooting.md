# Troubleshooting

## Preflight zuerst

- `./scripts/doctor.sh`
- `docker compose config`

Wenn der Doctor fehlschlägt, zuerst diesen Fehler beheben. Viele Runtime-Probleme sind in Wirklichkeit Konfigurations- oder Mount-Probleme.

## PermissionError auf `/staging-stacks`

Typisches Symptom:

- `PermissionError: [Errno 13] Permission denied: '/staging-stacks'`

Ursache:

- `HOST_STAGING_STACK_ROOT` fehlt auf dem Host
- der Pfad ist nicht beschreibbar
- der finale Compose-Mount nach `/staging-stacks` fehlt, oft durch ein überschriebenes `volumes:` in einem Service

Prüfen:

- `./scripts/doctor.sh`
- `docker compose config | grep -n "/staging-stacks"`
- `docker inspect <container-name> --format '{{json .Mounts}}'`

## Orchestrator unhealthy oder nicht erreichbar

Typische Ursachen:

- Host-Port-Konflikt auf `ORCHESTRATOR_PORT`
- fehlender Daten- oder Staging-Mount
- Fehler beim Starten eines abhängigen Services

Prüfen:

- `docker compose ps`
- `docker compose logs -f orchestrator`
- `curl http://localhost:18080/health`

Wenn `18080` bereits belegt ist, in `.env` einen anderen Wert setzen und den Stack mit `docker compose up -d --build --force-recreate` neu starten.

## Doppelte Einträge in `.env`

Typisches Symptom:

- Werte verhalten sich widersprüchlich
- Compose nutzt andere Ports als erwartet
- Services zeigen unerwartete Pfade an

Prüfen:

- `./scripts/doctor.sh`

Die `.env` darf jeden Schlüssel nur einmal enthalten. Doppelte Schlüssel werden jetzt als Fehler behandelt, damit Port- und Pfadkonflikte nicht still überdeckt werden.

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
