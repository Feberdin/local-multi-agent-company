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

## Offene Erweiterungen

- tieferer Dependency-Scanner
- Allow-/Deny-Listen pro Ziel-Repository
- granularere Secret-Store-Integration
