# FIELDKIT

FIELDKIT vereint den vollständigen **Roblox Avatar Finder** und den neuen **Discord Name Sniper** in einer App. Der Avatar Finder ist die Startansicht; den Name Sniper öffnest du über den Tab in derselben Seitenleiste.

```bash
python tool_hub.py
```

Der Browser öffnet `http://127.0.0.1:8765`. Es werden nur die Python-Standardbibliothek und keine zusätzlichen Pakete benötigt.

Für eine Arena-/Sandbox-Vorschau:

```bash
python tool_hub.py --host 0.0.0.0 --port 8002 --no-browser
```

## Discord Name Sniper

Du kannst die Länge (2–32 Zeichen) und die Anzahl der Namensideen einstellen. Die Vorschläge werden lokal erzeugt. Die App fragt Discord **nicht** automatisch ab: Prüfe einen Namen selbst in Discord und bestätige erst danach **„Ich habe geprüft – verfügbar“**. Nur nach dieser Bestätigung sendet FIELDKIT den Namen an den optionalen Discord-Webhook.

Die Webhook-Adresse und Namensideen werden lokal in `username_ideas.json` gespeichert. Diese Datei ist in Git ignoriert. Teile sie nicht. Es werden keine Passwörter oder Bot-Tokens gespeichert; die App beansprucht keine Namen.
