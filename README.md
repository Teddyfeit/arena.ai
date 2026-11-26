# FIELDKIT — Roblox Avatar Finder + OSINT Toolkit

Beim Öffnen erscheint eine Auswahl mit genau zwei Tools:

1. **Roblox Avatar Finder** — Roblox-Profile per Name oder Benutzer-ID suchen und Avatar, Items, Outfits, Spiele, Gruppen, Favoriten und Verlauf erkunden.
2. **OSINT Toolkit** — eigene Seiten unter `/osint/username`, `/osint/email` und `/osint/csint` für öffentliche Username-, E-Mail-Domain- und Domain/IP-Prüfungen.

Der Username Finder prüft **44 öffentliche Profilseiten**. Bestätigte Treffer, mögliche Treffer und nicht eindeutig prüfbare Seiten werden getrennt angezeigt. Plattformen können automatisierte Prüfungen blockieren oder uneindeutige Antworten liefern; Links zu möglichen Treffern sollten manuell geöffnet werden.

Der E-Mail-Check prüft Format und öffentliche MX-Mailserver der Domain. Er ermittelt **nicht**, ob ein Postfach existiert oder bei Websites registriert ist, und sucht nicht in Datenlecks. CSINT wird hier als passive Cyber-/Domain-Info verstanden: öffentliche DNS- und RDAP-Daten, ohne Portscans oder Exploit-Tests. Details und Lizenzhinweis zum adaptierten Sherlock-Prüfansatz stehen in `THIRD_PARTY_NOTICES.md`.

Die Oberfläche nutzt ein macOS-inspiriertes Liquid-Glass-Design. Die komplette Anwendung steckt in `tool_hub.py` und benötigt keine zusätzlichen Python-Pakete.

## Starten

```bash
python tool_hub.py
```

FIELDKIT öffnet sich automatisch im Browser unter `http://127.0.0.1:8765`. Lass das Terminal geöffnet, solange du die App verwendest; mit `Ctrl+C` beendest du sie.

## Tests

```bash
python -m unittest -v test_avatar_finder.py
```
