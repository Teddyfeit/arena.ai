# FIELDKIT — Roblox Avatar Finder + OSINT Toolkit

Beim Öffnen der Website erscheint eine Auswahl mit genau zwei Tools. Jeder OSINT-Bereich hat eine eigene URL und wird in derselben Browser-Registerkarte geöffnet.

## Roblox Avatar Finder

- Roblox-Profile per Benutzername oder Benutzer-ID suchen.
- Avatar, öffentliche Profildetails und Präsenz anzeigen, soweit Roblox diese bereitstellt.
- Getragene Items und gespeicherte Outfits erkunden.
- Öffentliche Spiele, Gruppen und Rollen ansehen.
- Profile als Favoriten speichern und wieder öffnen; lokaler Verlauf.
- Profil-/Item-Daten kopieren und exportieren; Sammlungen filtern, sortieren und als Raster oder Liste darstellen.
- Roblox-Antworten lokal in SQLite zwischenspeichern.

## OSINT Toolkit

- **Username Finder (`/osint/username`)**: öffentliche Profilseiten auf 44 Websites prüfen, darunter GitHub, GitLab, Reddit, Steam, Twitch, YouTube, Instagram, TikTok, X, LinkedIn, Flickr, Vimeo und Telegram. Statuswerte unterscheiden bestätigte, mögliche und nicht eindeutig prüfbare Profile.
- **E-Mail-Domain-Check (`/osint/email`)**: Format, Domain und öffentliche MX-Einträge prüfen; bekannte Wegwerf-Maildomains anhand einer kleinen lokalen Liste kennzeichnen. Er prüft weder Postfach-Existenz noch Website-Registrierungen oder Datenlecks.
- **CSINT / Domain-Info (`/osint/csint`)**: öffentliche DNS- und RDAP-Registrierungsdaten für eine Domain oder IP-Adresse anzeigen. Keine Ports scannen, keine Systeme angreifen und keine Sicherheitsbewertung vortäuschen.

Der Username-Prüfansatz ist von den öffentlich dokumentierten Methoden des MIT-lizenzierten Projekts Sherlock inspiriert; siehe `THIRD_PARTY_NOTICES.md`. Website-Antworten können blockiert oder mehrdeutig sein, daher werden unsichere Ergebnisse ausdrücklich nicht als bestätigt ausgegeben.

## Datenschutz und Grenzen

- Username-Abfragen gehen an die aufgeführten öffentlichen Profilseiten.
- Beim E-Mail-Check wird nur die Domain an den öffentlichen DNS-over-HTTPS-Resolver gesendet; die vollständige Adresse wird nicht gespeichert.
- Es gibt keine Prüfung privater Konten, keine Anmeldung und keine Abfrage von geleakten Zugangsdaten.

## Start

```bash
python tool_hub.py
```
