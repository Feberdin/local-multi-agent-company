# Troubleshooting

## Preflight zuerst

- `bash ./scripts/doctor.sh`
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

- `bash ./scripts/doctor.sh`
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

- `bash ./scripts/doctor.sh`

## Secret-Dateien nicht lesbar

Typisches Symptom:

- `PermissionError` auf `/run/project-secrets/...`
- Orchestrator oder Worker starten nur im Restart-Loop

Ursache:

- der Secret-Ordner ist fuer `PUID` und `PGID` nicht lesbar
- ein `root:root`-Ordner mit `700` und Dateien mit `600` blockiert Container, die als `99:100` laufen

Fix auf Unraid:

```bash
chown -R 99:100 /mnt/user/appdata/feberdin-agent-team/secrets
chmod 750 /mnt/user/appdata/feberdin-agent-team/secrets
chmod 640 /mnt/user/appdata/feberdin-agent-team/secrets/*
```

Aktuelles Verhalten:

- fehlende oder nicht lesbare optionale Secret-Dateien blockieren den Start nicht mehr automatisch
- der Dienst loggt stattdessen eine Warnung und arbeitet ohne diesen Wert weiter
- wenn ein bestimmter Key fuer einen externen Dienst wirklich Pflicht ist, erscheint der Folgefehler erst an der konkreten Integrationsstelle

Die `.env` darf jeden Schlüssel nur einmal enthalten. Doppelte Schlüssel werden jetzt als Fehler behandelt, damit Port- und Pfadkonflikte nicht still überdeckt werden.

## Worker startet nicht

- `docker compose ps`
- `docker compose logs -f <service>`
- Healthcheck-URL des betroffenen Workers prüfen

## Modellaufrufe schlagen fehl

- `MISTRAL_BASE_URL` und `QWEN_BASE_URL` prüfen
- Routing-Datei und Worker-Zuordnung prüfen
- Erreichbarkeit des lokalen OpenAI-kompatiblen Endpoints verifizieren

Typische Timeout-Symptome:

- Task bleibt lange in `REQUIREMENTS`, `REVIEWING` oder `DOCUMENTING`
- `httpx.ReadTimeout` oder `httpcore.ReadTimeout` in Worker-Logs
- der Orchestrator meldet spaeter einen Worker-Timeout oder eine fehlgeschlagene Stage
- die Task-Detailansicht zeigt Heartbeats fuer denselben Schritt ueber mehrere Minuten

Empfohlene Gegenmaßnahmen:

- für leichtere Stufen `mistral-small3.2:latest` bevorzugen
- `qwen3.5:35b-a3b` nur für schwerere Reasoning-Stufen oder explizite Overrides verwenden
- `LLM_READ_TIMEOUT_SECONDS` und `LLM_REQUEST_DEADLINE_SECONDS` prüfen
- `WORKER_STAGE_TIMEOUT_SECONDS` groesser als die realistische laengste Modelllaufzeit halten
- `STAGE_HEARTBEAT_INTERVAL_SECONDS` nicht zu hoch setzen, damit der Fortschritt sichtbar bleibt
- Worker-Logs prüfen:
  - `docker compose logs -f fmac-req`
  - `docker compose logs -f fmac-rsch`
  - `docker compose logs -f fmac-orch`

Hinweis für schwächere Hardware:

- starte konservativ mit `DEFAULT_MODEL_PROVIDER=mistral`
- lasse `requirements`, `reviewer` und `documentation` auf Mistral
- aktiviere Qwen nur dort, wo die zusätzliche Tiefe den höheren Laufzeitpreis wirklich rechtfertigt
- sinnvolle Startwerte sind oft:
  - `LLM_CONNECT_TIMEOUT_SECONDS=30`
  - `LLM_READ_TIMEOUT_SECONDS=1200`
  - `LLM_WRITE_TIMEOUT_SECONDS=60`
  - `LLM_POOL_TIMEOUT_SECONDS=60`
  - `LLM_REQUEST_DEADLINE_SECONDS=1500`
  - `WORKER_STAGE_TIMEOUT_SECONDS=1800`

## Logging-Fehler mit `service`

Typisches Symptom:

- `ValueError: Formatting field not found in record: 'service'`
- ausgelöst durch `httpx`, `httpcore`, `anyio` oder andere Fremdlogger

Ursache:

- der Formatter erwartete bisher Felder wie `service` und `task_id`, die Third-Party-Logger nicht automatisch setzen

Fix:

- das Logging ergänzt diese Felder jetzt zentral für alle `LogRecord`s
- dadurch bleiben strukturierte Logs erhalten, ohne dass Fremdlogger den Stack destabilisieren

## Web-UI zeigt lange Stage nicht mehr als Freeze

Aktuelles Verhalten:

- die Task-Ansicht aktualisiert sich waehrend aktiver Stages automatisch
- die aktuelle Stage zeigt Worker, Startzeit, Laufzeit und letzte Aktivitaet
- Heartbeat-Ereignisse machen sichtbar, dass ein langsamer lokaler Modellaufruf noch lebt

Pruefen:

- Task im Browser oeffnen und etwa 15 bis 30 Sekunden offen lassen
- schauen, ob neue Heartbeat-Ereignisse auftauchen
- parallel:
  - `docker compose logs -f fmac-orch`
  - `docker compose logs -f fmac-req`

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
