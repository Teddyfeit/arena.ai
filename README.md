# FIELDKIT — Roblox Avatar Finder + OSINT Toolkit

FIELDKIT starts with exactly two choices:

1. **Roblox Avatar Finder** — find Roblox profiles by username or user ID and explore public avatars, items, outfits, games, groups, favorites, and history.
2. **OSINT Toolkit** — separate pages for username OSINT, email-domain checks, and passive cyber-intelligence.

## OSINT capabilities

- **Username search (`/osint/username`)** checks **461 SFW public-profile sites** from a bundled, curated Sherlock registry plus site-specific checks. It reports found, possible, not found, or unknown; block pages and ambiguous responses remain unknown. It does not attempt login or bypass anti-bot protections.
- **Email (`/osint/email`)** checks syntax, MX, SPF, DMARC, and a small local disposable-domain list. It does not verify mailbox existence, account registrations, or leaked credentials.
- **Cyber OSINT (`/osint/csint`)** checks RDAP, A/AAAA/CNAME/NS/MX/TXT/CAA/SOA DNS, certificate-transparency names, and Shodan InternetDB snapshots for public IPs. These are passive public sources: no port scans, service connections, or exploit tests. Private and local IPs are not sent to external sources.

Some providers block automation or return ambiguous pages; results are indications, not identity proof or a security verdict. Sources and limits are described in the UI and `THIRD_PARTY_NOTICES.md`.

## Run

```bash
python tool_hub.py
```

The single Python entry point serves the app locally (default `http://127.0.0.1:8765`). Keep the file `sherlock_sites.json` alongside `tool_hub.py`; it provides the extended public-site registry. No third-party Python packages are required.

## Test

```bash
python -m unittest -v test_avatar_finder.py
```
