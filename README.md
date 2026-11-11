# FIELDKIT

FIELDKIT startet mit einer übersichtlichen **Lookup-Startseite**. Von dort kannst du den vollständigen **Roblox Avatar Finder** und **20 weitere öffentliche Lookup-Tools** auswählen.

```bash
python tool_hub.py
```

Der Browser öffnet `http://127.0.0.1:8765`. Es werden nur die Python-Standardbibliothek und keine zusätzlichen Pakete benötigt.

Für die Arena-/Sandbox-Vorschau:

```bash
python tool_hub.py --host 0.0.0.0 --port 8002 --no-browser
```

Die Startseite enthält den Avatar Finder plus 20 Lookup-Werkzeuge: Domain-/RDAP-, IP-/ASN-, DNS-, Zertifikats-, TLS-, URL-Reputations-, Archiv-, GitHub- und lokale JSON-/Base64-Lookups. Externe Tools öffnen sich in einem neuen Tab; die eingegebene Suchanfrage kann an den jeweiligen Anbieter übertragen werden. JSON und Base64 werden lokal im Browser verarbeitet. Die vollständige Liste steht in [FEATURES.md](FEATURES.md).

Nutze Lookup-Dienste verantwortungsvoll und prüfe nur Informationen oder Systeme, auf die du zugreifen darfst. Es gibt keinen Discord Name Sniper mehr.
