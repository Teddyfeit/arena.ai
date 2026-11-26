# FIELDKIT — Avatar- und Username-Finder

FIELDKIT startet mit einer Auswahl aus genau zwei Tools:

1. **Roblox Avatar Finder** — Roblox-Profile nach Name oder Benutzer-ID suchen und Avatar, Items, Outfits, Spiele, Gruppen, Favoriten und Verlauf erkunden.
2. **Username Finder** — einen Nutzernamen auf 16 öffentlichen Profilseiten prüfen, darunter GitHub, GitLab, Reddit, npm, Hacker News, Steam, Twitch, YouTube, Instagram, TikTok und X.

Der Username Finder unterscheidet bestätigte Treffer, mögliche Treffer und nicht eindeutig prüfbare Seiten. Viele Plattformen blockieren automatisierte Prüfungen oder liefern keine verlässliche Auskunft; mögliche Treffer sollten manuell geöffnet werden. Es werden nur öffentliche Profilseiten geprüft, ohne Anmeldung oder Umgehung von Schutzmaßnahmen.

Die Oberfläche nutzt ein macOS-inspiriertes Liquid-Glass-Design mit anpassbarem Theme, Akzentfarbe, Wallpaper und Glass-Effekten.

## Starten

```bash
python tool_hub.py
```

FIELDKIT öffnet sich automatisch im Browser unter `http://127.0.0.1:8765`. Die komplette Anwendung steckt in dieser einen Python-Datei und benötigt keine zusätzlichen Pakete. Lass das Terminal geöffnet, solange du die App verwendest; mit `Ctrl+C` beendest du sie.

Für eine Sandbox-Vorschau:

```bash
python tool_hub.py --host 0.0.0.0 --port 8002 --no-browser
```

## Tests

```bash
python -m unittest -v test_avatar_finder.py
```
