# FIELDKIT Roblox Avatar Finder

FIELDKIT enthält nur den vollständigen **Roblox Avatar Finder**: Suche ein Profil nach Roblox-Namen oder Benutzer-ID und erkunde Avatar, Items, Outfits, Spiele, Gruppen, Favoriten und Verlauf. Die Oberfläche nutzt ein animiertes macOS-inspiriertes Liquid-Glass-Design mit anpassbarem Theme, Akzentfarbe, Wallpaper und Glass-Effekten.

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
