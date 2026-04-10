# Unraid Deployment

## Empfohlene Host-Pfade

- `/mnt/user/appdata/feberdin-agent-team/data`
- `/mnt/user/appdata/feberdin-agent-team/reports`
- `/mnt/user/appdata/feberdin-agent-team/workspace`
- `/mnt/user/appdata/feberdin-agent-team/staging-stacks`

## Schritte

1. Repo auf Unraid bereitstellen
2. `.env` aus `.env.example` erzeugen
3. `./scripts/unraid/install-appdata.sh`
4. `docker compose up --build -d`

## Installation direkt aus Git

Für eine hostseitige Installation aus einem Git-Repo liegt [scripts/unraid/install-from-git.sh](/Users/joachim.stiegler/CodingFamily/scripts/unraid/install-from-git.sh) bei.

Beispiel:

```bash
PROJECT_ROOT=/mnt/user/appdata/feberdin-agent-team \
REPO_URL=https://github.com/OWNER/REPO.git \
REPO_REF=main \
AUTO_START_STACK=false \
bash ./scripts/unraid/install-from-git.sh
```

Verhalten:

- legt den dedizierten Projektpfad unter `PROJECT_ROOT` an
- erstellt `data`, `reports`, `workspace` und `staging-stacks`
- klont oder aktualisiert das Repo in `PROJECT_ROOT/repo`
- erzeugt `.env` aus `.env.example`, wenn sie noch fehlt
- startet den Stack nur, wenn `AUTO_START_STACK=true` gesetzt ist

## Installation per Unraid XML Template

Für Nutzer, die lieber aus der Unraid-Weboberfläche starten, liegt eine Bootstrap-Vorlage unter [infra/unraid/templates/feberdin-agent-bootstrap.xml](/Users/joachim.stiegler/CodingFamily/infra/unraid/templates/feberdin-agent-bootstrap.xml).

So nutzt du sie:

1. Öffne in Unraid `Docker -> Add Container`
2. wechsle auf `Advanced View`
3. importiere oder kopiere die XML-Vorlage
4. setze diese Felder:
   - `Project Root`: `/mnt/user/appdata/feberdin-agent-team`
   - `Bootstrap Script URL`: Raw-URL zu `scripts/unraid/install-from-git.sh` in deinem Git
   - `Repository URL`: Git-Clone-URL deines Repos
   - `Repository Ref`: meist `main`
   - `Auto Start Stack`: `false` für den sicheren ersten Lauf
5. mounte `/var/run/docker.sock` nur dann, wenn `Auto Start Stack=true` wirklich gewünscht ist

Hinweis:

- Unraid-Template-XMLs sind für einzelne Container gedacht.
- Diese XML ist deshalb absichtlich nur ein Bootstrap-Container und nicht die eigentliche Laufzeitdefinition des gesamten Multi-Agent-Stacks.
- Die eigentliche Anwendung läuft weiterhin aus dem geklonten Projekt unter `PROJECT_ROOT/repo`.

## Staging-Prinzip

- Staging ist ein separater Git-Checkout
- Deploy-Worker zieht den gewünschten Branch per SSH auf den Staging-Host
- `docker compose up -d --build` aktualisiert nur Staging
- Rollback erfolgt mit `scripts/unraid/rollback-staging.sh`

## Logs

- Docker-Logs über Unraid UI
- Task-Reports unter `HOST_REPORTS_DIR/<task-id>/`
- dauerhafte Entscheidungen unter `HOST_DATA_DIR/memory/`
