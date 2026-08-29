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
  - `WORKER_STAGE_TIMEOUT_SECONDS=3600`

## Git meldet `detected dubious ownership`

Typisches Symptom:

- `fatal: detected dubious ownership in repository at '/workspace/<repo>'`
- haeufig in `coding-worker`, `research-worker` oder spaeter im `github-worker`

Ursache:

- das Repository ist als Host-Mount im Container sichtbar
- Git vertraut diesem Pfad ohne `safe.directory` nicht automatisch
- zusaetzlich fehlt in manchen Self-Hosted Setups ein gueltiges, beschreibbares `HOME`

Fix im Stack:

- Worker setzen jetzt ein eigenes beschreibbares Laufzeit-`HOME`
- `safe.directory` wird fuer den konkret genutzten Repo-Pfad automatisch und idempotent gesetzt
- neue Tasks arbeiten nicht mehr direkt auf dem gemeinsamen Basis-Checkout

Pruefen:

- `docker compose logs --tail=200 coding-worker`
- `docker compose logs --tail=200 research-worker`
- im Debug-Center die Runtime-Pfade `runtime_home_dir` und `task_workspace_root` ansehen

Wichtige `.env`-Werte:

- `RUNTIME_HOME_DIR=/tmp/agent-home`
- `TASK_WORKSPACE_ROOT=/workspace/.task-workspaces`

## Ein gemeinsames dirty Repo fuehrt zu unerwarteten Worker-Fehlern

Typisches Symptom:

- `git status` zeigt bereits vor der eigentlichen Coding-Stage viele lokale Aenderungen
- neue Tasks verhalten sich inkonsistent oder diffen gegen alte Task-Reste

Ursache:

- mehrere Tasks oder manuelle Arbeiten teilen sich denselben Checkout unter `/workspace/<repo>`
- lokale Aenderungen aus einem frueheren Task landen dadurch im naechsten

Fix im Stack:

- jeder Task bekommt jetzt eine isolierte Arbeitskopie unter `TASK_WORKSPACE_ROOT`
- der gemeinsame Checkout bleibt nur noch Quelle fuer saubere Task-Kopien
- spaetere Worker-Stages arbeiten innerhalb derselben Task-Kopie weiter

Konkreter Hinweis:

- wenn ein alter Task noch mit einem historischen gemeinsamen Checkout angelegt wurde, starte am besten eine neue Aufgabe nach dem Update
- neue Aufgaben sollten im Detail-View einen `local_repo_path` unter `.task-workspaces/<task-id>/...` verwenden

## Logging-Fehler mit `service`

Typisches Symptom:

- `ValueError: Formatting field not found in record: 'service'`
- ausgelöst durch `httpx`, `httpcore`, `anyio` oder andere Fremdlogger

Ursache:

- der Formatter erwartete bisher Felder wie `service` und `task_id`, die Third-Party-Logger nicht automatisch setzen

Fix:

- das Logging ergänzt diese Felder jetzt zentral für alle `LogRecord`s
- dadurch bleiben strukturierte Logs erhalten, ohne dass Fremdlogger den Stack destabilisieren

## SearXNG-Provider liefert `404 Not Found`

Typisches Symptom:

- Providertest im Dashboard meldet `404 Not Found`
- in der Fehlermeldung taucht ein Endpoint wie `http://<host>:8081/search?...` auf

Ursache:

- `base_url` oder `search_path` zeigen nicht auf einen echten SearXNG-JSON-Endpunkt
- der Stack startet standardmaessig keinen eigenen SearXNG-Container
- haeufig antwortet ein Reverse Proxy, ein Platzhalter-Webserver oder eine andere App auf diesem Port

Pruefen:

- im Dashboard unter `Web Search Providers` den kompletten Endpoint kontrollieren
- typische Ziel-URL: `http://<searxng-host>:8080/search`
- direkt testen:
  - `curl -I http://<searxng-host>:8080/search`
  - `curl "http://<searxng-host>:8080/search?q=python&format=json"`

Empfehlung:

- wenn du noch keine eigene SearXNG-Instanz betreibst, lass den Provider deaktiviert
- oder trage bewusst eine bestehende externe Instanz ein

## SearXNG antwortet mit `403 Forbidden` auf `format=json`

Typisches Symptom:

- `GET /search?q=test` funktioniert oder die Browser-Suche funktioniert sichtbar
- der JSON-Healthcheck oder ein Providertest mit `format=json` liefert `403 Forbidden`

Haeufige Ursache:

- in der SearXNG-`settings.yml` ist JSON nicht unter `search.formats` aktiviert
- oder die Instanz bzw. ein vorgeschalteter Proxy blockiert API-Aufrufe

Pflicht-Konfiguration:

```yaml
search:
  formats:
    - html
    - json
```

Pruefen:

- `curl "http://<searxng-host>/search?q=test"`
- `curl "http://<searxng-host>/search?q=test&format=json&categories=general&language=auto&safesearch=0"`

Interpretation:

- HTML ok, JSON ok:
  - die Instanz ist fuer Worker nutzbar
- HTML ok, JSON 403:
  - SearXNG ist erreichbar, aber die JSON-API ist nicht aktiv oder gesperrt
- HTML und JSON beide kaputt:
  - Base-URL, Port, Reverse Proxy oder SearXNG selbst pruefen

## Web-UI zeigt lange Stage nicht mehr als Freeze

Aktuelles Verhalten:

## Self-Improvement wartet auf Freigabe

Typisches Symptom:

- im Dashboard steht der Zyklus auf `awaiting_manual_review`
- es gibt keinen neuen Commit oder PR
- unter `Offene Freigaben` erscheint der Zyklus weiter

Erwartetes Verhalten:

- das System blockiert nicht blind, sondern wartet bewusst an einem Governance-Gate
- andere sichere Aufgaben koennen weiterlaufen

Pruefen:

- `http://<host>:18088/self-improvement`
- `curl http://localhost:18080/api/self-improvement/status`
- `curl http://localhost:18080/api/settings/self-improvement/policy`

Wichtige Felder:

- `governance_status`
- `governance_action`
- `current_gate_name`
- `current_gate_reason`
- `approval_email_status`

## Self-Improvement-Mail wurde nicht versendet

Typisches Symptom:

- im Dashboard steht `approval_email_status=queued`, `skipped` oder `failed`

Bedeutung:

- `skipped`
  - E-Mail-Versand ist deaktiviert
- `queued`
  - Outbox-Datei wurde geschrieben, aber SMTP ist noch nicht voll konfiguriert
- `failed`
  - SMTP wurde versucht, ist aber technisch fehlgeschlagen
- `sent`
  - Nachricht wurde versendet und zusaetzlich protokolliert

Pruefen:

- Outbox-Ordner:
  - `ls -lah <DATA_DIR>/self-improvement-email-outbox`
- SMTP-Konfiguration in `.env`
- Secret-Datei fuer `SELF_IMPROVEMENT_SMTP_PASSWORD_FILE`, falls genutzt

Wichtig:

- fehlende SMTP-Konfiguration ist absichtlich kein harter Runtime-Fehler
- die Nachricht bleibt trotzdem als JSON im Outbox-Ordner verfuegbar

## Self-Improvement hat einen Incident oder Rollback erzeugt

Typisches Symptom:

- ein Zyklus ist fehlgeschlagen
- im Dashboard erscheint ein Incident mit `rollback_running`
- ein weiterer Task mit Rollback-Ziel wurde angelegt

Bedeutung:

- das System hat den fehlerhaften Self-Improvement-Lauf erkannt
- der Vorfall wurde dauerhaft protokolliert
- falls moeglich wurde automatisch ein Rollback-Task vorbereitet

Pruefen:

- `http://<host>:18088/self-improvement`
- `curl http://localhost:18080/api/self-improvement/incidents`
- `docker compose logs --tail=200 rollback-worker`
- `docker compose logs --tail=200 orchestrator`

Technischer Hinweis:

- der Rollback-Task nutzt einen deterministischen `git revert`-Pfad im `rollback-worker`
- der Self-Update-Watchdog bleibt beim Host-Restart absichtlich auf dem alten Container aktiv
- wenn kein Commit-SHA vorliegt, kann nur ein Incident erzeugt werden, aber kein automatischer Revert

- die Task-Ansicht aktualisiert sich waehrend aktiver Stages automatisch
- die aktuelle Stage zeigt Worker, Startzeit, Laufzeit und letzte Aktivitaet
- Heartbeat-Ereignisse machen sichtbar, dass ein langsamer lokaler Modellaufruf noch lebt
- das `Worker-Theater` zeigt den aktiven Worker mit Denkblase und abgeschlossene oder blockierte Worker mit Sprechblasen

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

## Alle Tasks enden auf `FAILED`

Typische Symptome:

- mehrere Aufgaben bleiben kurz in `REQUIREMENTS` oder `Wartet auf den naechsten Schritt`
- die Detailansicht zeigt nur den letzten Fehler
- Docker-Logs sind nicht sofort vollstaendig greifbar oder die UI war zwischendurch degradiert

Empfohlener Ablauf:

- `Debug-Center` oeffnen:
  - `http://<unraid-host>:18088/debug`
- betroffene Task auswaehlen
- zuerst `Task-Bundle herunterladen`
- wenn unklar ist, ob auch die Umgebung selbst instabil war:
  - `Alles zusammen herunterladen`

Das Bundle enthaelt:

- System-Snapshots vom Orchestrator
- Task-Detail, Event-Historie, Worker-Ergebnisse und Suggestions
- vorhandene Report-Dateien unter `REPORTS_DIR/<task-id>`
- persistierte Runtime-Dateien aus `DATA_DIR`, soweit vorhanden
- eine Textdatei mit Host-Befehlen fuer zusaetzliche Docker-Logs

Wichtig:

- Docker-Host-Logs selbst sind nicht im ZIP, weil die Web-UI keinen Docker-Socket mountet
- falls du sie zusaetzlich brauchst, im Debug-Center die `Host-Log-Befehle` mitnehmen und auf Unraid ausfuehren

## Nur den fehlerhaften Teilbereich neu starten

Typisches Symptom:

- ein Task ist an `Recherche`, `Coding` oder einer spaeteren Stage gescheitert
- die vorherigen Schritte waren aber bereits sinnvoll und sollen nicht komplett neu erzeugt werden

Vorgehen:

- die Task-Detailseite der betroffenen Aufgabe oeffnen
- den Bereich `Teilbereich neu starten` verwenden
- den fruehesten Schritt waehlen, der neu laufen soll
- optional kurz notieren, was behoben wurde

Aktuelles Verhalten:

- derselbe Task bleibt erhalten
- Event-Historie und Audit-Trail bleiben sichtbar
- zurueckgesetzt werden nur die gewaehlte Stage und alle nachfolgenden Worker-Ergebnisse
- der Neustart laeuft danach direkt wieder im Hintergrund an

Hinweis:

- wenn der Fehler in `Coding` lag, ist oft `Recherche` oder `Architektur` der sinnvollere Neustartpunkt, falls sich der technische Kontext geaendert hat

## Research-Worker endet mit HTTP 500

Typische Symptome:

- `latest_error` enthaelt nur `Worker at http://research-worker:8091 failed after 3 attempts`
- unter `/reports/<task-id>` fehlen `research-notes.md`
- Requirements, Cost und HR haben schon Reports geschrieben, Research aber nicht

Haeufige Ursache:

- der bestehende Workspace-Checkout ist nicht sauber genug fuer `git fetch/checkout/pull`
- frueher fuehrte das zu einem nackten 500er aus dem Research-Worker

Aktuelles Verhalten:

- der Research-Worker versucht zuerst weiter normal zu aktualisieren
- wenn nur das Refresh des bestehenden Checkouts scheitert, arbeitet er jetzt lesend mit dem vorhandenen Repo weiter
- falls trotzdem etwas Unerwartetes passiert, liefert er einen normalen Worker-Fehler mit echter Ursache statt eines stillen 500ers

Pruefen:

- `docker compose logs --tail=200 research-worker`
- `docker compose logs --tail=200 orchestrator`
- im Debug-Center das Task-Bundle der betroffenen Aufgabe herunterladen

## Brave Search ist optional und inzwischen praktisch kostenpflichtig

Wichtig:

- Brave wird in diesem Stack standardmaessig nur als optionaler Fallback behandelt
- der Provider ist ab Werk deaktiviert
- ohne `BRAVE_SEARCH_API_KEY` kann Brave nicht serverseitig genutzt werden
- laut der oeffentlichen Brave-Preisuebersicht vom 11. April 2026 solltest du fuer neue Setups von kostenpflichtigen Search-/Answers-Tarifen mit monatlichem Guthaben ausgehen
- fuer lokale Hobby-Stacks ist es oft sinnvoller, Brave deaktiviert zu lassen und nur Trusted Sources oder eine eigene SearXNG-Instanz zu verwenden
