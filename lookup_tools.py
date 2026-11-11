"""Public-data lookup providers used by FIELDKIT's lookup desk.

Every provider has a fixed upstream host; callers cannot supply arbitrary URLs.
The module deliberately has no Discord username-snipe or account-token support.
"""
from __future__ import annotations

import concurrent.futures
import ipaddress
import json
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from http.client import HTTPSConnection, HTTPResponse
from typing import Any, Callable

USER_AGENT = "FIELDKIT-LookupDesk/1.0 (+local public-information lookup)"
MAX_RESPONSE_BYTES = 1_500_000
MAX_SITE_BYTES = 300_000
MAX_REDIRECTS = 4
DNS_PROVIDERS = {
    "Google Public DNS": "https://dns.google/resolve",
    "Cloudflare DNS": "https://cloudflare-dns.com/dns-query",
}


class LookupError(ValueError):
    """Safe, user-displayable lookup error."""


@dataclass(frozen=True)
class Upstream:
    name: str
    hosts: frozenset[str]
    timeout: float = 12.0
    max_bytes: int = MAX_RESPONSE_BYTES


PROVIDERS = {
    "rdap": Upstream("RDAP", frozenset({"rdap.org", "rdap.arin.net", "rdap.verisign.com", "rdap.publicinterestregistry.org"})),
    "dns": Upstream("DNS", frozenset({"dns.google", "cloudflare-dns.com"})),
    "ct": Upstream("Certificate Transparency", frozenset({"crt.sh"})),
    "ssllabs": Upstream("SSL Labs", frozenset({"api.ssllabs.com"}), timeout=20.0),
    "urlscan": Upstream("urlscan.io", frozenset({"urlscan.io"})),
    "urlhaus": Upstream("URLhaus", frozenset({"urlhaus-api.abuse.ch"})),
    "virustotal": Upstream("VirusTotal", frozenset({"www.virustotal.com"})),
    "wayback": Upstream("Wayback Machine", frozenset({"web.archive.org"})),
    "github": Upstream("GitHub API", frozenset({"api.github.com"})),
}


def _clean_text(value: Any, limit: int = 300) -> str:
    text = str(value or "").strip()
    if len(text) > limit:
        raise LookupError(f"Eingabe ist zu lang (maximal {limit} Zeichen).")
    if any(ord(ch) < 32 for ch in text):
        raise LookupError("Die Eingabe enthält nicht erlaubte Steuerzeichen.")
    return text


def normalize_domain(value: Any) -> str:
    value = _clean_text(value, 253).rstrip(".").lower()
    if "://" in value or "/" in value or "@" in value or "\\" in value:
        raise LookupError("Bitte nur einen Domainnamen eingeben, ohne URL, Pfad oder Zugangsdaten.")
    try:
        value = value.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise LookupError("Der Domainname ist ungültig.") from exc
    if len(value) < 3 or len(value) > 253 or "." not in value:
        raise LookupError("Bitte eine vollständige Domain wie example.com eingeben.")
    if not re.fullmatch(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", value):
        raise LookupError("Der Domainname enthält ungültige Zeichen.")
    return value


def normalize_ip(value: Any) -> str:
    try:
        address = ipaddress.ip_address(_clean_text(value, 64))
    except ValueError as exc:
        raise LookupError("Bitte eine gültige IPv4- oder IPv6-Adresse eingeben.") from exc
    if (not address.is_global or address.is_multicast or address.is_reserved or address.is_unspecified
            or address.is_loopback or address.is_link_local or address.is_private):
        raise LookupError("Private, reservierte und lokale IP-Adressen werden nicht abgefragt.")
    return address.compressed


def normalize_public_url(value: Any) -> urllib.parse.SplitResult:
    text = _clean_text(value, 2048)
    parsed = urllib.parse.urlsplit(text if "://" in text else "https://" + text)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise LookupError("Bitte eine öffentliche HTTPS-Adresse ohne Benutzername oder Passwort eingeben.")
    if parsed.port not in (None, 443):
        raise LookupError("Nur der HTTPS-Standardport 443 wird unterstützt.")
    try:
        domain = normalize_domain(parsed.hostname)
    except LookupError:
        raise LookupError("Bitte eine gültige öffentliche Domain eingeben.") from None
    if domain in {"localhost", "localhost.localdomain"} or domain.endswith(".localhost"):
        raise LookupError("Lokale Hosts werden nicht abgefragt.")
    return parsed._replace(netloc=domain)


def _validate_provider_url(url: str, provider: Upstream) -> urllib.parse.SplitResult:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.hostname.lower() not in provider.hosts:
        raise LookupError("Interner Provider-Fehler: nicht erlaubter Zielhost.")
    if parsed.username or parsed.password or parsed.port not in (None, 443):
        raise LookupError("Unsichere Zieladresse zurückgewiesen.")
    return parsed


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Allow redirects only to HTTPS public hostnames; do not allow IP literals."""
    def __init__(self, allowed_hosts: frozenset[str]):
        super().__init__()
        self.allowed_hosts = allowed_hosts
        self.count = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.count += 1
        if self.count > MAX_REDIRECTS:
            raise LookupError("Zu viele Weiterleitungen vom Lookup-Anbieter.")
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise LookupError("Unsichere Weiterleitung wurde blockiert.")
        try:
            ipaddress.ip_address(parsed.hostname)
        except ValueError:
            # RDAP referrals are restricted to the exact registry hosts returned by our bootstrap allowlist.
            if parsed.hostname in self.allowed_hosts:
                return super().redirect_request(req, fp, code, msg, headers, newurl)
            raise LookupError("Nicht erlaubtes Weiterleitungsziel wurde blockiert.")
        raise LookupError("Weiterleitung auf eine IP-Adresse wurde blockiert.")


def request_bytes(url: str, provider: Upstream, *, method: str = "GET", data: bytes | None = None,
                  headers: dict[str, str] | None = None, redirect_hosts: frozenset[str] | None = None) -> bytes:
    parsed = _validate_provider_url(url, provider)
    req = urllib.request.Request(url, data=data, method=method, headers={
        "User-Agent": USER_AGENT, "Accept": "application/json, text/plain;q=0.9, */*;q=0.5",
        **(headers or {}),
    })
    handlers = [_SafeRedirect(redirect_hosts or provider.hosts)]
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(req, timeout=provider.timeout) as response:
            final = urllib.parse.urlsplit(response.geturl())
            if final.scheme != "https" or not final.hostname:
                raise LookupError("Unsicheres Ziel nach Weiterleitung.")
            payload = response.read(provider.max_bytes + 1)
            if len(payload) > provider.max_bytes:
                raise LookupError("Die Antwort des Anbieters ist zu groß.")
            if not 200 <= response.status < 300:
                raise LookupError(f"{provider.name} antwortete mit HTTP {response.status}.")
            return payload
    except LookupError:
        raise
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise LookupError(f"{provider.name} ist gerade ausgelastet. Bitte später erneut versuchen.") from None
        raise LookupError(f"{provider.name} antwortete mit HTTP {exc.code}.") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LookupError(f"{provider.name} ist derzeit nicht erreichbar.") from exc


def request_json(url: str, provider: Upstream, **kwargs) -> Any:
    payload = request_bytes(url, provider, **kwargs)
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LookupError(f"{provider.name} lieferte keine gültigen JSON-Daten.") from exc


def _url(base: str, **params: Any) -> str:
    return base + ("&" if "?" in base else "?") + urllib.parse.urlencode(params)


def lookup_domain_rdap(query: str) -> dict[str, Any]:
    domain = normalize_domain(query)
    url = f"https://rdap.org/domain/{urllib.parse.quote(domain, safe='.-')}"
    data = request_json(url, PROVIDERS["rdap"], redirect_hosts=frozenset({"rdap.org", "rdap.arin.net", "rdap.verisign.com", "rdap.publicinterestregistry.org", "rdap.nic.io", "rdap.nic.google", "rdap.identitydigital.services"}))
    events = [{"event": x.get("eventAction"), "date": x.get("eventDate")} for x in data.get("events", []) if isinstance(x, dict)]
    nameservers = [x.get("ldhName") for x in data.get("nameservers", []) if isinstance(x, dict) and x.get("ldhName")]
    return {"provider": "RDAP", "domain": data.get("ldhName", domain), "status": data.get("status", []), "events": events,
            "nameservers": nameservers, "entities": len(data.get("entities", [])), "source": "ICANN RDAP"}


def lookup_ip(query: str) -> dict[str, Any]:
    address = normalize_ip(query)
    url = f"https://rdap.org/ip/{urllib.parse.quote(address, safe=':')}"
    data = request_json(url, PROVIDERS["rdap"], redirect_hosts=frozenset({"rdap.org", "rdap.arin.net", "rdap.apnic.net", "rdap.db.ripe.net", "rdap.lacnic.net", "rdap.afrinic.net"}))
    return {"provider": "RDAP", "ip": address, "name": data.get("name"), "handle": data.get("handle"),
            "country": data.get("country"), "startAddress": data.get("startAddress"), "endAddress": data.get("endAddress"),
            "entities": len(data.get("entities", [])), "source": "Regional Internet Registry RDAP"}


def lookup_dns(query: str, record_type: str) -> dict[str, Any]:
    domain = normalize_domain(query)
    if record_type not in {"A", "AAAA", "MX", "TXT"}:
        raise LookupError("Dieser DNS-Record-Typ wird nicht unterstützt.")
    url = _url("https://dns.google/resolve", name=domain, type=record_type)
    data = request_json(url, PROVIDERS["dns"])
    return {"provider": "Google Public DNS", "name": domain, "type": record_type,
            "status": data.get("Status"), "answers": [
                {"name": row.get("name"), "type": row.get("type"), "ttl": row.get("TTL"), "data": row.get("data")}
                for row in data.get("Answer", []) if isinstance(row, dict)], "source": "DNS over HTTPS"}


def lookup_dns_addresses(query: str) -> dict[str, Any]:
    domain = normalize_domain(query)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        v4 = pool.submit(lookup_dns, domain, "A")
        v6 = pool.submit(lookup_dns, domain, "AAAA")
        results = {"A": v4.result(), "AAAA": v6.result()}
    return {"domain": domain, "records": results, "source": "Google Public DNS over HTTPS"}


def lookup_tls_certificate(query: str) -> dict[str, Any]:
    domain = normalize_domain(query)
    address = _resolve_public(domain, 443)
    context = ssl.create_default_context()
    try:
        with socket.create_connection((address, 443), timeout=10) as raw:
            with context.wrap_socket(raw, server_hostname=domain) as secured:
                cert = secured.getpeercert()
                cipher = secured.cipher()
                return {
                    "domain": domain, "address": address, "protocol": secured.version(),
                    "cipher": cipher[0] if cipher else None,
                    "subject": [[list(item) for item in group] for group in cert.get("subject", ())],
                    "issuer": [[list(item) for item in group] for group in cert.get("issuer", ())],
                    "notBefore": cert.get("notBefore"), "notAfter": cert.get("notAfter"),
                    "subjectAltNames": [name for kind, name in cert.get("subjectAltName", ()) if kind == "DNS"][:100],
                    "source": "Live TLS handshake to validated public IP on port 443",
                }
    except (OSError, ssl.SSLError) as exc:
        raise LookupError("Der TLS-Endpunkt konnte nicht sicher abgefragt werden.") from exc


def lookup_dmarc(query: str) -> dict[str, Any]:
    domain = normalize_domain(query)
    # `_dmarc.example.com` is a valid DNS name but not a registrable domain, so query the provider directly.
    data = request_json(_url("https://dns.google/resolve", name="_dmarc." + domain, type="TXT"), PROVIDERS["dns"])
    return {"provider": "Google Public DNS", "name": "_dmarc." + domain, "type": "TXT",
            "status": data.get("Status"), "answers": [x.get("data") for x in data.get("Answer", []) if isinstance(x, dict)],
            "source": "DNS over HTTPS"}


def lookup_dns_propagation(query: str, record_type: str = "A") -> dict[str, Any]:
    domain = normalize_domain(query)
    if record_type not in {"A", "AAAA", "MX", "TXT"}:
        raise LookupError("Dieser DNS-Record-Typ wird nicht unterstützt.")
    providers = [("Google Public DNS", "https://dns.google/resolve"), ("Cloudflare DNS", "https://cloudflare-dns.com/dns-query")]
    def check(item):
        label, base = item
        try:
            provider_data = request_json(_url(base, name=domain, type=record_type), PROVIDERS["dns"], headers={"Accept": "application/dns-json"})
            return {"resolver": label, "status": provider_data.get("Status"), "answers": [x.get("data") for x in provider_data.get("Answer", []) if isinstance(x, dict)]}
        except LookupError as exc:
            return {"resolver": label, "error": str(exc)}
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        answers = list(pool.map(check, providers))
    return {"name": domain, "type": record_type, "resolvers": answers}


def lookup_certificates(query: str) -> dict[str, Any]:
    domain = normalize_domain(query)
    url = _url("https://crt.sh/", q="%." + domain, output="json")
    data = request_json(url, PROVIDERS["ct"])
    seen, rows = set(), []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        names = item.get("name_value", "").splitlines()
        for name in names:
            value = name.strip().lower()
            if value and value not in seen:
                seen.add(value)
                rows.append({"name": value, "issuer": item.get("issuer_name"), "notBefore": item.get("not_before"), "notAfter": item.get("not_after")})
    return {"domain": domain, "certificates": rows[:300], "count": len(rows), "source": "crt.sh public CT logs"}


def lookup_ssllabs(query: str) -> dict[str, Any]:
    domain = normalize_domain(query)
    url = _url("https://api.ssllabs.com/api/v3/analyze", host=domain, publish="off", all="done", fromCache="on")
    data = request_json(url, PROVIDERS["ssllabs"])
    endpoints = [{"ip": x.get("ipAddress"), "grade": x.get("grade"), "status": x.get("statusMessage"),
                  "details": x.get("details", {}).get("gradeTrustIgnored") if isinstance(x.get("details"), dict) else None}
                 for x in data.get("endpoints", []) if isinstance(x, dict)]
    return {"domain": domain, "status": data.get("status"), "statusMessage": data.get("statusMessage"), "endpoints": endpoints,
            "source": "SSL Labs API; report may be queued or cached"}


def lookup_urlscan(query: str) -> dict[str, Any]:
    target = _clean_text(query, 2048)
    if not target or any(ch.isspace() for ch in target):
        raise LookupError("Gib eine Domain oder URL ohne Leerzeichen ein.")
    url = _url("https://urlscan.io/api/v1/search/", q=target)
    data = request_json(url, PROVIDERS["urlscan"])
    results = []
    for item in data.get("results", [])[:30]:
        page = item.get("page") or {}
        task = item.get("task") or {}
        results.append({"url": page.get("url"), "domain": page.get("domain"), "ip": page.get("ip"),
                        "country": page.get("country"), "verdicts": item.get("verdicts"),
                        "result": task.get("result"), "time": task.get("time")})
    return {"query": target, "total": data.get("total"), "results": results, "source": "urlscan.io public search"}


def lookup_urlhaus(query: str) -> dict[str, Any]:
    target = normalize_domain(query)
    body = urllib.parse.urlencode({"host": target}).encode("ascii")
    data = request_json("https://urlhaus-api.abuse.ch/v1/host/", PROVIDERS["urlhaus"], method="POST", data=body,
                        headers={"Content-Type": "application/x-www-form-urlencoded"})
    urls = [{"url": x.get("url"), "threat": x.get("threat"), "status": x.get("url_status"), "dateAdded": x.get("date_added")}
            for x in data.get("urls", [])[:100] if isinstance(x, dict)]
    return {"host": target, "queryStatus": data.get("query_status"), "urls": urls, "source": "URLhaus abuse.ch"}


def lookup_virustotal(query: str, api_key: str) -> dict[str, Any]:
    raw = _clean_text(query, 256)
    try:
        ipaddress.ip_address(raw)
    except ValueError:
        target = normalize_domain(raw)
        endpoint = "domains"
        target_type = "domain"
    else:
        target = normalize_ip(raw)
        endpoint = "ip_addresses"
        target_type = "IP"
    key = _clean_text(api_key, 256)
    if not key:
        raise LookupError("VirusTotal benötigt deinen persönlichen API-Schlüssel. Er wird nur für diese Anfrage verwendet und nicht gespeichert.")
    url = f"https://www.virustotal.com/api/v3/{endpoint}/{urllib.parse.quote(target, safe='.-:')}"
    data = request_json(url, PROVIDERS["virustotal"], headers={"x-apikey": key})
    attrs = ((data.get("data") or {}).get("attributes") or {})
    stats = attrs.get("last_analysis_stats") or {}
    return {"target": target, "targetType": target_type, "reputation": attrs.get("reputation"),
            "lastAnalysisDate": attrs.get("last_analysis_date"), "analysisStats": stats,
            "categories": attrs.get("categories"), "source": "VirusTotal API v3"}


def lookup_wayback(query: str) -> dict[str, Any]:
    domain = normalize_domain(query)
    url = _url("https://web.archive.org/cdx/search/cdx", url=domain + "/*", output="json",
               filter="statuscode:200", collapse="digest", fl="timestamp,original,statuscode", limit="200")
    data = request_json(url, PROVIDERS["wayback"])
    rows = []
    for row in data[1:] if isinstance(data, list) and data else []:
        if isinstance(row, list) and len(row) >= 3:
            rows.append({"timestamp": row[0], "url": row[1], "status": row[2]})
    return {"domain": domain, "snapshots": rows, "count": len(rows), "source": "Internet Archive CDX API"}


def lookup_github_user(query: str) -> dict[str, Any]:
    username = _clean_text(query, 39)
    if not re.fullmatch(r"[A-Za-z0-9-]{1,39}", username):
        raise LookupError("GitHub-Namen dürfen hier nur Buchstaben, Zahlen und Bindestriche enthalten.")
    data = request_json(f"https://api.github.com/users/{urllib.parse.quote(username)}", PROVIDERS["github"],
                        headers={"Accept": "application/vnd.github+json"})
    return {"login": data.get("login"), "name": data.get("name"), "bio": data.get("bio"), "company": data.get("company"),
            "location": data.get("location"), "blog": data.get("blog"), "publicRepos": data.get("public_repos"),
            "followers": data.get("followers"), "following": data.get("following"), "createdAt": data.get("created_at"),
            "profile": data.get("html_url"), "source": "GitHub REST API"}


def lookup_github_repos(query: str) -> dict[str, Any]:
    value = _clean_text(query, 160)
    if len(value) < 2:
        raise LookupError("Gib mindestens zwei Zeichen als Suchbegriff ein.")
    data = request_json(_url("https://api.github.com/search/repositories", q=value, per_page=30, sort="stars"), PROVIDERS["github"],
                        headers={"Accept": "application/vnd.github+json"})
    rows = [{"name": x.get("full_name"), "description": x.get("description"), "stars": x.get("stargazers_count"),
             "language": x.get("language"), "updatedAt": x.get("updated_at"), "url": x.get("html_url")}
            for x in data.get("items", []) if isinstance(x, dict)]
    return {"query": value, "total": data.get("total_count"), "repositories": rows, "source": "GitHub REST API"}


def lookup_github_users(query: str) -> dict[str, Any]:
    value = _clean_text(query, 100)
    if len(value) < 2:
        raise LookupError("Gib mindestens zwei Zeichen als Suchbegriff ein.")
    data = request_json(_url("https://api.github.com/search/users", q=value, per_page=30), PROVIDERS["github"],
                        headers={"Accept": "application/vnd.github+json"})
    return {"query": value, "total": data.get("total_count"),
            "users": [{"login": x.get("login"), "url": x.get("html_url"), "avatar": x.get("avatar_url")}
                      for x in data.get("items", []) if isinstance(x, dict)], "scope": "GitHub only", "source": "GitHub REST API"}


def _resolve_public(host: str, port: int) -> str:
    try:
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise LookupError("Die Website konnte nicht aufgelöst werden.") from exc
    addresses = sorted({row[4][0] for row in records})
    if not addresses:
        raise LookupError("Für die Domain wurde keine IP-Adresse gefunden.")
    for item in addresses:
        ip = ipaddress.ip_address(item.split("%", 1)[0])
        if not ip.is_global:
            raise LookupError("Private oder reservierte IP-Adressen werden nicht abgefragt.")
    return addresses[0]


class _PinnedHTTPSConnection(HTTPSConnection):
    """HTTPS connection pinned to a previously validated public IP, preserving SNI."""
    def __init__(self, host: str, ip: str, timeout: float):
        super().__init__(host, 443, timeout=timeout, context=ssl.create_default_context())
        self._pinned_ip = ip

    def connect(self):
        raw = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


def safe_fetch_site(value: str) -> dict[str, Any]:
    """Fetch a small public HTTPS page without following redirects or allowing SSRF."""
    parsed = normalize_public_url(value)
    host = parsed.hostname or ""
    ip = _resolve_public(host, 443)
    connection = _PinnedHTTPSConnection(host, ip, 10.0)
    path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    try:
        connection.request("GET", path, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1"})
        response: HTTPResponse = connection.getresponse()
        headers = {k.lower(): v[:1000] for k, v in response.getheaders() if k.lower() in {
            "server", "content-type", "content-security-policy", "strict-transport-security", "x-frame-options",
            "x-content-type-options", "referrer-policy", "permissions-policy", "cache-control", "location",
            "x-powered-by", "via", "alt-svc", "access-control-allow-origin"}}
        if 300 <= response.status < 400:
            return {"url": f"https://{host}{path}", "status": response.status, "headers": headers,
                    "redirect": headers.get("location"), "redirectFollowed": False, "body": ""}
        content_type = headers.get("content-type", "")
        body = response.read(MAX_SITE_BYTES + 1)
        if len(body) > MAX_SITE_BYTES:
            body = body[:MAX_SITE_BYTES]
        try:
            text = body.decode("utf-8", "replace")
        except Exception:
            text = ""
        return {"url": f"https://{host}{path}", "status": response.status, "headers": headers,
                "contentType": content_type, "body": text[:MAX_SITE_BYTES], "bytesRead": len(body)}
    except (OSError, ssl.SSLError, TimeoutError) as exc:
        raise LookupError("Die öffentliche Website konnte nicht sicher geladen werden.") from exc
    finally:
        connection.close()


def lookup_security_headers(query: str) -> dict[str, Any]:
    result = safe_fetch_site(query)
    headers = result.get("headers", {})
    recommendations = {
        "strict-transport-security": "HSTS schützt HTTPS-Verbindungen vor Downgrade-Angriffen.",
        "content-security-policy": "CSP begrenzt, welche Quellen Inhalte laden dürfen.",
        "x-content-type-options": "nosniff verhindert bestimmte MIME-Type-Verwechslungen.",
        "x-frame-options": "Schränkt Einbettung in Frames ein; alternativ CSP frame-ancestors.",
        "referrer-policy": "Legt fest, wie viele Referrer-Informationen gesendet werden.",
        "permissions-policy": "Begrenzt Browser-Funktionen wie Kamera und Mikrofon.",
    }
    names = list(recommendations)
    return {"url": result["url"], "status": result["status"], "present": {k: headers[k] for k in names if k in headers},
            "missing": [{"header": k, "why": v} for k, v in recommendations.items() if k not in headers],
            "redirect": result.get("redirect"), "source": "Direct HTTPS response; redirect is not followed"}


def lookup_technology(query: str) -> dict[str, Any]:
    result = safe_fetch_site(query)
    body = result.get("body", "")
    headers = result.get("headers", {})
    sample = (body[:MAX_SITE_BYTES]).lower()
    matches = []
    checks = [
        ("WordPress", "wp-content"), ("Drupal", "drupal.settings"), ("Joomla", "joomla"),
        ("Shopify", "cdn.shopify.com"), ("Wix", "wixstatic.com"), ("Squarespace", "squarespace.com"),
        ("React", "reactroot"), ("Next.js", "_next/static"), ("Vue", "__vue__"),
        ("Angular", "ng-version"), ("Svelte", "svelte"), ("jQuery", "jquery"),
        ("Google Analytics", "google-analytics.com"), ("Google Tag Manager", "googletagmanager.com"),
        ("Cloudflare", "cf-ray"), ("Bootstrap", "bootstrap.min.css"),
    ]
    for product, needle in checks:
        if needle in sample or (product == "Cloudflare" and "cf-ray" in headers):
            matches.append(product)
    powered = headers.get("x-powered-by") or headers.get("server")
    return {"url": result["url"], "status": result["status"], "technologies": matches,
            "serverHeader": powered, "detectionNote": "Heuristics from a small public HTML sample and selected response headers; not a definitive inventory.",
            "source": "FIELDKIT local fingerprint rules"}


LOOKUP_HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {
    "rdap": lookup_domain_rdap,
    "ip": lookup_ip,
    "dns-a": lookup_dns_addresses,
    "dns-txt": lambda q: lookup_dns(q, "TXT"),
    "tls-cert": lookup_tls_certificate,
    "dns-mx": lambda q: lookup_dns(q, "MX"),
    "spf": lambda q: lookup_dns(q, "TXT"),
    "dmarc": lookup_dmarc,
    "ct": lookup_certificates,
    "ssl": lookup_ssllabs,
    "headers": lookup_security_headers,
    "urlscan": lookup_urlscan,
    "urlhaus": lookup_urlhaus,
    "vt": lookup_virustotal,
    "dnscheck": lookup_dns_propagation,
    "tech": lookup_technology,
    "archive": lookup_wayback,
    "gh-user": lookup_github_user,
    "gh-search": lookup_github_repos,
    "user-search": lookup_github_users,
}


def perform_lookup(tool: str, query: Any, *, api_key: str = "") -> dict[str, Any]:
    handler = LOOKUP_HANDLERS.get(tool)
    if handler is None:
        raise LookupError("Unbekanntes Lookup-Werkzeug.")
    query = _clean_text(query, 2048)
    if tool == "vt":
        result = handler(query, api_key)
    else:
        result = handler(query)
    result["lookup"] = tool
    result["queriedAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return result
