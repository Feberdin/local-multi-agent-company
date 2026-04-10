# Unraid Deployment

## Empfohlene Host-Pfade

- `/mnt/user/appdata/feberdin-agent-team/data`
- `/mnt/user/appdata/feberdin-agent-team/reports`
- `/mnt/user/appdata/feberdin-agent-team/workspace`
- `/mnt/user/appdata/feberdin-agent-team/staging-stacks`
- `/mnt/user/appdata/feberdin-agent-team/secrets`

## Standard-Ports

- Orchestrator: `18080 -> 8080`
- Web UI: `18088 -> 8088`

Wenn auf dem Host bereits andere Dienste auf diesen Ports laufen, ändere `ORCHESTRATOR_PORT` und `WEB_UI_PORT` in `.env`, bevor du den Stack startest.

## Schritte

1. Repo auf Unraid bereitstellen
2. `.env` aus `.env.example` erzeugen
3. `./scripts/unraid/install-appdata.sh`
4. `bash ./scripts/doctor.sh`
5. `docker compose up -d --build --force-recreate`

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
- akzeptiert auch einen bereits vorhandenen, aber leeren Ordner `PROJECT_ROOT/repo`
- erzeugt `.env` aus `.env.example`, wenn sie noch fehlt
- erstellt auch den Secret-Ordner unter `PROJECT_ROOT/secrets`
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

## Preflight und Mount-Prüfung

Der wichtigste Vorab-Check ist jetzt [scripts/doctor.sh](/Users/joachim.stiegler/CodingFamily/scripts/doctor.sh). Das Skript bricht mit einer klaren Fehlermeldung ab, wenn:

- `HOST_STAGING_STACK_ROOT` fehlt
- `HOST_STAGING_STACK_ROOT` nicht beschreibbar ist
- das finale Compose-Modell keinen Mount nach `/staging-stacks` enthält
- `ORCHESTRATOR_PORT` oder `WEB_UI_PORT` bereits belegt sind
- `.env` doppelte Schlüssel enthält

Manuelle Kontrolle des finalen Compose-Modells:

```bash
bash ./scripts/doctor.sh
docker compose config
```

Wenn du den Mount gezielt prüfen willst:

```bash
docker compose config | grep -n "/staging-stacks"
```

Nach dem Start kannst du zusätzlich die aktiven Mounts eines Containers prüfen:

```bash
docker inspect <container-name> --format '{{json .Mounts}}'
```

## Logs

- Docker-Logs über Unraid UI
- Task-Reports unter `HOST_REPORTS_DIR/<task-id>/`
- dauerhafte Entscheidungen unter `HOST_DATA_DIR/memory/`
