# FIELDKIT — vollständige Funktionsliste

## Startseite / Lookup Desk

- Helle, responsive Startseite mit Suchfeld zum Filtern der Werkzeuge.
- Kategorien: Profil, Domain, Netzwerk, Sicherheit, Code, Websuche und Lokal.
- **21 Karten insgesamt:** der Roblox Avatar Finder plus 20 weitere Werkzeuge.
- Externe Dienste öffnen sich mit dem eingegebenen Suchwert in einem neuen Browser-Tab.
- Hinweis vor externen Lookups: Suchwerte können an den jeweiligen Drittanbieter übertragen werden.

## Roblox Avatar Finder

1. **Profil suchen:** Roblox-Benutzername oder Benutzer-ID eingeben.
2. **Profilansicht:** Avatar, Anzeigename, Benutzername, öffentliche Profilangaben, Beitrittsdatum, Freund-/Follower-Angaben und – sofern Roblox sie bereitstellt – Präsenz ansehen.
3. **Getragene Items:** aktuelle Kleidung und Accessoires ansehen.
4. **Outfits:** gespeicherte Roblox-Outfits und deren Items ansehen.
5. **Spiele:** öffentliche Experiences des Profils ansehen.
6. **Gruppen:** öffentliche Gruppenmitgliedschaften und Rollen ansehen.
7. **Favoriten:** Profile im Browser speichern und später wieder öffnen.
8. **Zuletzt gesucht:** lokale Suchhistorie ansehen, einzelne Einträge löschen oder Verlauf leeren.
9. **Export und Teilen:** Profil als JSON exportieren, Items als CSV speichern, Profil-Link oder IDs kopieren und Profil teilen.
10. **Ansicht und Bedienung:** Suchbeispiele, Filter-/Sortieroptionen für Sammlungen, Raster-/Listenansicht, Cache und Verlauf, helle Darstellung, Akzentfarben und Hintergrundthemen.
11. **Technische Daten:** verfügbare Rohdaten des geladenen Profils ansehen.
12. **Lokaler Cache:** Roblox-Antworten werden lokal zwischengespeichert, damit wiederholte Lookups schneller sind.

## Die 20 zusätzlichen Lookup-Werkzeuge

### Domain und Netzwerk

1. **Domain-Registrierung (RDAP / ICANN Lookup)** — öffentliche Registrierungsdaten einer Domain.
2. **IP- / ASN-Lookup (ipinfo.io)** — öffentliche Netzwerkinformationen einer IP-Adresse.
3. **DNS A / AAAA** — öffentliche A-Record-Abfrage.
4. **Mailserver (MX)** — öffentliche Mailserver-Einträge einer Domain.
5. **SPF / TXT-Einträge** — öffentliche TXT-Einträge, darunter oft SPF-Richtlinien.
6. **DMARC-Eintrag** — TXT-Abfrage von `_dmarc.<domain>`.
7. **DNS-Verteilung weltweit** — A-Record-Antworten aus verschiedenen Regionen.

### Website-Sicherheit und Verlauf

8. **Zertifikat-Transparenz (crt.sh)** — öffentliche TLS-Zertifikate und zugehörige Hostnamen.
9. **TLS / SSL-Bericht (SSL Labs)** — öffentlicher TLS-Konfigurationstest.
10. **HTTP-Sicherheitsheader (SecurityHeaders)** — Header-Bericht zu einer angegebenen Website.
11. **URLScan-Suche** — bereits öffentliche URL-Scan-Ergebnisse suchen.
12. **URLhaus-Suche** — öffentliche Malware-URL-Datenbank durchsuchen.
13. **VirusTotal Domain-Bericht** — vorhandenen öffentlichen Domain-Bericht öffnen.
14. **Website-Technologien (Wappalyzer)** — öffentliche Technologie-Erkennung.
15. **Wayback Machine** — archivierte Webseiten-Snapshots suchen.

### Code und öffentliche Websuche

16. **GitHub-Profil** — öffentliches Profil anhand des GitHub-Benutzernamens öffnen.
17. **GitHub-Reposuche** — öffentliche Repositories nach einem Begriff suchen.
18. **Öffentliche Username-Suche** — einen Handle als Suchbegriff im öffentlichen Web suchen.

### Lokale Daten-Helfer

19. **JSON formatieren** — JSON im Browser parsen, validieren und lesbar formatieren.
20. **Base64 kodieren / dekodieren** — Text im Browser umwandeln; der Text wird nicht an den Server gesendet.

## Nicht enthalten

- Kein Discord Name Sniper oder automatisierter Discord-Verfügbarkeitscheck.
- Keine Passwort-, Bot-Token- oder API-Schlüssel-Eingabe für die Homepage-Tools.
- Keine automatischen Scans ganzer IP-Bereiche, Konten oder Domains.
- Die Homepage selbst lädt externe Ergebnisse nicht in die App; sie verlinkt die jeweiligen öffentlichen Dienste.

## Start

```bash
python tool_hub.py
```

Für eine Sandbox-Vorschau:

```bash
python tool_hub.py --host 0.0.0.0 --port 8002 --no-browser
```
