# Konfiguration

## Wichtige `.env`-Werte

- `HOST_DATA_DIR`
- `HOST_REPORTS_DIR`
- `HOST_WORKSPACE_ROOT`
- `HOST_STAGING_STACK_ROOT`
- `GITHUB_TOKEN`
- `MISTRAL_BASE_URL`
- `QWEN_BASE_URL`
- `MISTRAL_MODEL_NAME`
- `QWEN_MODEL_NAME`
- `MODEL_ROUTING_CONFIG`
- `STAGING_*`

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
- SSH-Remotes für Ziel-Repositories sind für Push/Deploy weiterhin sinnvoll.

## Staging

- `AUTO_DEPLOY_STAGING=true` aktiviert den Staging-Schritt nach GitHub.
- `STAGING_PROJECT_DIR` zeigt auf den bestehenden Staging-Checkout auf Unraid.

## Security

- Externe Inhalte bleiben untrusted.
- Keine Secrets im Repo.
- Review- und Security-Risikoflags führen zu Freigabepunkten.
