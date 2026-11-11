# FIELDKIT — Funktionsübersicht

## Lookup Desk

- Responsive Startseite mit Suche und Kategorien: Profil, Domain, Netzwerk, Sicherheit, Code, Websuche und Lokal.
- Der Avatar Finder und alle **20 öffentlichen Lookup-Karten** sind direkt in FIELDKIT integriert: Suchwerte eingeben, Anbieterantworten ansehen, Resultate kopieren oder als JSON herunterladen.
- Server, Oberfläche und sämtliche Lookup-Provider sind gemeinsam in der ausführbaren Datei `tool_hub.py` enthalten; die separate Testdatei wird nur zum Testen benötigt.
- macOS-inspiriertes Liquid-Glass-Design mit animierten, lichtdurchlässigen Flächen und interaktiven Werkzeugkarten.
- Darstellung anpassbar: Hell/Dunkel, Akzentfarbe, Hintergrund, Unschärfestärke, Kartengröße und reduzierte Animationen. Theme, Akzent, Wallpaper, Glass-Intensität und Bewegungspräferenz werden mit dem Avatar Finder geteilt und lokal gespeichert.
- Dazu kommen zwei lokal im Browser ausgeführte Helfer (JSON-Formatierer und Base64-Konverter). Das sind keine öffentlichen Lookups.
- Lookup-Ergebnisse und die letzten 100 Abfragen werden in der lokalen SQLite-Datenbank gespeichert; der sichtbare Verlauf zeigt bis zu 12 Einträge und kann gelöscht werden.
- Wiederholte Lookups verwenden einen anbieterabhängigen Kurzzeit-Cache. Pro Client-IP sind höchstens 24 Lookup-Anfragen pro Minute erlaubt.
- Antworten sind JSON-Daten des jeweiligen Dienstes; FIELDKIT zeigt Quelle und Einschränkungen an. Drittanbieter erhalten die für die Suche eingegebenen Werte.

## Die 20 öffentlichen Lookups

### Domain und Netzwerk

1. **Domain-RDAP** — Registrierungsstatus, Datenereignisse und Nameserver über öffentliche RDAP-Dienste.
2. **IP-Netzwerkdaten** — Netzblock, Organisation und Länderangaben aus RDAP für öffentliche IP-Adressen.
3. **DNS A / AAAA** — IPv4- und IPv6-Antworten über Google Public DNS.
4. **DNS TXT** — allgemeine TXT-Records einer Domain.
5. **DNS MX** — Mailserver-Records.
6. **SPF-Record** — TXT-Antworten für die SPF-Richtlinie.
7. **DMARC-Record** — TXT-Abfrage für `_dmarc.<domain>`.
8. **DNS-Propagation** — Vergleich von Antworten zweier öffentlicher DNS-over-HTTPS-Resolver.

### Website-Sicherheit und Reputation

9. **Zertifikatstransparenz** — öffentliche Zertifikate und Hostnamen via crt.sh.
10. **SSL Labs** — öffentliche TLS-Analyse und Endpunktbewertungen; eine Analyse kann noch in Bearbeitung sein.
11. **TLS-Zertifikat live** — verifizierter TLS-Handshake auf Port 443, mit Protokoll, Cipher und Zertifikatsfeldern.
12. **HTTP-Sicherheitsheader** — sichere HTTPS-Abfrage und Bericht vorhandener/fehlender ausgewählter Header.
13. **urlscan.io** — Suche in bereits öffentlichen Scan-Ergebnissen.
14. **URLhaus** — Abfrage der öffentlichen Abuse.ch-Datenbank.
15. **VirusTotal** — Domain-Reputation und Analyse-Statistiken. Erfordert einen persönlichen VirusTotal-API-Schlüssel; dieser wird nur für die Anfrage im Request-Header gesendet und weder in Verlauf noch Cache-Schlüssel gespeichert.
16. **Website-Technologien** — lokale Heuristiken über eine kleine öffentliche HTML-Stichprobe und ausgewählte Header; kein vollständiges oder garantiertes Inventar.

### Archive und GitHub

17. **Wayback Machine** — öffentliche Archiv-Snapshots über die CDX-Suche.
18. **GitHub-Profil** — ausgewählte öffentliche Profilfelder und Kennzahlen.
19. **GitHub-Repositories** — öffentliche Repository-Suche.
20. **GitHub-Nutzersuche** — öffentliche GitHub-Profile; Suche ist bewusst auf GitHub begrenzt.

## Lokale Helfer

- **JSON-Formatierer** — parst und formatiert JSON ausschließlich im Browser.
- **Base64-Konverter** — kodiert oder dekodiert Text ausschließlich im Browser.

## Roblox Avatar Finder

Die bestehende Avatar-Finder-Seite bleibt unter `/avatar` erhalten und ist auch über die Startseite erreichbar. Sie bietet weiterhin Profil-/Namenssuche, Avatar und Profildetails, Items, Outfits, Spiele, Gruppen, Favoriten, lokalen Suchverlauf, Export-/Kopierfunktionen, Filter und Sortierung, Ansichtsoptionen, Browser-Cache sowie den lokalen Python-/SQLite-Cache.

## Schutzmaßnahmen und Grenzen

- Lookup-Ziele zu externen Datenanbietern sind im Code festgelegt; beliebige Provider-URLs können nicht über die Lookup-API eingeschleust werden.
- Website- und TLS-Abfragen erlauben nur öffentliche Domains/IP-Adressen. Website-Aufrufe werden auf eine validierte öffentliche IP gepinnt, folgen keinen Redirects und lesen nur eine begrenzte HTML-Antwort.
- Keine Abfragen privater/lokaler/reservierter IP-Adressen; keine Scans von IP-Bereichen oder automatisierten Konten.
- RDAP, SSL Labs, VirusTotal und andere Anbieter können Limits, verzögerte Ergebnisse oder eigene Verfügbarkeitsprobleme haben. FIELDKIT kann keine Anbieterantwort garantieren.
- Es gibt keinen Discord Name Sniper und keine Discord-Verfügbarkeits- oder Beanspruchungsfunktion.

## Start

```bash
python tool_hub.py
```

Für eine Sandbox-Vorschau:

```bash
python tool_hub.py --host 0.0.0.0 --port 8002 --no-browser
```
