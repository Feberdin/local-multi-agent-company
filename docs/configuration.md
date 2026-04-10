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
- `WEB_SEARCH_API_KEY_FILE`
- `MODEL_ROUTING_CONFIG`
- `STAGING_*`

## Empfohlener lokaler Secret-Store

Für Unraid ist ein projektgebundener Secret-Ordner sinnvoll:

- Host: `/mnt/user/appdata/feberdin-agent-team/secrets`
- Container: `/run/project-secrets`

Empfehlung:

- ein Secret pro Datei
- `chmod 700` auf dem Ordner
- `chmod 600` auf jeder Secret-Datei
- Klartextwerte nur dort ablegen, nicht im Git-Repo

Beispiel:

```bash
mkdir -p /mnt/user/appdata/feberdin-agent-team/secrets
chmod 700 /mnt/user/appdata/feberdin-agent-team/secrets
printf '%s' 'ghp_xxx' > /mnt/user/appdata/feberdin-agent-team/secrets/github_token
chmod 600 /mnt/user/appdata/feberdin-agent-team/secrets/github_token
```

Dann in `.env`:

```env
HOST_SECRETS_DIR=/mnt/user/appdata/feberdin-agent-team/secrets
GITHUB_TOKEN_FILE=/run/project-secrets/github_token
MISTRAL_API_KEY_FILE=/run/project-secrets/mistral_api_key
QWEN_API_KEY_FILE=/run/project-secrets/qwen_api_key
WEB_SEARCH_API_KEY_FILE=/run/project-secrets/web_search_api_key
```

## Modellrouting

- Worker-Routing wird in [config/model-routing.example.yaml](/Users/joachim.stiegler/CodingFamily/config/model-routing.example.yaml) definiert.
- Pro Worker konfigurierbar:
  - `primary_provider`
  - `fallback_provider`
  - `temperature`
  - `max_tokens`
  - `budget_tokens`
  - `reasoning`

## GitHub

- `GITHUB_TOKEN` braucht mindestens Repo- und PR-Rechte.
- `GITHUB_TOKEN_FILE` ist die bevorzugte Variante für den produktiven Betrieb dieses Stacks.
- SSH-Remotes für Ziel-Repositories sind für Push/Deploy weiterhin sinnvoll.

## Staging

- `AUTO_DEPLOY_STAGING=true` aktiviert den Staging-Schritt nach GitHub.
- `STAGING_PROJECT_DIR` zeigt auf den bestehenden Staging-Checkout auf Unraid.

## Security

- Externe Inhalte bleiben untrusted.
- Keine Secrets im Repo.
- Review- und Security-Risikoflags führen zu Freigabepunkten.
