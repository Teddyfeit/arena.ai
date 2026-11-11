# Discord Name Ideas

Ein kleiner, anfängerfreundlicher Discord-Namensideen-Generator. Er erzeugt lokal zufällige Vorschläge mit einer einstellbaren Länge von 2 bis 32 Zeichen. Er fragt Discord **nicht** automatisch ab; die Verfügbarkeit musst du selbst in Discord prüfen.

Wenn du einen Discord-Webhook einträgst, wird ein Name erst dann gesendet, wenn du ihn nach eigener Prüfung ausdrücklich als verfügbar bestätigst. Der Webhook wird lokal in `username_ideas.json` gespeichert. Teile diese Datei nicht und füge sie nicht zu Git hinzu.

## Starten

```bash
python tool_hub.py
```

Der Browser öffnet `http://127.0.0.1:8765`. Python-Standardbibliothek genügt; es sind keine zusätzlichen Pakete nötig.

Für eine Arena-/Sandbox-Vorschau:

```bash
python tool_hub.py --host 0.0.0.0 --port 8002 --no-browser
```

`test.py` bleibt als kleiner Kompatibilitäts-Launcher erhalten.

## Webhook

1. Erstelle in deinem Discord-Server einen Webhook und kopiere dessen URL.
2. Füge die URL im Feld **Discord-Webhook verbinden** ein und speichere sie.
3. Erzeuge Vorschläge und prüfe einen Namen selbst in Discord.
4. Klicke nur bei bestätigter Verfügbarkeit auf **Ich habe geprüft – verfügbar**. Erst dann sendet die App den Namen an den Webhook.

Die App akzeptiert Discord-Webhook-URLs unter `discord.com` oder `discordapp.com`. Sie speichert keine Discord-Passwörter oder Bot-Tokens, führt keine automatische Verfügbarkeitsprüfung durch und beansprucht keine Namen.
