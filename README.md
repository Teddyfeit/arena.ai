# FIELDKIT

FIELDKIT startet mit einem integrierten **Lookup Desk**. Er bewahrt den vollständigen **Roblox Avatar Finder** und bietet außerdem 20 öffentliche Lookups, die direkt in der App ausgeführt werden und ihre Ergebnisse dort anzeigen. Zwei weitere Helfer (JSON und Base64) laufen ausschließlich lokal im Browser.

```bash
python tool_hub.py
```

Der Browser öffnet `http://127.0.0.1:8765`. Die komplette ausführbare Anwendung – Avatar Finder, Server, Oberfläche und alle Lookup-Provider – steckt in `tool_hub.py`; außer Python wird keine weitere Laufzeitdatei oder kein Zusatzpaket benötigt.

Für die Arena-/Sandbox-Vorschau:

```bash
python tool_hub.py --host 0.0.0.0 --port 8002 --no-browser
```

Die 20 Lookup-Karten decken RDAP und IP-Netzwerkdaten, DNS, Zertifikate und TLS, Sicherheitsheader, öffentliche URL-Reputationsdienste, Wayback sowie GitHub ab. Für VirusTotal ist ein eigener API-Schlüssel nötig; er wird nur für diese Anfrage übertragen. Ergebnisse können kopiert oder als JSON exportiert werden. Suchverlauf und Cache werden lokal in SQLite gespeichert. Die vollständige Funktionsliste und Hinweise zu Grenzen und Datenschutz stehen in [FEATURES.md](FEATURES.md).

Öffentliche Lookup-Anbieter erhalten die eingegebenen Suchwerte. Nutze die Werkzeuge verantwortungsvoll und prüfe nur Informationen oder Systeme, auf die du zugreifen darfst. Es gibt keinen Discord Name Sniper.

Die Lookup-Provider, Eingabe-/Netzwerkschutzmaßnahmen und lokalen API-Routen lassen sich mit gemockten Anbietern testen:

```bash
python -m unittest -v test_lookup_tools.py
```
