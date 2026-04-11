# Feberdin Multi-Agent Company

Lokales, zustandsbehaftetes Multi-Agent-System für Softwareentwicklung, Recherche, Review, Testing, GitHub-Automation und kontrolliertes Staging-Deployment auf Unraid. Der Stack ist so aufgebaut, als würdest du ein Ziel an ein spezialisiertes Team delegieren: Der Orchestrator strukturiert den Auftrag, verteilt Arbeit an Worker, protokolliert Ergebnisse, erzwingt Freigaben und dokumentiert das Resultat in GitHub.

## Kurzüberblick

- Local-first und containerisiert für Unraid
- GitHub als zentrale Wahrheit für Branches, Commits, Issues und PRs
- LangGraph-Orchestrator mit persistiertem Task-State
- Konfigurierbares Modellrouting für `Mistral` und `Qwen`
- Spezialisierte Worker statt unkontrollierter Vollautonomie
- Staging-only Deployment als Standard
- Freigabepunkte vor riskanten Schritten

## Zielarchitektur in Kurzform

- `orchestrator`: nimmt Aufgaben an, steuert Workflow, persistiert Status, erzwingt Freigaben
- `requirements-worker`: extrahiert Anforderungen, Annahmen, Risiken und Akzeptanzkriterien
- `cost-worker`: schätzt Modell- und Ressourcenbedarf
- `human-resources-worker`: empfiehlt passende Worker-Zuschnitte
- `research-worker`: Repo-Analyse und optional untrusted Web-Recherche
- `trusted sources`: offizielle Coding-Quellen, Routing und Fallback-Regeln für den Research-Worker
- `architecture-worker`: Komponenten, Datenflüsse, Modulgrenzen, Implementierungsplan
- `data-worker`: Datenverarbeitungs-Hinweise bei Parsing-/Extraktionsaufgaben
- `ux-worker`: UI-/Flow-Hinweise bei UI-orientierten Zielen
- `coding-worker`: branch-basiertes Coding via `local_patch` oder optional OpenHands
- `reviewer-worker`: Logik-, Stil-, Test- und Architekturreview
- `test-worker`: Linting, Typing, Unit-/Integrationstests
- `security-worker`: Prompt-Injection-, Secret-, Diff- und Dependency-Risiken
- `validation-worker`: prüft Ergebnis gegen Originalauftrag und Akzeptanzkriterien
- `documentation-worker`: erstellt verständliche Handover-/Ops-Zusammenfassungen
- `github-worker`: Commit, Push, Draft-PR
- `deploy-worker`: Staging-Deployment auf Unraid
- `qa-worker`: Smoke-/Health-/API-Checks
- `memory-worker`: speichert Entscheidungen und Learnings dauerhaft
- `web-ui`: Aufgaben, Status, Logs, Freigaben
- Task-Detailseite mit Worker-Theater, Heartbeats und lesbarer Event-Historie fuer lange lokale LLM-Laufzeiten
- Worker-Benchmarkseite mit Laufzeiten, sichtbarem Input/Output, Modellnutzung und Fehlerbildern pro Worker

## Annahmen

- Unraid stellt Docker Compose oder einen kompatiblen Compose-Manager bereit.
- Deine lokalen Modell-Endpunkte sind erreichbar:
  - `MISTRAL_BASE_URL=http://192.168.57.10:11434/v1`
  - `QWEN_BASE_URL=http://192.168.57.10:11434/v1`
- GitHub wird per PAT oder SSH angebunden.
- Ziel-Repositories liegen unter dem gemounteten Workspace oder können dort geklont werden.
- Die Staging-Umgebung auf Unraid besitzt einen separaten Git-Checkout des Zielprojekts.
- Produktive Deployments bleiben außerhalb dieses Stacks und benötigen immer separate Freigabe.

## Was dieses Repo liefert

1. Einen realistisch lauffähigen Startpunkt für ein Feberdin-Multi-Agent-Team
2. Eine modulare Worker-Architektur mit klarer Zuständigkeit pro Rolle
3. Konfigurierbares Modellrouting für kleinere und größere Aufgaben
4. Docker-/Compose-/Unraid-Artefakte inklusive Healthchecks und Volumes
5. GitHub-Workflow-Grundlagen für CI, PRs und Staging
6. Sicherheitsmechanismen als Code-Struktur statt als bloßer Text
7. Tests für Kernlogik, Routing, Guardrails und API-Schnittstellen

## Dateibaum

```text
.
├── .github/
├── config/
├── docker/
├── docs/
├── github/
├── infra/
├── scripts/
├── services/
│   ├── architecture_worker/
│   ├── coding_worker/
│   ├── cost_worker/
│   ├── data_worker/
│   ├── deploy_worker/
│   ├── documentation_worker/
│   ├── github_worker/
│   ├── human_resources_worker/
│   ├── memory_worker/
│   ├── orchestrator/
│   ├── qa_worker/
│   ├── requirements_worker/
│   ├── research_worker/
│   ├── reviewer_worker/
│   ├── security_worker/
│   ├── shared/agentic_lab/
│   ├── test_worker/
│   ├── ux_worker/
│   ├── validation_worker/
│   └── web_ui/
├── tests/
├── .env.example
├── docker-compose.override.yml
├── docker-compose.yml
├── pyproject.toml
└── README.md
```

## Quickstart

1. Konfiguration vorbereiten:

   ```bash
   cp .env.example .env
   ```

2. Host-Pfade und Tokens anpassen:

   - `HOST_DATA_DIR`
   - `HOST_REPORTS_DIR`
   - `HOST_WORKSPACE_ROOT`
   - `GITHUB_TOKEN`
   - `STAGING_*`

3. Ordner bootstrapen:

   ```bash
   ./scripts/bootstrap.sh
   ```

4. Preflight prüfen:

   ```bash
   bash ./scripts/doctor.sh
   ```

5. Stack starten:

   ```bash
   docker compose up -d --build --force-recreate
   ```

6. Dashboard öffnen:

   - `http://<unraid-host>:18088`
   - `http://<unraid-host>:18088/worker-guidance`
   - `http://<unraid-host>:18088/suggestions`
   - `http://<unraid-host>:18088/trusted-sources`
   - `http://<unraid-host>:18088/web-search`
   - `http://<unraid-host>:18088/benchmarks`
   - `http://<unraid-host>:18088/debug`

   Auf der Task-Detailseite zeigt das `Worker-Theater`, welcher Worker gerade denkt, welcher bereits gesprochen hat und wo der Workflow aktuell haengt.
   Unter `Benchmarks` siehst du pro Worker gut lesbar Laufzeit, sichtbaren Auftrag, sichtbares Ergebnis, Fehlerhaeufung und Modellnutzung.
   Wenn nur ein Teilbereich schiefgelaufen ist, kannst du auf derselben Task-Seite unter `Teilbereich neu starten` ab genau dem betroffenen Schritt erneut ansetzen, ohne einen komplett neuen Task anzulegen.
   Im `Debug-Center` kannst du System-Snapshots, persistierte Runtime-Dateien und Task-Reports einzeln oder als ZIP-Bundle herunterladen.

7. Beispiel-Task anlegen:

   ```bash
   ./scripts/create-task.sh \
     "Verbessere ein bestehendes Repo mit sicherem Review- und Staging-Flow" \
     "Feberdin/example-repo" \
     "/workspace/example-repo"
   ```

## SSH Bootstrap für Unraid

Das Projekt enthält ein dediziertes SSH-Bootstrap für den Coding-Agent nach dem Prinzip „eigener Schlüssel pro Zweck“.

- Skript: [scripts/setup_unraid_ssh.sh](/Users/joachim.stiegler/CodingFamily/scripts/setup_unraid_ssh.sh)
- Ziel: installiert `~/.ssh/unraid_agent.pub` auf `root@192.168.57.10`
- Verhalten:
  - erzeugt lokal bei Bedarf einen `ed25519`-Key unter `~/.ssh/unraid_agent`
  - überschreibt keinen bestehenden Key
  - nutzt bevorzugt `ssh-copy-id`
  - verwendet sonst einen idempotenten manuellen Fallback
  - testet anschließend den Login mit dem dedizierten Key

Voraussetzungen:

- Auf Unraid muss SSH aktiviert sein.
- Für die Erstinstallation muss Passwort-Login oder bereits ein bestehender Zugang möglich sein.

Direkter Aufruf:

```bash
./scripts/setup_unraid_ssh.sh
```

Optional beim Projekt-Bootstrap:

```bash
BOOTSTRAP_UNRAID_SSH=true ./scripts/bootstrap.sh
```

Die vollständige Beschreibung inklusive optionaler Härtung per Forced Command findest du in [docs/ssh-bootstrap.md](/Users/joachim.stiegler/CodingFamily/docs/ssh-bootstrap.md).

## Unraid XML Bootstrap

Wenn du das Projekt lieber über eine Unraid-Template-XML anstoßen willst, liegt eine Bootstrap-Vorlage unter [infra/unraid/templates/feberdin-agent-bootstrap.xml](/Users/joachim.stiegler/CodingFamily/infra/unraid/templates/feberdin-agent-bootstrap.xml).

Wichtig:

- Die XML ist bewusst ein Bootstrap-Helfer für ein Multi-Container-Projekt.
- Sie lädt per `curl` das Host-Skript [scripts/unraid/install-from-git.sh](/Users/joachim.stiegler/CodingFamily/scripts/unraid/install-from-git.sh) aus deinem Git-Repo.
- Danach wird das Repo unter `/mnt/user/appdata/feberdin-agent-team/repo` geklont oder aktualisiert.
- Optional kann der Bootstrap-Container anschließend `docker compose up -d` auf dem Unraid-Host ausführen, wenn du den Docker-Socket bewusst mountest und `AUTO_START_STACK=true` setzt.

Die vollständige Anleitung steht in [docs/unraid-deployment.md](/Users/joachim.stiegler/CodingFamily/docs/unraid-deployment.md).

## Kurze Containernamen und Unraid-Icons

Fuer die Unraid-Docker-Ansicht verwendet der Stack jetzt kurze, eindeutige Containernamen wie:

- `fmac-orch`
- `fmac-arch`
- `fmac-code`
- `fmac-rsch`
- `fmac-web`

Zusaetzlich liegt ein kleines SVG-Icon-Set fuer die Worker unter [infra/unraid/icons](/Users/joachim.stiegler/CodingFamily/infra/unraid/icons) bereit. Die Zuordnung steht in [docs/unraid-icons.md](/Users/joachim.stiegler/CodingFamily/docs/unraid-icons.md).

## Runtime-Preflight

Vor jedem Start prüft [scripts/doctor.sh](/Users/joachim.stiegler/CodingFamily/scripts/doctor.sh):

- doppelte Schlüssel in `.env`
- ob `HOST_DATA_DIR`, `HOST_REPORTS_DIR`, `HOST_WORKSPACE_ROOT` und `HOST_STAGING_STACK_ROOT` existieren und beschreibbar sind
- ob das finale Compose-Modell den Bind-Mount nach `/staging-stacks` sowie die übrigen Runtime-Mounts für alle relevanten Services enthält
- ob `ORCHESTRATOR_PORT` und `WEB_UI_PORT` frei sind

Wichtige Defaults:

- `ORCHESTRATOR_PORT=18080`
- `WEB_UI_PORT=18088`

Wichtige Diagnosebefehle:

```bash
bash ./scripts/doctor.sh
docker compose config
docker compose up -d --build --force-recreate
docker compose ps
docker compose logs -f orchestrator
curl http://localhost:18080/health
curl http://localhost:18088/health
```

## Debug-Center

Unter [services/web_ui/templates/debug.html](/Users/joachim.stiegler/CodingFamily/services/web_ui/templates/debug.html) bietet die Web-UI jetzt ein eigenes `Debug-Center`:

- Einzel-Downloads fuer aktuelle API-Snapshots wie Tasks, Repository-Allowlist, Trusted Sources, Worker Guidance und Suggestions
- Direktdownloads fuer persistierte Runtime-Dateien aus `DATA_DIR`, soweit sie vorhanden sind
- Task-spezifische Snapshots wie Task-Detail, Event-Historie, Worker-Ergebnisse und Suggestions
- Roh-Reports aus `REPORTS_DIR/<task-id>`
- ZIP-Bundles fuer `System`, `Task` oder `alles zusammen`

Wichtig:

- Die Bundles enthalten absichtlich keine Secret-Werte.
- Docker-Host-Logs werden nicht direkt mit ausgeliefert, weil die Web-UI keinen Docker-Socket mountet.
- Ein Textfile mit den passenden Host-Befehlen liegt trotzdem im Bundle, damit du fuer Support-Faelle die fehlenden Logs schnell nachziehen kannst.

## Modellrouting

Das Routing liegt in [config/model-routing.example.yaml](/Users/joachim.stiegler/CodingFamily/config/model-routing.example.yaml).

- `Mistral` ist Standard für leichte Extraktion, Doku, Routing-Hinweise und einfache Klassifikation.
- `Qwen` ist fuer die schwereren Stufen wie Architektur, komplexes Coding, Security und Validation reserviert.
- Pro Worker lassen sich definieren:
  - primäres Modell
  - Fallback-Modell
  - Temperatur
  - `max_tokens`
  - Budget-Hinweis
  - Reasoning-Tiefe

Empfohlene Startwerte fuer langsame lokale Ollama-Hardware:

```env
DEFAULT_MODEL_PROVIDER=mistral
LLM_CONNECT_TIMEOUT_SECONDS=30
LLM_READ_TIMEOUT_SECONDS=1200
LLM_WRITE_TIMEOUT_SECONDS=60
LLM_POOL_TIMEOUT_SECONDS=60
LLM_REQUEST_DEADLINE_SECONDS=1500
WORKER_CONNECT_TIMEOUT_SECONDS=30
WORKER_STAGE_TIMEOUT_SECONDS=1800
WORKER_WRITE_TIMEOUT_SECONDS=60
WORKER_POOL_TIMEOUT_SECONDS=60
STAGE_HEARTBEAT_INTERVAL_SECONDS=30
```

Wichtig:

- `requirements`, `reviewer`, `documentation` und `qa` laufen standardmaessig auf `mistral-small3.2:latest`
- lange lokale Modelllaeufe erscheinen im UI jetzt nicht mehr wie ein Freeze, weil Heartbeat-Ereignisse und Auto-Refresh sichtbar bleiben
- wenn ein Task trotzdem zu lange in einer Stage bleibt, zuerst die Laufzeitgrenzen erhoehen und erst danach das Routing aendern

## Git auf Unraid und isolierte Task-Workspaces

Gerade auf Unraid mit gemounteten Repositories sind zwei Runtime-Pfade wichtig:

```env
RUNTIME_HOME_DIR=/tmp/agent-home
TASK_WORKSPACE_ROOT=/workspace/.task-workspaces
```

Warum:

- Git braucht ein beschreibbares `HOME`, damit `safe.directory` gesetzt werden kann
- der gemeinsame Checkout unter `/workspace/<repo>` kann durch alte Aenderungen dirty sein
- neue Tasks arbeiten deshalb jetzt in einer eigenen isolierten Arbeitskopie unter `.task-workspaces/<task-id>/...`

Das reduziert typische Self-Hosted-Fehler wie:

- `fatal: detected dubious ownership`
- `could not lock config file //.gitconfig`
- diffende Tasks, die unbemerkt auf alten lokalen Aenderungen aufsetzen

## Sicherheit

- Externe Inhalte gelten immer als untrusted.
- Prompt-Injection-Signale werden heuristisch erkannt und markiert.
- Secret-/Infra-/destruktive Diff-Muster erzeugen Risikoflags.
- Deployments gehen standardmäßig nur nach Staging.
- Merge nach `main` ist nicht automatisiert.
- Shell-Kommandos im Test-Worker laufen nur über eine Allowlist.

## Lokaler Secret-Store

Für dieses Projekt ist ein **projektgebundener lokaler Secret-Ordner** sinnvoller als Tokens immer wieder in Deployments oder in die Repo-`.env` zu schreiben.

Empfohlener Ort auf Unraid:

- `/mnt/user/appdata/feberdin-agent-team/secrets`

Prinzip:

- ein Secret pro Datei
- Verzeichnis bleibt außerhalb des Git-Repos
- Docker mountet den Ordner read-only nach `/run/project-secrets`
- der Python-Config-Layer unterstützt `*_FILE`-Variablen wie `GITHUB_TOKEN_FILE`

Beispiele:

```bash
mkdir -p /mnt/user/appdata/feberdin-agent-team/secrets
chown -R 99:100 /mnt/user/appdata/feberdin-agent-team/secrets
chmod 750 /mnt/user/appdata/feberdin-agent-team/secrets
printf '%s' 'ghp_xxx' > /mnt/user/appdata/feberdin-agent-team/secrets/github_token
chmod 640 /mnt/user/appdata/feberdin-agent-team/secrets/github_token
```

Wichtig:

- Bei `PUID=99` und `PGID=100` müssen Verzeichnis und Dateien fuer diesen Container-User lesbar sein.
- Ein Verzeichnis mit `700` und Dateien mit `600`, die `root:root` gehoeren, sind fuer den Container oft **nicht** lesbar.

Danach reicht in `.env` der Dateipfad, nicht der Klartextwert:

```env
HOST_SECRETS_DIR=/mnt/user/appdata/feberdin-agent-team/secrets
GITHUB_TOKEN_FILE=/run/project-secrets/github_token
```

## Repository-Allowlist und Änderungsfreigabe

Das Dashboard enthält jetzt eine zentrale Allowlist für GitHub-Repositories.

- Standard ist deny-by-default.
- Worker dürfen nur Repositories ansehen, die im Webinterface unter `Einstellungen` explizit freigegeben wurden.
- Änderungen an erlaubten Repositories benötigen zusätzlich eine ausdrückliche Bestätigung.
- Ohne diese Bestätigung stoppt der Workflow vor dem Coding-Schritt und liefert zuerst Analyse- und Verbesserungsvorschläge.
- Commits und PRs enthalten einen sichtbaren Herkunftshinweis auf das `Feberdin local-multi-agent-company worker project`.

## Trusted Sources und Web Search

Der Research-/Search-Worker verwendet jetzt ein persistentes Trusted-Source-Profil.

- Seed-Profil: [config/trusted_sources.coding_profile.json](/Users/joachim.stiegler/CodingFamily/config/trusted_sources.coding_profile.json)
- Web-Search-Provider: [config/web_search.providers.json](/Users/joachim.stiegler/CodingFamily/config/web_search.providers.json)
- Doku: [docs/trusted-sources.md](/Users/joachim.stiegler/CodingFamily/docs/trusted-sources.md)

Verhalten:

- strukturierte offizielle APIs und Registries vor HTML-Doku
- offizielle Doku vor allgemeiner Websuche
- unbekannte Domains standardmäßig blockiert
- SearXNG als vorgesehener Primär-Provider
- Brave optional als Fallback
- Dry-Run, Connectivity-Checks und JSON Import/Export direkt im Dashboard
- der Stack bringt standardmaessig keinen eigenen SearXNG-Container mit; die Provider-Konfiguration verweist daher auf eine externe Instanz
- SearXNG wird fuer Worker immer ueber die offizielle JSON-API auf `GET /search` mit `format=json` angesprochen

Wichtig zu SearXNG:

- Browser-HTML allein reicht nicht aus; produktiv zaehlt nur der JSON-Healthcheck
- die Instanz muss in ihrer `settings.yml` JSON ausdruecklich freischalten:

```yaml
search:
  formats:
    - html
    - json
```

- wenn der Basis-Check funktioniert, aber der JSON-Check `403 Forbidden` liefert, ist oft genau diese JSON-Freigabe oder eine serverseitige Zugriffsbeschraenkung die Ursache

Wichtig zu Keys:

- `GITHUB_TOKEN`: für GitHub-Automation nötig
- `BRAVE_SEARCH_API_KEY`: nur nötig, wenn Brave aktiviert wird
- `MODEL_API_KEY`, `MISTRAL_API_KEY`, `QWEN_API_KEY`: nur nötig, wenn dein lokaler Modell-Endpoint Auth verlangt

Hinweis zu Brave:

- Brave bleibt in diesem Stack ein optionaler Fallback und ist standardmaessig deaktiviert.
- Nach der oeffentlichen Brave-Preisuebersicht vom 11. April 2026 solltest du fuer neue Setups von kostenpflichtigen Search-/Answers-Tarifen mit monatlichem Guthaben ausgehen.
- Fuer Home-Labs ist deshalb eine eigene SearXNG-Instanz oder reine Trusted-Source-Nutzung oft die einfachere Ausgangslage.

## Worker Guidance und Mitarbeiterideen

Das Dashboard enthält jetzt zwei zusätzliche Führungsbereiche:

- `Worker Guidance`: pro Worker Handlungsempfehlungen, Entscheidungspräferenzen und Kompetenzgrenzen pflegen
- `Mitarbeiterideen`: Verbesserungsvorschläge der Worker prüfen und freigeben oder ablehnen

Außerdem schreibt der Orchestrator für jeden Worker-Lauf einen sichtbaren Entscheidungsbaum in die Task-Detail-Seite.

Mehr dazu in [docs/worker-governance.md](/Users/joachim.stiegler/CodingFamily/docs/worker-governance.md).

## Logs und Debugging

- Laufzeit-Logs liegen im Docker-Log-Stream.
- Task-Artefakte liegen unter `HOST_REPORTS_DIR/<task-id>/`.
- Persistenter Status liegt in `HOST_DATA_DIR/orchestrator.db`.
- Dauerhafter Memory-Output liegt in `HOST_DATA_DIR/memory/`.
- Für mehr Details: `LOG_LEVEL=DEBUG`

## Tests

Lokal ausführbar:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
./scripts/run-local-tests.sh
```

## Wichtige Dokumente

- [docs/architecture.md](/Users/joachim.stiegler/CodingFamily/docs/architecture.md)
- [docs/workflows.md](/Users/joachim.stiegler/CodingFamily/docs/workflows.md)
- [docs/unraid-deployment.md](/Users/joachim.stiegler/CodingFamily/docs/unraid-deployment.md)
- [docs/ssh-bootstrap.md](/Users/joachim.stiegler/CodingFamily/docs/ssh-bootstrap.md)
- [docs/configuration.md](/Users/joachim.stiegler/CodingFamily/docs/configuration.md)
- [docs/security.md](/Users/joachim.stiegler/CodingFamily/docs/security.md)
- [docs/troubleshooting.md](/Users/joachim.stiegler/CodingFamily/docs/troubleshooting.md)

## Lizenz

MIT. Details siehe [LICENSE](/Users/joachim.stiegler/CodingFamily/LICENSE).
