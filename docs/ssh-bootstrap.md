# SSH Bootstrap für Unraid

## Zweck

Dieses Projekt enthält eine dedizierte SSH-Bootstrap-Lösung für den Coding-Agent, damit der Zugriff auf den Unraid-Server über einen eigenen Schlüssel statt über allgemeine Benutzer-Keys erfolgt.

Zielsystem:

- Host: `192.168.57.10`
- User: `root`
- lokaler Private Key: `~/.ssh/unraid_agent`
- lokaler Public Key: `~/.ssh/unraid_agent.pub`

## Was das Bootstrap-Skript macht

Das Skript [scripts/setup_unraid_ssh.sh](/Users/joachim.stiegler/CodingFamily/scripts/setup_unraid_ssh.sh):

1. prüft, ob `~/.ssh/unraid_agent` und `~/.ssh/unraid_agent.pub` bereits existieren
2. erzeugt bei Bedarf lokal einen neuen `ed25519`-Key mit `ssh-keygen`
3. überschreibt niemals einen bestehenden privaten Schlüssel
4. installiert nur den Public Key auf `root@192.168.57.10`
5. bevorzugt `ssh-copy-id`, wenn verfügbar
6. verwendet sonst einen manuellen SSH-Fallback mit sauberem Rechte-Setup auf Unraid
7. verhindert Doppeleinträge in `/root/.ssh/authorized_keys` durch exakten Whole-Line-Vergleich
8. führt danach einen nicht-interaktiven SSH-Test mit dem dedizierten Key aus

## Voraussetzungen

- Das Skript läuft auf einem Linux-System mit `bash`.
- Verfügbar sein müssen:
  - `bash`
  - `ssh`
  - `ssh-keygen`
  - optional `ssh-copy-id`
  - `grep`
  - `chmod`
  - `mkdir`
  - `cat`
- Auf Unraid muss SSH aktiviert sein.
- Für die Erstinstallation muss entweder Passwort-Login für `root@192.168.57.10` funktionieren oder es muss bereits ein anderer funktionierender SSH-Zugang existieren.

Wichtig:

- Das Skript speichert kein Passwort.
- Der private Schlüssel bleibt immer lokal und wird nie auf den Server kopiert.

## Direkter Aufruf

```bash
./scripts/setup_unraid_ssh.sh
```

Erfolgsprüfung:

```bash
ssh -i ~/.ssh/unraid_agent -o BatchMode=yes -o StrictHostKeyChecking=accept-new root@192.168.57.10 'echo unraid-agent-ssh-ok'
```

## Manueller Fallback ohne ssh-copy-id

Wenn `ssh-copy-id` nicht vorhanden ist, verwendet das Skript einen bewusst einfachen und nachvollziehbaren Fallback per `ssh`. Auf dem Unraid-System wird dabei genau dieses Schema angewendet:

```bash
mkdir -p ~/.ssh
chmod 700 ~/.ssh
touch ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

Danach wird der Public Key nur dann angehängt, wenn er nicht bereits exakt als ganze Zeile vorhanden ist.

## Idempotenz und Sicherheit

Das Bootstrap ist absichtlich idempotent:

- vorhandene Schlüssel werden nicht überschrieben
- ein bestehender Public-Key-Eintrag wird nicht doppelt eingetragen
- ein erfolgreicher bestehender SSH-Zugang beendet das Skript ohne Änderungen

Sicherheitsrelevante Entscheidungen:

- `StrictHostKeyChecking=accept-new` akzeptiert nur neue Host-Keys automatisch, nicht geänderte
- der Login-Test nutzt `BatchMode=yes`, damit nur wirklich funktionierende Key-Authentifizierung als Erfolg zählt
- `grep -Fqx` erzwingt einen exakten Vergleich der kompletten Public-Key-Zeile

## Optionale Härtung

Zusätzlich liegt das Skript [scripts/harden_unraid_authorized_key.sh](/Users/joachim.stiegler/CodingFamily/scripts/harden_unraid_authorized_key.sh) bei. Es ist standardmäßig **nicht** Teil des normalen Bootstraps.

Es ersetzt den unbeschränkten Agent-Key-Eintrag optional durch eine restriktive `authorized_keys`-Zeile wie:

```text
command="/boot/config/custom/agent-deploy.sh",no-agent-forwarding,no-port-forwarding,no-X11-forwarding,no-pty ssh-ed25519 ...
```

Aufruf:

```bash
./scripts/harden_unraid_authorized_key.sh
```

Wichtig:

- Diese Härtung ist nur sinnvoll, wenn der Agent später gezielt auf ein Forced-Command-Szenario umgestellt werden soll.
- Danach sind interaktive SSH-Aufrufe mit demselben Key bewusst eingeschränkt.
- Das Skript verweigert absichtlich die automatische Änderung, wenn der Key bereits mit unbekannten Optionen in `authorized_keys` vorhanden ist.

## Einbindung beim Projekt-Setup

Das Projekt kann die Initialisierung optional schon beim Bootstrap auslösen, ohne sie standardmäßig zu erzwingen.

Empfohlener Weg:

1. In `.env` den Wert `BOOTSTRAP_UNRAID_SSH=true` setzen
2. dann [scripts/bootstrap.sh](/Users/joachim.stiegler/CodingFamily/scripts/bootstrap.sh) ausführen

Beispiel:

```bash
BOOTSTRAP_UNRAID_SSH=true ./scripts/bootstrap.sh
```

Sichere Voreinstellung:

- In `.env.example` bleibt `BOOTSTRAP_UNRAID_SSH=false`
- Dadurch bleibt das Projekt standardmäßig bei „deny by default“ und führt keinen SSH-Zugriff ungefragt aus

Alternativ kann ein späteres Makefile oder ein Task-Runner einfach dieses Skript als expliziten Schritt aufrufen:

```bash
./scripts/setup_unraid_ssh.sh
```

## Typische Fehler

`Permission denied (publickey,password,keyboard-interactive)`

- SSH ist auf Unraid nicht aktiv
- Passwort-Login ist für die Erstinstallation nicht möglich
- der Public Key wurde noch nicht installiert oder der falsche Key wird verwendet

`Host key verification failed`

- der bekannte Host-Key hat sich geändert und muss manuell geprüft werden

`ssh-copy-id not found`

- das ist kein Problem; das Skript nutzt dann automatisch den manuellen Fallback
