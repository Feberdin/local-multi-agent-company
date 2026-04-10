# Konfiguration

## Wichtige `.env`-Werte

- `HOST_DATA_DIR`
- `HOST_REPORTS_DIR`
- `HOST_WORKSPACE_ROOT`
- `HOST_STAGING_STACK_ROOT`
- `HOST_SECRETS_DIR`
- `GITHUB_TOKEN`
- `GITHUB_TOKEN_FILE`
- `MISTRAL_BASE_URL`
- `QWEN_BASE_URL`
- `MISTRAL_MODEL_NAME`
- `QWEN_MODEL_NAME`
- `MODEL_API_KEY_FILE`
- `MISTRAL_API_KEY_FILE`
- `QWEN_API_KEY_FILE`
- `LLM_TIMEOUT_CONNECT_SECONDS`
- `LLM_TIMEOUT_READ_SECONDS`
- `LLM_TIMEOUT_WRITE_SECONDS`
- `LLM_TIMEOUT_POOL_SECONDS`
- `LLM_REQUEST_DEADLINE_SECONDS`
- `WORKER_TIMEOUT_CONNECT_SECONDS`
- `WORKER_TIMEOUT_READ_SECONDS`
- `WORKER_TIMEOUT_WRITE_SECONDS`
- `WORKER_TIMEOUT_POOL_SECONDS`
- `WORKER_RETRY_ATTEMPTS`
- `WEB_SEARCH_API_KEY_FILE`
- `BRAVE_SEARCH_API_KEY`
- `BRAVE_SEARCH_API_KEY_FILE`
- `MODEL_ROUTING_CONFIG`
- `ORCHESTRATOR_PORT`
- `WEB_UI_PORT`
- `STAGING_*`

Wichtige Defaults:

- `ORCHESTRATOR_PORT=18080`
- `WEB_UI_PORT=18088`
- `DEFAULT_MODEL_PROVIDER=mistral`
- `LLM_TIMEOUT_READ_SECONDS=60`
- `LLM_REQUEST_DEADLINE_SECONDS=90`
- `WORKER_TIMEOUT_READ_SECONDS=240`

Regel:

- Jeder Schlüssel darf in `.env` nur einmal vorkommen. Doppelte Einträge werden vom Runtime-Doctor und von den Services als Fehler behandelt.

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

Hinweis:

- Wenn deine lokalen Modelle ohne Auth laufen, bleiben `MODEL_API_KEY`, `MISTRAL_API_KEY` und `QWEN_API_KEY` leer.
- `BRAVE_SEARCH_API_KEY` wird nur benötigt, wenn Brave wirklich aktiviert wird.

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

Empfohlene Standardverteilung:

- `requirements`, `reviewer`, `documentation`, `qa` und sonstige leichte Hilfsstufen bevorzugen `mistral-small3.2:latest`
- `research`, `architecture`, `coding`, `security` und `validation` duerfen `qwen3.5:35b-a3b` nutzen
- Unbekannte oder neue Worker fallen konservativ auf den sicheren Default statt automatisch auf das groessere Modell

Timeout-Hinweise:

- `LLM_TIMEOUT_*` steuern die HTTP-Transportgrenzen zum OpenAI-kompatiblen Endpoint
- `LLM_REQUEST_DEADLINE_SECONDS` begrenzt jeden einzelnen Modellaufruf zusaetzlich pro Stage
- `request_timeout_seconds` im Routing kann pro Worker enger oder grosszuegiger gesetzt werden
- `WORKER_TIMEOUT_*` steuern, wie lange der Orchestrator auf einen Worker warten darf

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
