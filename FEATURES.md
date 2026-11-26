# FIELDKIT — Roblox Avatar Finder + OSINT Toolkit

The start page offers exactly two tools. OSINT features use real pages and routes, not pop-up placeholders.

## Roblox Avatar Finder

- Find profiles by username or user ID.
- Explore public avatar, profile, presence, items, outfits, games, groups, and roles where Roblox exposes them.
- Save favorites and local recent searches; export profile data and item lists.
- Cache Roblox responses locally in SQLite.

## OSINT Toolkit

- **Username OSINT (`/osint/username`)**: checks 461 safe-for-work public-profile sites using a bundled, MIT-licensed Sherlock site registry and site-specific response checks. Results distinguish found, possible, not found, and unknown. Providers that block automation are never treated as confirmed matches.
- **Email domain (`/osint/email`)**: validates format; checks public MX, SPF, and DMARC records and a small local disposable-domain list. It cannot confirm mailbox existence, account registration, or data-breach exposure.
- **Cyber OSINT / CSINT (`/osint/csint`)**: combines RDAP registration/network data, public DNS (A, AAAA, CNAME, NS, MX, TXT, CAA, SOA), certificate transparency names from crt.sh, and public Shodan InternetDB snapshots for global IPs. All lookups are passive. No ports are probed, no services contacted, and no exploits run. Private/local IP addresses are rejected before external lookup.

## Scope and limits

- Public-source data can be stale, incomplete, blocked, or ambiguous; it is not proof of identity, current reachability, or a vulnerability.
- Username lookups contact the queried public sites. Automated checks must follow provider terms and be used responsibly.
- The complete upstream Sherlock site list's NSFW entries are excluded from the bundled registry.
- Third-party attribution and license terms are in `THIRD_PARTY_NOTICES.md`.

## Run and test

```bash
python tool_hub.py
python -m unittest -v test_avatar_finder.py
```
