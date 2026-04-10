# Self-hosted Runner Setup

Empfohlene Labels:

- `self-hosted`
- `linux`
- `unraid`
- `staging`

Empfohlene Rollen:

- Runner nur für CI, Staging-Deploy und Smoke-Tests.
- Keine produktiven Deployments auf demselben Runner.
- SSH-Key oder Deploy-Key nur mit minimal nötigen Rechten.

Vorbereitung:

1. `./scripts/github/prepare-runner.sh`
2. `docker compose -f infra/unraid/runner/docker-compose.runner.yml up -d`
3. Runner im gewünschten Repo oder der gewünschten Org registrieren.
