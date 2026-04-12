# Konfiguration

## Wichtige `.env`-Werte

- `HOST_DATA_DIR`
- `HOST_REPORTS_DIR`
- `HOST_WORKSPACE_ROOT`
- `HOST_STAGING_STACK_ROOT`
- `HOST_SECRETS_DIR`
- `RUNTIME_HOME_DIR`
- `TASK_WORKSPACE_ROOT`
- `GITHUB_TOKEN`
- `GITHUB_TOKEN_FILE`
- `MISTRAL_BASE_URL`
- `QWEN_BASE_URL`
- `MISTRAL_MODEL_NAME`
- `QWEN_MODEL_NAME`
- `MODEL_API_KEY_FILE`
- `MISTRAL_API_KEY_FILE`
- `QWEN_API_KEY_FILE`
- `LLM_CONNECT_TIMEOUT_SECONDS`
- `LLM_READ_TIMEOUT_SECONDS`
- `LLM_WRITE_TIMEOUT_SECONDS`
- `LLM_POOL_TIMEOUT_SECONDS`
- `LLM_REQUEST_DEADLINE_SECONDS`
- `WORKER_CONNECT_TIMEOUT_SECONDS`
- `WORKER_STAGE_TIMEOUT_SECONDS`
- `WORKER_WRITE_TIMEOUT_SECONDS`
- `WORKER_POOL_TIMEOUT_SECONDS`
- `WORKER_RETRY_ATTEMPTS`
- `STAGE_HEARTBEAT_INTERVAL_SECONDS`
- `WEB_SEARCH_API_KEY_FILE`
- `BRAVE_SEARCH_API_KEY`
- `BRAVE_SEARCH_API_KEY_FILE`
- `MODEL_ROUTING_CONFIG`
- `ORCHESTRATOR_PORT`
- `WEB_UI_PORT`
- `STAGING_*`
- `SELF_IMPROVEMENT_*`

Wichtige Defaults:

- `ORCHESTRATOR_PORT=18080`
- `WEB_UI_PORT=18088`
- `DEFAULT_MODEL_PROVIDER=mistral`
- `LLM_READ_TIMEOUT_SECONDS=1200`
- `LLM_REQUEST_DEADLINE_SECONDS=1500`
- `WORKER_STAGE_TIMEOUT_SECONDS=1800`
- `STAGE_HEARTBEAT_INTERVAL_SECONDS=30`
- `RUNTIME_HOME_DIR=/tmp/agent-home`
- `TASK_WORKSPACE_ROOT=/workspace/.task-workspaces`

## Self-Improvement

Die Self-Improvement-Funktion arbeitet nur am eigenen Repository `Feberdin/local-multi-agent-company` und nutzt dafuer dieselbe Task-, Branch- und Worker-Architektur wie normale Aufgaben.

Wichtige Schalter:

- `SELF_IMPROVEMENT_ENABLED`
- `SELF_IMPROVEMENT_MODE`
- `SELF_IMPROVEMENT_POLICY_PATH`
- `SELF_IMPROVEMENT_MAX_AUTO_FIX_ATTEMPTS`
- `SELF_IMPROVEMENT_MAX_CYCLES_PER_DAY`
- `SELF_IMPROVEMENT_DEPLOY_AFTER_SUCCESS`
- `SELF_IMPROVEMENT_REQUIRE_APPROVAL_FOR_RISKY`
- `SELF_IMPROVEMENT_PREFLIGHT_REQUIRED`
- `SELF_IMPROVEMENT_AUTO_ROLLBACK`
- `SELF_IMPROVEMENT_TARGET_REPO`
- `SELF_IMPROVEMENT_LOCAL_REPO_PATH`
- `SELF_UPDATE_WATCHDOG_POLL_SECONDS`
- `SELF_UPDATE_WATCHDOG_TIMEOUT_SECONDS`

E-Mail- und Approval-Schalter:

- `SELF_IMPROVEMENT_EMAIL_ENABLED`
- `SELF_IMPROVEMENT_EMAIL_TO`
- `SELF_IMPROVEMENT_EMAIL_FROM`
- `SELF_IMPROVEMENT_EMAIL_REPLY_TO`
- `SELF_IMPROVEMENT_SMTP_HOST`
- `SELF_IMPROVEMENT_SMTP_PORT`
- `SELF_IMPROVEMENT_SMTP_USERNAME`
- `SELF_IMPROVEMENT_SMTP_PASSWORD`
- `SELF_IMPROVEMENT_SMTP_PASSWORD_FILE`
- `SELF_IMPROVEMENT_SMTP_USE_STARTTLS`
- `SELF_IMPROVEMENT_EMAIL_TIMEOUT_SECONDS`

Empfohlene Startwerte:

```env
SELF_IMPROVEMENT_ENABLED=false
SELF_IMPROVEMENT_MODE=assisted
SELF_IMPROVEMENT_POLICY_PATH=/app/config/self-improvement.policy.yaml
SELF_IMPROVEMENT_MAX_AUTO_FIX_ATTEMPTS=3
SELF_IMPROVEMENT_MAX_CYCLES_PER_DAY=5
SELF_IMPROVEMENT_DEPLOY_AFTER_SUCCESS=false
SELF_IMPROVEMENT_REQUIRE_APPROVAL_FOR_RISKY=true
SELF_IMPROVEMENT_PREFLIGHT_REQUIRED=true
SELF_IMPROVEMENT_AUTO_ROLLBACK=true
SELF_IMPROVEMENT_TARGET_REPO=Feberdin/local-multi-agent-company
SELF_IMPROVEMENT_LOCAL_REPO_PATH=/workspace/local-multi-agent-company
SELF_IMPROVEMENT_EMAIL_ENABLED=false
SELF_IMPROVEMENT_EMAIL_TO=
SELF_IMPROVEMENT_EMAIL_FROM=
```

Policy-Datei:

- Standardpfad: [config/self-improvement.policy.yaml](/Users/joachim.stiegler/CodingFamily/config/self-improvement.policy.yaml)
- regelt pro Modus und Risikostufe:
  - Aktion
  - E-Mail-Intent
  - Deploy-Erlaubnis
  - Publish-Approval

Wichtige Semantik:

- `LOW`
  - darf je nach Modus autonom laufen
- `MEDIUM`
  - darf autonom laufen, aber mit zusaetzlicher Information an Operatoren
- `HIGH`
  - darf Branch, Tests und Artefakte vorbereiten, pausiert aber vor riskanter Veroeffentlichung
- `CRITICAL`
  - bleibt analysebasiert und benoetigt explizite Freigabe

E-Mail-Verhalten:

- Jede Approval-/Info-Mail wird zuerst im Outbox-Ordner `DATA_DIR/self-improvement-email-outbox` gespeichert.
- Wenn SMTP fehlt oder unvollstaendig ist, wird das nicht als Crash behandelt.
- Das Dashboard zeigt dann `queued`, `skipped`, `failed` oder `sent`.

Rollback-Verhalten:

- Bei fehlgeschlagenen Self-Improvement-Tasks kann das System einen Incident anlegen und einen Rollback-Task erzeugen.
- Der Rollback nutzt einen deterministischen `git revert`-Pfad im dedizierten `rollback-worker`.
- Self-Updates werden vor dem Host-Restart von einem Watchdog im `rollback-worker` bewaffnet.
- Der Watchdog speichert Heartbeats und Endstatus unter `DATA_DIR/self-update-watchdogs/`.
- Der Rollback-Task bleibt voll auditierbar und erscheint im Incident-Bereich des Dashboards.

Regel:

- Jeder Schlüssel darf in `.env` nur einmal vorkommen. Doppelte Einträge werden vom Runtime-Doctor und von den Services als Fehler behandelt.

Hinweis zur Rueckwaertskompatibilitaet:

- Die frueheren Variablennamen wie `LLM_TIMEOUT_READ_SECONDS` und `WORKER_TIMEOUT_READ_SECONDS` werden weiterhin akzeptiert.
- Fuer neue Setups sollten nur noch die neuen Namen aus dieser Datei verwendet werden, damit die Konfiguration leichter lesbar bleibt.

## Empfohlener lokaler Secret-Store

Für Unraid ist ein projektgebundener Secret-Ordner sinnvoll:

- Host: `/mnt/user/appdata/feberdin-agent-team/secrets`
- Container: `/run/project-secrets`

Empfehlung:

- ein Secret pro Datei
- der Container-User aus `PUID` und `PGID` muss den Ordner lesen und betreten koennen
- sichere Startwerte fuer `PUID=99` und `PGID=100`:
  - `chown -R 99:100 /mnt/user/appdata/feberdin-agent-team/secrets`
  - `chmod 750 /mnt/user/appdata/feberdin-agent-team/secrets`
  - `chmod 640 /mnt/user/appdata/feberdin-agent-team/secrets/*`
- Klartextwerte nur dort ablegen, nicht im Git-Repo

Beispiel:

```bash
mkdir -p /mnt/user/appdata/feberdin-agent-team/secrets
chown -R 99:100 /mnt/user/appdata/feberdin-agent-team/secrets
chmod 750 /mnt/user/appdata/feberdin-agent-team/secrets
printf '%s' 'ghp_xxx' > /mnt/user/appdata/feberdin-agent-team/secrets/github_token
chmod 640 /mnt/user/appdata/feberdin-agent-team/secrets/github_token
```

Dann in `.env`:

```env
HOST_SECRETS_DIR=/mnt/user/appdata/feberdin-agent-team/secrets
GITHUB_TOKEN_FILE=/run/project-secrets/github_token
MISTRAL_API_KEY_FILE=/run/project-secrets/mistral_api_key
QWEN_API_KEY_FILE=/run/project-secrets/qwen_api_key
WEB_SEARCH_API_KEY_FILE=/run/project-secrets/web_search_api_key
BRAVE_SEARCH_API_KEY_FILE=/run/project-secrets/brave_search_api_key
```

## Git und Task-Workspaces

Fuer gemountete Repositories auf Unraid oder anderen Self-Hosted Hosts gelten zwei wichtige Regeln:

- Git braucht immer ein beschreibbares `HOME`, damit `safe.directory` und andere globale Einstellungen sauber gesetzt werden koennen.
- Worker sollten niemals direkt auf dem geteilten Basis-Checkout unter `/workspace/<repo>` arbeiten, wenn mehrere Tasks oder bereits lokale Aenderungen im Spiel sind.

Der Stack nutzt dafuer jetzt diese Defaults:

- `RUNTIME_HOME_DIR=/tmp/agent-home`
- `TASK_WORKSPACE_ROOT=/workspace/.task-workspaces`

Verhalten:

- der gemeinsame Checkout unter `DEFAULT_LOCAL_REPO_PATH` bleibt die Quelle
- jeder Task bekommt darunter eine eigene isolierte Arbeitskopie
- Git-Operationen setzen `safe.directory` pro konkret genutztem Repo-Pfad automatisch
- spaetere Stages wie `coding`, `reviewer`, `tester` und `github` arbeiten danach auf demselben task-isolierten Checkout weiter

Warum das wichtig ist:

- `fatal: detected dubious ownership` verschwindet nicht durch Glueck, sondern durch ein gueltiges HOME plus `safe.directory`
- ein bereits dirty geteilter Checkout kann sonst neue Tasks verunreinigen
- isolierte Task-Workspaces machen Fehler reproduzierbarer und sicherer

## Trusted Sources

- Seed-Datei: [config/trusted_sources.coding_profile.json](/Users/joachim.stiegler/CodingFamily/config/trusted_sources.coding_profile.json)
- Laufzeit-Persistenz: `DATA_DIR/trusted_sources.json`
- Verwaltung im Dashboard unter `Trusted Sources`

Unterstützt:

- Profile mit aktivem Profilwechsel
- Quellen hinzufügen, bearbeiten, deaktivieren, löschen
- JSON Import/Export
- Dry-Run: welche Quelle würde gewählt
- Quellentest inkl. Connectivity-Check

## Web Search Provider

- Seed-Datei: [config/web_search.providers.json](/Users/joachim.stiegler/CodingFamily/config/web_search.providers.json)
- Laufzeit-Persistenz: `DATA_DIR/web_search_providers.json`
- Verwaltung im Dashboard unter `Web Search Providers`

Sichere Defaults:

- `SearXNG` als vorgesehener primärer Provider
- `Brave` als optionaler Fallback
- beide initial deaktiviert
- Trusted Sources behalten Vorrang
- SearXNG nutzt standardmaessig `GET /search` mit `format=json`, `categories=general`, `language=auto` und `safesearch=0`

Hinweis:

- Der Compose-Stack startet aktuell keinen eigenen SearXNG-Container. Trage deshalb eine bestehende SearXNG-Instanz in `base_url` und `search_path` ein oder lasse den Provider deaktiviert.
- Ein typischer JSON-Endpunkt ist `http://<host>:8080/search` oder `https://<host>/search`.
- SearXNG muss fuer Maschinenzugriffe JSON aktiviert haben:

```yaml
search:
  formats:
    - html
    - json
```

- Wenn der normale `/search?q=test`-Check funktioniert, aber `/search?q=test&format=json` mit `403` scheitert, fehlt oft genau diese JSON-Freigabe oder eine API-Zugriffsbeschraenkung.
- Wenn deine lokalen Modelle ohne Auth laufen, bleiben `MODEL_API_KEY`, `MISTRAL_API_KEY` und `QWEN_API_KEY` leer.
- `BRAVE_SEARCH_API_KEY` wird nur benötigt, wenn Brave wirklich aktiviert wird.
- Laut der oeffentlichen Brave-Preisuebersicht vom 11. April 2026 solltest du fuer neue Setups von kostenpflichtigen Search-/Answers-Tarifen mit monatlichem Guthaben ausgehen. Plane Brave daher bewusst als optionalen Bezahl-Fallback ein, nicht als kostenlosen Pflichtbaustein.

## Worker Guidance

- Seed-Datei: [config/worker_guidance.defaults.json](/Users/joachim.stiegler/CodingFamily/config/worker_guidance.defaults.json)
- Laufzeit-Persistenz: `DATA_DIR/worker_guidance.json`
- Vorschlags-Persistenz: `DATA_DIR/improvement_suggestions.json`

Im Dashboard pflegbar:

- Handlungsempfehlungen pro Worker
- Entscheidungspräferenzen
- Kompetenzgrenzen
- automatische Einreichung von Mitarbeiterideen

## Modellrouting

- Worker-Routing wird in [config/model-routing.example.yaml](/Users/joachim.stiegler/CodingFamily/config/model-routing.example.yaml) definiert.
- Pro Worker konfigurierbar:
  - `primary_provider`
  - `fallback_provider`
  - `temperature`
  - `max_tokens`
  - `budget_tokens`
  - `request_timeout_seconds`
  - `reasoning`
  - `output_contract`
  - `routing_note`

Empfohlene Standardverteilung:

- Strukturierte Worker mit JSON-, Schema-, Patch- oder Dateioperations-Output bevorzugen `mistral-small3.2:latest`
  - dazu gehoeren standardmaessig `requirements`, `cost`, `human_resources`, `coding`, `reviewer`, `tester`, `qa`, `security`, `validation`, `data`, `memory`, `github` und `deploy`
- Semantisch tiefere Analyse- und Architektur-Worker bevorzugen `qwen3.5:35b-a3b`
  - dazu gehoeren standardmaessig `research` und `architecture`
- `ux` bleibt gemischt, bevorzugt aber standardmaessig `qwen3.5:35b-a3b` mit strukturiertem Fallback
- `documentation` bleibt auf gut lesbare Textausgabe optimiert und nutzt standardmaessig `mistral-small3.2:latest`
- Unbekannte oder neue Worker fallen konservativ auf den sicheren Default statt automatisch auf das groessere Modell

Warum diese Verteilung sinnvoll ist:

- `Qwen` ist lokal oft staerker bei Recherche, Einordnung, Architektur und semantisch reicheren Aufgaben.
- `Mistral` ist im Stack robuster, wenn Worker strikt parsebare JSON-, Schema- oder Patch-Ausgaben liefern muessen.
- Structured Worker sollen deshalb nicht an freier Prosa, `thinking`-Feldern oder kaputten Dateioperationen scheitern.

Output-Haertung:

- Der LLM-Client liest bei OpenAI-kompatiblen Antworten primaer `choices[0].message.content`.
- Wenn ein Backend stattdessen Ollama-aehnliche Antworten liefert, werden auch `message.content`, `response` und `content` sauber ausgewertet.
- Separate `thinking`- oder `reasoning`-Felder werden nur noch als Diagnoseinformation betrachtet und nicht als eigentlicher Nutzoutput.
- Bei strukturierten Workern erzwingt `output_contract` einen strengeren JSON-Hinweis, einen Reparaturversuch und bei Bedarf einen Fallback auf das andere Modell.

Timeout-Hinweise:

- `LLM_*_TIMEOUT_SECONDS` steuern die HTTP-Transportgrenzen zum OpenAI-kompatiblen Endpoint
- `LLM_REQUEST_DEADLINE_SECONDS` begrenzt jeden einzelnen Modellaufruf zusaetzlich pro Stage
- `request_timeout_seconds` im Routing kann pro Worker enger oder grosszuegiger gesetzt werden
- `WORKER_STAGE_TIMEOUT_SECONDS` begrenzt die gesamte Wartezeit des Orchestrators auf eine Worker-Stage
- `WORKER_*_TIMEOUT_SECONDS` fuer Connect/Write/Pool steuern den HTTP-Transport zum Worker
- `STAGE_HEARTBEAT_INTERVAL_SECONDS` bestimmt, wie oft laufende Stages sichtbare Heartbeat-Events in den Task schreiben

Empfehlung fuer langsame lokale Hardware:

- starte mit `DEFAULT_MODEL_PROVIDER=mistral`
- lasse strukturierte Worker wie `coding`, `reviewer`, `validation`, `security`, `requirements` und `qa` auf `mistral-small3.2:latest`
- nutze `qwen3.5:35b-a3b` gezielt fuer `research`, `architecture` und semantisch reichere UX- oder Analyse-Stufen
- erhoehe zuerst `LLM_READ_TIMEOUT_SECONDS`, `LLM_REQUEST_DEADLINE_SECONDS` und `WORKER_STAGE_TIMEOUT_SECONDS`, bevor du instabile Workarounds suchst

## GitHub

- `GITHUB_TOKEN` braucht mindestens Repo- und PR-Rechte.
- `GITHUB_TOKEN_FILE` ist die bevorzugte Variante für den produktiven Betrieb dieses Stacks.
- SSH-Remotes für Ziel-Repositories sind für Push/Deploy weiterhin sinnvoll.

## Staging

- `AUTO_DEPLOY_STAGING=true` aktiviert den Staging-Schritt nach GitHub.
- `STAGING_PROJECT_DIR` zeigt auf den bestehenden Staging-Checkout auf Unraid.

## Runtime-Doctor

- Skript: [scripts/doctor.sh](/Users/joachim.stiegler/CodingFamily/scripts/doctor.sh)
- Prüft vor dem Start:
  - Host-Verzeichnisse
  - Schreibrechte
  - doppelte `.env`-Schlüssel
  - finale Compose-Mounts
  - Portkonflikte

Beispiel:

```bash
bash ./scripts/doctor.sh
```

## Security

- Externe Inhalte bleiben untrusted.
- Keine Secrets im Repo.
- Review- und Security-Risikoflags führen zu Freigabepunkten.
- GitHub-Repositories müssen zusätzlich in der Dashboard-Allowlist explizit freigegeben werden.
- Repository-Änderungen brauchen eine ausdrückliche Bestätigung, bevor der Coding-Worker schreibt.
