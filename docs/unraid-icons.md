# Unraid Icons und Kurznamen

## Ziel

Die Compose-Container bekommen kurze, eindeutige Namen, damit die Unraid-Docker-Ansicht lesbarer wird. Zusaetzlich liegt ein kleines SVG-Icon-Set fuer alle Worker unter [infra/unraid/icons](/Users/joachim.stiegler/CodingFamily/infra/unraid/icons) bereit.

Wichtig:

- Bei Docker Compose auf Unraid werden individuelle Worker-Icons nicht immer automatisch in der Containerliste angezeigt.
- Die Assets sind deshalb bewusst als fertige Dateien und Mapping-Tabelle im Repo hinterlegt.
- Fuer die Bootstrap-XML wird das Icon direkt verwendet.

## Kurzname-Mapping

| Rolle | Compose-Service | Containername | Icon |
| --- | --- | --- | --- |
| Orchestrator | `orchestrator` | `fmac-orch` | `orch.svg` |
| Requirements | `requirements-worker` | `fmac-req` | `req.svg` |
| Research | `research-worker` | `fmac-rsch` | `rsch.svg` |
| Architecture | `architecture-worker` | `fmac-arch` | `arch.svg` |
| Coding | `coding-worker` | `fmac-code` | `code.svg` |
| Review | `reviewer-worker` | `fmac-rev` | `rev.svg` |
| Testing | `test-worker` | `fmac-test` | `test.svg` |
| GitHub | `github-worker` | `fmac-gh` | `gh.svg` |
| Deploy | `deploy-worker` | `fmac-deploy` | `deploy.svg` |
| QA | `qa-worker` | `fmac-qa` | `qa.svg` |
| Security | `security-worker` | `fmac-sec` | `sec.svg` |
| Validation | `validation-worker` | `fmac-val` | `val.svg` |
| Documentation | `documentation-worker` | `fmac-docs` | `docs.svg` |
| Memory | `memory-worker` | `fmac-mem` | `mem.svg` |
| Data | `data-worker` | `fmac-data` | `data.svg` |
| UX | `ux-worker` | `fmac-ux` | `ux.svg` |
| Cost | `cost-worker` | `fmac-cost` | `cost.svg` |
| HR | `human-resources-worker` | `fmac-hr` | `hr.svg` |
| Rollback | `rollback-worker` | `fmac-rb` | `rb.svg` |
| Web UI | `web-ui` | `fmac-web` | `web.svg` |

Die maschinenlesbare Quelle dazu ist [infra/unraid/icons/manifest.json](/Users/joachim.stiegler/CodingFamily/infra/unraid/icons/manifest.json).

## Beispielnutzung

Nach dem naechsten Deploy erscheinen die Container in `docker compose ps` kuerzer, zum Beispiel:

```bash
fmac-arch
fmac-code
fmac-rsch
fmac-sec
fmac-web
```

Wenn du das Bootstrap-Template in Unraid verwendest, zieht es jetzt bereits das Icon aus dem Repo.
