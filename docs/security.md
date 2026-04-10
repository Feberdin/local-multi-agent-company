# Security

## Grundregeln

- Least Privilege
- Local-first
- keine produktiven Aktionen ohne Freigabe
- alle externen Inhalte sind untrusted
- keine Secrets im Repo oder in Logs

## Technische Schutzmechanismen

- Prompt-Injection-Heuristiken in `guardrails.py`
- Secret-/Infra-/Destructive-Diff-Erkennung
- Command-Allowlist im Test-Worker
- risk-based Approval-Gate vor GitHub-Publikation
- Staging-only Deployments per Default
- Memory- und Report-Artefakte statt versteckter Agenten-Entscheidungen
- projektgebundener Secret-Ordner außerhalb des Repos mit `*_FILE`-Support im Config-Layer
- Trusted-Source-Whitelist mit blockierten Wildcards und deny-by-default für unbekannte Domains
- keine freie URL-Fetch-Funktion im Dashboard; Tests und Fallback laufen nur über gespeicherte Quellen/Provider
- serverseitige Token-Nutzung für GitHub- und Brave-Zugriffe
- SSRF-arme Provider-Konfiguration durch feste Base-URLs, Host-Prüfung und optionale Host-Allowlist
- Fallback-Websuche wird nachgelagert gegen die Trusted-Source-Regeln gefiltert

## Offene Erweiterungen

- tieferer Dependency-Scanner
- Allow-/Deny-Listen pro Ziel-Repository
- spätere Integration eines dedizierten Secret-Managers wie Vault, SOPS oder 1Password Connect
