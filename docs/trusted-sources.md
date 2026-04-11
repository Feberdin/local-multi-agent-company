# Trusted Sources und Web Search

## Ziel

Der Research-/Search-Worker arbeitet jetzt standardmäßig mit einem aktiven Trusted-Source-Profil.

Reihenfolge:

1. strukturierte offizielle APIs und Registries
2. offizielle Dokumentation
3. allgemeine Websuche nur als klar markierter Fallback

Der Seed-Startpunkt liegt in [config/trusted_sources.coding_profile.json](/Users/joachim.stiegler/CodingFamily/config/trusted_sources.coding_profile.json).  
Die Web-Search-Defaults liegen in [config/web_search.providers.json](/Users/joachim.stiegler/CodingFamily/config/web_search.providers.json).

## Neue Quellen hinzufügen

Im Dashboard:

1. `Trusted Sources` öffnen
2. Quelle ausfüllen
3. `Quelle aktivieren` nur bewusst setzen
4. `Quelle speichern`

Wichtige Regeln:

- `Domain` und `Base URL` müssen zusammenpassen
- Wildcards sind blockiert
- `Allowed Paths` und `Deny Paths` müssen mit `/` beginnen
- unbekannte Quellen bleiben standardmäßig deaktiviert, bis du sie bewusst aktivierst

## Profile importieren und exportieren

- Auf der Seite `Trusted Sources` liegt ein JSON-Export/Import-Feld
- Das JSON enthält `active_profile_id` und `profiles`
- Du kannst damit Profile sichern, offline pflegen und wieder importieren

## Worker-Routing

Die Routing-Logik liegt in [source_router.py](/Users/joachim.stiegler/CodingFamily/services/shared/agentic_lab/source_router.py).

Beispiele:

- Python-Paketversion -> zuerst `pypi.org`
- npm-Paketmetadaten -> zuerst `registry.npmjs.org`
- GitHub Release/Tags -> zuerst `api.github.com`
- Python-Syntax -> `docs.python.org`
- Web-API-Doku -> `developer.mozilla.org`
- Docker-Doku -> `docs.docker.com`
- RFC/Standard -> `www.rfc-editor.org`

Der Research-Worker schreibt den Routing-Plan in seine Ergebnisse, damit du später nachvollziehen kannst, welche Quelle bevorzugt wurde.

## APIs pro Quelle beschreiben

Für wichtige Quellen gibt es strukturierte Beschreibungen:

- `base_url`
- `source_type`
- `preferred_access`
- `auth_type`
- `auth_env_var`
- `usage_instructions`
- `rate_limit_notes`

Für GitHub, PyPI und npm sind im Seed bereits konkrete Hinweise enthalten.

## Auth per ENV

Tokens werden nie im Frontend gespeichert.

Stattdessen referenziert eine Quelle oder ein Provider nur den ENV-Namen:

- GitHub: `GITHUB_TOKEN`
- Brave: `BRAVE_SEARCH_API_KEY`

Optional kannst du auch Secret-Dateien verwenden:

- `GITHUB_TOKEN_FILE`
- `BRAVE_SEARCH_API_KEY_FILE`

Der Server liest diese Werte lokal ein. Das Webinterface zeigt nur den ENV-Namen, nie den Secret-Inhalt.

## General Web Search bewusst aktivieren

Die Seite `Web Search Providers` verwaltet SearXNG und Brave.

Sichere Defaults:

- `SearXNG` ist als primärer Provider vorgesehen, aber initial deaktiviert
- `Brave` ist optionaler Fallback und initial deaktiviert
- Trusted Sources behalten Vorrang
- Fallback-Ergebnisse werden domain-gefiltert
- SearXNG wird immer ueber die offizielle JSON-API auf `GET /search` mit Query-Parametern genutzt

Aktivierung:

1. SearXNG Base URL eintragen
2. Provider aktivieren
3. optional Brave ENV-Key serverseitig setzen
4. Health-Check und Testabfrage ausführen

Hinweis zu Brave:

- Brave bleibt in diesem Projekt bewusst optional.
- Laut der oeffentlichen Brave-Preisuebersicht vom 11. April 2026 solltest du fuer neue Setups von kostenpflichtigen Search-/Answers-Tarifen mit monatlichem Guthaben ausgehen.
- Fuer lokale oder sparsame Setups ist es oft sinnvoller, zunaechst nur Trusted Sources und optional eine eigene SearXNG-Instanz zu betreiben.

Hinweis zu SearXNG:

- Fuer produktive Worker-Nutzung muss JSON in der SearXNG-`settings.yml` aktiv sein:

```yaml
search:
  formats:
    - html
    - json
```

- Wenn nur die Browser-Suche funktioniert, aber der JSON-Healthcheck nicht, ist die Instanz fuer strukturierte Agent-Anfragen noch nicht fertig konfiguriert.

## Keys: was du wirklich brauchst

Für dein aktuelles Setup typischerweise:

- `GITHUB_TOKEN`: ja, für GitHub-Automation
- `BRAVE_SEARCH_API_KEY`: nur wenn du Brave aktivierst
- `MODEL_API_KEY`, `MISTRAL_API_KEY`, `QWEN_API_KEY`: nur wenn dein lokaler OpenAI-kompatibler Endpoint Auth verlangt

Wenn deine lokalen Modelle ohne Auth laufen, bleiben die Model-Keys leer.
