# FIELDKIT — Avatar Finder & Username Finder

Beim Öffnen der Website erscheint eine Auswahl mit genau zwei Tools.

## Roblox Avatar Finder

- Roblox-Profile per Benutzername oder Benutzer-ID suchen.
- Avatar, öffentliche Profildetails und Präsenz anzeigen, soweit Roblox diese bereitstellt.
- Getragene Items und gespeicherte Outfits erkunden.
- Öffentliche Spiele, Gruppen und Rollen ansehen.
- Profile als Favoriten speichern und wieder öffnen.
- Lokalen Verlauf der zuletzt gesuchten Profile ansehen, einzelne Einträge entfernen oder den Verlauf löschen.
- Profil-/Item-Daten kopieren und exportieren; Sammlungen filtern, sortieren und als Raster oder Liste darstellen.
- Einstellungen für helles/dunkles Erscheinungsbild, Akzentfarbe und animierte Hintergründe; Größenanpassung der Seitenleiste.
- Roblox-Antworten werden lokal in SQLite zwischengespeichert.

## Username Finder

- Einen Nutzernamen auf 16 öffentlichen Profilseiten prüfen: GitHub, GitLab, Codeberg, Reddit, npm, Hacker News, Steam, Dev.to, Medium, SoundCloud, Twitch, YouTube, Instagram, TikTok, X und Pinterest.
- Treffer als bestätigt, möglich, nicht gefunden oder nicht eindeutig prüfbar kennzeichnen.
- Direktlinks zu den jeweiligen Profilseiten bereitstellen.
- Der Finder prüft ausschließlich öffentliche Profilseiten. Plattformen können automatisierte Abfragen blockieren; deshalb sind mögliche oder nicht eindeutig prüfbare Treffer keine Bestätigung. Der Finder umgeht keine Logins oder Schutzmaßnahmen.

## Datenschutz und Grenzen

- Es werden nur die eingegebenen Nutzernamen an die aufgeführten öffentlichen Profilseiten gesendet.
- Die Roblox-Suche benötigt kein Roblox-Passwort und fordert keine Anmeldung an.
- Private oder nicht öffentlich verfügbare Informationen werden nicht freigeschaltet.

## Start

```bash
python tool_hub.py
```
