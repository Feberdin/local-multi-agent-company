# Worker Guidance und Mitarbeiterideen

## Ziel

Das System unterstützt jetzt zwei zusätzliche Führungswerkzeuge:

1. `Worker Guidance`
   Damit gibst du einzelnen Workern grundsätzliche Handlungsempfehlungen, Entscheidungspräferenzen und Kompetenzgrenzen mit.
2. `Mitarbeiterideen`
   Worker können Verbesserungsvorschläge einreichen, wenn sie wiederkehrende Schwächen oder sinnvolle Erweiterungen sehen.

## Worker Guidance

Seite im Dashboard:

- `Worker Guidance`

Pro Worker konfigurierbar:

- Rollenbeschreibung
- Handlungsempfehlungen
- Entscheidungspräferenzen
- Kompetenzgrenze
- ob Ideen außerhalb des Kompetenzrahmens eskaliert werden sollen
- ob Verbesserungsvorschläge automatisch eingereicht werden

Die Guidance wird vom Orchestrator in jede Worker-Anfrage injiziert.
Reasoning-Worker nutzen sie direkt im Prompting.

## Mitarbeiterideen

Seite im Dashboard:

- `Mitarbeiterideen`

Dort siehst du:

- offene Vorschläge
- freigegebene Vorschläge
- abgelehnte Vorschläge

Vorschläge enthalten:

- welcher Worker sie eingebracht hat
- zu welcher Aufgabe und welchem Repository sie gehören
- Begründung
- empfohlene Maßnahme
- ob der Kompetenzrahmen überschritten wurde

Wichtig:

- Vorschläge führen nie automatisch Aktionen aus
- sie sind reine, explizite Empfehlungen
- du entscheidest als Geschäftsführer über Freigabe oder Ablehnung

## Entscheidungsbäume

Jeder Worker-Output wird im Orchestrator mit einem Entscheidungsbaum angereichert.

Zu sehen auf:

- Task-Detail-Seite unter `Entscheidungsbäume`

Der Baum zeigt unter anderem:

- welche Eingaben betrachtet wurden
- welche Guidance aktiv war
- welcher Ausführungspfad gewählt wurde
- ob Risiken oder Eskalationen erkannt wurden
- welches Ergebnis daraus entstand

## Persistenz

JSON-Dateien im Datenverzeichnis:

- `DATA_DIR/worker_guidance.json`
- `DATA_DIR/improvement_suggestions.json`

Seed-Konfiguration:

- [config/worker_guidance.defaults.json](/Users/joachim.stiegler/CodingFamily/config/worker_guidance.defaults.json)

## Hinweise

- Kompetenzgrenzen ersetzen keine Approval-Gates, sie ergänzen sie
- Vorschläge sind bewusst konservativ und dedupliziert
- bestehende Tasks erhalten Entscheidungsbäume erst, wenn Worker erneut laufen
