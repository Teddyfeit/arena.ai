"""Mocked provider and local HTTP integration tests for FIELDKIT lookups."""
import json
import re
import sqlite3
import subprocess
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.request import Request, urlopen

import tool_hub as lookups
import tool_hub


class InputValidationTests(unittest.TestCase):
    def test_normalizes_ascii_and_idn_domains(self):
        self.assertEqual(lookups.normalize_domain("Example.COM."), "example.com")
        self.assertEqual(lookups.normalize_domain("münich.de"), "xn--mnich-kva.de")

    def test_rejects_urls_and_malformed_domains(self):
        for value in ("https://example.com", "example.com/path", "user@example.com", "localhost", "-bad.example", "bad..example", "bad_domain.example", "a" * 64 + ".com"):
            with self.subTest(value=value), self.assertRaises(lookups.LookupError):
                lookups.normalize_domain(value)

    def test_rejects_empty_and_control_text(self):
        for value in ("", "  ", "example.com\nX: y"):
            with self.subTest(value=value), self.assertRaises(lookups.LookupError):
                lookups.normalize_domain(value)

    def test_public_ip_validation(self):
        self.assertEqual(lookups.normalize_ip("8.8.8.8"), "8.8.8.8")
        self.assertEqual(lookups.normalize_ip("2001:4860:4860::8888"), "2001:4860:4860::8888")
        for value in ("127.0.0.1", "10.1.2.3", "192.168.1.1", "169.254.1.1", "224.0.0.1", "not-an-ip"):
            with self.subTest(value=value), self.assertRaises(lookups.LookupError):
                lookups.normalize_ip(value)

    def test_public_url_rules(self):
        parsed = lookups.normalize_public_url("example.com/path?q=1")
        self.assertEqual((parsed.scheme, parsed.hostname, parsed.path), ("https", "example.com", "/path"))
        for value in ("http://example.com", "https://user:pass@example.com", "https://example.com:8443", "https://localhost"):
            with self.subTest(value=value), self.assertRaises(lookups.LookupError):
                lookups.normalize_public_url(value)

    def test_provider_allowlist_rejects_arbitrary_url(self):
        with self.assertRaises(lookups.LookupError):
            lookups.request_json("https://attacker.example/data", lookups.PROVIDERS["dns"])


class ProviderTests(unittest.TestCase):
    @patch.object(lookups, "request_json")
    def test_domain_rdap_normalizes_summary(self, request_json):
        request_json.return_value = {"ldhName": "EXAMPLE.COM", "status": ["active"], "events": [{"eventAction": "registration", "eventDate": "2020-01-01"}], "nameservers": [{"ldhName": "NS1.EXAMPLE.COM"}], "entities": [{"handle": "R1"}]}
        result = lookups.lookup_domain_rdap("Example.com")
        self.assertEqual(result["domain"], "EXAMPLE.COM")
        self.assertEqual(result["nameservers"], ["NS1.EXAMPLE.COM"])
        self.assertEqual(result["entities"], 1)

    @patch.object(lookups, "request_json")
    def test_ip_rdap_summary(self, request_json):
        request_json.return_value = {"name": "Google", "handle": "NET-8-8-8-0-1", "country": "US", "startAddress": "8.8.8.0", "endAddress": "8.8.8.255", "entities": []}
        result = lookups.lookup_ip("8.8.8.8")
        self.assertEqual(result["ip"], "8.8.8.8")
        self.assertEqual(result["country"], "US")

    @patch.object(lookups, "request_json")
    def test_dns_address_lookup_returns_a_and_aaaa(self, request_json):
        request_json.side_effect = [{"Status": 0, "Answer": [{"data": "93.184.216.34"}]}, {"Status": 0, "Answer": [{"data": "2001:db8::1"}]}]
        result = lookups.lookup_dns_addresses("example.com")
        self.assertEqual(set(result["records"]), {"A", "AAAA"})
        self.assertEqual(request_json.call_count, 2)

    @patch.object(lookups, "request_json")
    def test_dns_txt_maps_answer_fields(self, request_json):
        request_json.return_value = {"Status": 0, "Answer": [{"name": "example.com.", "type": 16, "TTL": 60, "data": '"v=spf1 -all"'}]}
        result = lookups.lookup_dns("example.com", "TXT")
        self.assertIn("spf1", result["answers"][0]["data"])
        self.assertEqual(result["answers"][0]["ttl"], 60)

    @patch.object(lookups, "request_json")
    def test_dns_rejects_unsupported_type_without_network(self, request_json):
        with self.assertRaises(lookups.LookupError):
            lookups.lookup_dns("example.com", "AXFR")
        request_json.assert_not_called()

    @patch.object(lookups, "request_json")
    def test_dmarc_queries_policy_owner_name(self, request_json):
        request_json.return_value = {"Status": 0, "Answer": [{"data": '"v=DMARC1; p=none"'}]}
        result = lookups.lookup_dmarc("example.com")
        self.assertEqual(result["name"], "_dmarc.example.com")
        self.assertIn("_dmarc.example.com", request_json.call_args.args[0])

    @patch.object(lookups, "request_json")
    def test_dns_propagation_reports_resolvers_and_partial_failure(self, request_json):
        request_json.side_effect = [lookups.LookupError("offline"), {"Status": 0, "Answer": [{"data": "203.0.113.5"}]}]
        result = lookups.lookup_dns_propagation("example.com")
        self.assertEqual(len(result["resolvers"]), 2)
        self.assertTrue(any("error" in row for row in result["resolvers"]))

    @patch.object(lookups, "request_json")
    def test_certificates_are_deduplicated_and_limited(self, request_json):
        request_json.return_value = [{"name_value": "www.example.com\nmail.example.com", "issuer_name": "Issuer"}, {"name_value": "www.example.com", "issuer_name": "Other"}]
        result = lookups.lookup_certificates("example.com")
        self.assertEqual(result["count"], 2)
        self.assertEqual({x["name"] for x in result["certificates"]}, {"www.example.com", "mail.example.com"})

    def test_tls_certificate_uses_validated_ip_and_sni(self):
        raw_context = MagicMock()
        raw = MagicMock()
        raw.__enter__.return_value = raw
        raw_context.__enter__.return_value = raw
        secured_context = MagicMock()
        secured = secured_context.__enter__.return_value
        secured.getpeercert.return_value = {
            "subject": ((("commonName", "example.com"),),),
            "issuer": ((("commonName", "Test CA"),),),
            "notAfter": "Jan  1 00:00:00 2030 GMT",
            "subjectAltName": (("DNS", "example.com"),),
        }
        secured.cipher.return_value = ("TLS_AES_128_GCM_SHA256", "TLSv1.3", 128)
        secured.version.return_value = "TLSv1.3"
        tls_context = MagicMock()
        tls_context.wrap_socket.return_value = secured_context
        with (
            patch.object(lookups, "_resolve_public", return_value="93.184.216.34"),
            patch.object(lookups.socket, "create_connection", return_value=raw_context) as connect,
            patch.object(lookups.ssl, "create_default_context", return_value=tls_context),
        ):
            result = lookups.lookup_tls_certificate("example.com")
        connect.assert_called_once_with(("93.184.216.34", 443), timeout=10)
        tls_context.wrap_socket.assert_called_once_with(raw, server_hostname="example.com")
        self.assertEqual(result["protocol"], "TLSv1.3")
        self.assertEqual(result["subjectAltNames"], ["example.com"])

    @patch.object(lookups, "request_json")
    def test_ssl_labs_result_shape(self, request_json):
        request_json.return_value = {"status": "READY", "endpoints": [{"ipAddress": "203.0.113.4", "grade": "A+", "statusMessage": "Ready"}]}
        result = lookups.lookup_ssllabs("example.com")
        self.assertEqual(result["endpoints"][0]["grade"], "A+")

    @patch.object(lookups, "request_json")
    def test_urlscan_result_shape(self, request_json):
        request_json.return_value = {"total": 4, "results": [{"page": {"url": "https://example.com", "domain": "example.com"}, "task": {"time": "2025-01-01"}}]}
        result = lookups.lookup_urlscan("example.com")
        self.assertEqual(result["total"], 4)
        self.assertEqual(result["results"][0]["domain"], "example.com")

    @patch.object(lookups, "request_json")
    def test_urlhaus_posts_form_data(self, request_json):
        request_json.return_value = {"query_status": "no_results", "urls": []}
        result = lookups.lookup_urlhaus("example.com")
        self.assertEqual(result["queryStatus"], "no_results")
        self.assertEqual(request_json.call_args.kwargs["method"], "POST")
        self.assertIn(b"host=example.com", request_json.call_args.kwargs["data"])

    @patch.object(lookups, "request_json")
    def test_virustotal_requires_key_and_sends_it_only_in_header(self, request_json):
        with self.assertRaises(lookups.LookupError):
            lookups.lookup_virustotal("example.com", "")
        request_json.assert_not_called()
        request_json.return_value = {"data": {"attributes": {"reputation": 4, "last_analysis_stats": {"malicious": 0}}}}
        result = lookups.lookup_virustotal("example.com", "unit-test-key")
        self.assertEqual(result["reputation"], 4)
        self.assertEqual(request_json.call_args.kwargs["headers"]["x-apikey"], "unit-test-key")
        self.assertNotIn("unit-test-key", json.dumps(result))
        request_json.return_value = {"data": {"attributes": {"reputation": 1}}}
        ip_result = lookups.lookup_virustotal("8.8.8.8", "unit-test-key")
        self.assertEqual(ip_result["targetType"], "IP")
        self.assertIn("/ip_addresses/8.8.8.8", request_json.call_args.args[0])
        with self.assertRaises(lookups.LookupError):
            lookups.lookup_virustotal("127.0.0.1", "unit-test-key")

    @patch.object(lookups, "request_json")
    def test_wayback_parses_snapshots(self, request_json):
        request_json.return_value = [["timestamp", "original", "statuscode"], ["20200101000000", "https://example.com/", "200"]]
        result = lookups.lookup_wayback("example.com")
        self.assertEqual(result["snapshots"][0]["timestamp"], "20200101000000")

    @patch.object(lookups, "request_json")
    def test_github_handlers_return_scoped_public_data(self, request_json):
        request_json.return_value = {"login": "octocat", "public_repos": 8, "followers": 2, "html_url": "https://github.com/octocat"}
        self.assertEqual(lookups.lookup_github_user("octocat")["publicRepos"], 8)
        request_json.return_value = {"total_count": 2, "items": [{"full_name": "owner/repo", "stargazers_count": 42}]}
        self.assertEqual(lookups.lookup_github_repos("lookup tool")["repositories"][0]["stars"], 42)
        request_json.return_value = {"total_count": 1, "items": [{"login": "example", "html_url": "https://github.com/example"}]}
        result = lookups.lookup_github_users("example")
        self.assertEqual(result["scope"], "GitHub only")
        self.assertIn("api.github.com", request_json.call_args.args[0])

    @patch.object(lookups, "safe_fetch_site")
    def test_security_header_check_reports_missing_recommendations(self, fetch):
        fetch.return_value = {"url": "https://example.com/", "status": 200, "headers": {"strict-transport-security": "max-age=31536000"}}
        result = lookups.lookup_security_headers("example.com")
        self.assertIn("strict-transport-security", result["present"])
        self.assertTrue(any(x["header"] == "content-security-policy" for x in result["missing"]))

    @patch.object(lookups, "safe_fetch_site")
    def test_technology_lookup_discloses_heuristic_limits(self, fetch):
        fetch.return_value = {"url": "https://example.com/", "status": 200, "headers": {"server": "cloudflare", "cf-ray": "abc"}, "body": '<script src="/wp-content/app.js"></script><div id="reactRoot"></div>'}
        result = lookups.lookup_technology("example.com")
        self.assertIn("WordPress", result["technologies"])
        self.assertIn("React", result["technologies"])
        self.assertIn("Heuristics", result["detectionNote"])

    def test_dispatcher_rejects_unknown_and_adds_metadata(self):
        with self.assertRaises(lookups.LookupError):
            lookups.perform_lookup("unknown", "example.com")
        with patch.object(lookups, "lookup_github_user", return_value={"login": "abc"}):
            result = lookups.perform_lookup("gh-user", "abc")
        self.assertEqual(result["lookup"], "gh-user")
        self.assertTrue(result["queriedAt"].endswith("+00:00"))


class NetworkSafetyTests(unittest.TestCase):
    def test_public_resolver_blocks_private_or_mixed_addresses(self):
        cases = [[(2, 1, 6, "", ("127.0.0.1", 443))], [(2, 1, 6, "", ("93.184.216.34", 443)), (2, 1, 6, "", ("10.0.0.1", 443))]]
        for rows in cases:
            with patch.object(lookups.socket, "getaddrinfo", return_value=rows), self.assertRaises(lookups.LookupError):
                lookups._resolve_public("public-looking.example", 443)

    def test_pinned_website_fetch_does_not_follow_redirects(self):
        response = MagicMock(status=302)
        response.getheaders.return_value = [("Location", "http://127.0.0.1/")]
        response.read.return_value = b""
        connection = MagicMock()
        connection.getresponse.return_value = response
        with patch.object(lookups, "_resolve_public", return_value="93.184.216.34"), patch.object(lookups, "_PinnedHTTPSConnection", return_value=connection):
            result = lookups.safe_fetch_site("https://example.com")
        self.assertFalse(result["redirectFollowed"])
        connection.request.assert_called_once()

    def test_redirects_reject_ip_literals_and_unlisted_domains(self):
        redirect = lookups._SafeRedirect(frozenset({"rdap.arin.net"}))
        request = lookups.urllib.request.Request("https://rdap.org/example")
        for target in ("https://93.184.216.34/data", "https://attacker.org/data"):
            with self.subTest(target=target), self.assertRaises(lookups.LookupError):
                redirect.redirect_request(request, MagicMock(), 302, "Found", {}, target)
        self.assertIsNotNone(redirect.redirect_request(request, MagicMock(), 302, "Found", {}, "https://rdap.arin.net/ip/8.8.8.8"))

    def test_response_size_limit_is_enforced(self):
        response = MagicMock(status=200)
        response.geturl.return_value = "https://dns.google/resolve"
        response.read.return_value = b"x" * (lookups.PROVIDERS["dns"].max_bytes + 1)
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = response
        with patch.object(lookups.urllib.request, "build_opener", return_value=opener), self.assertRaises(lookups.LookupError):
            lookups.request_bytes("https://dns.google/resolve", lookups.PROVIDERS["dns"])


class LocalStateAndHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_db, cls.original_lock = tool_hub.DB, tool_hub.dblock
        tool_hub.DB = sqlite3.connect(":memory:", check_same_thread=False)
        tool_hub.dblock = threading.Lock()
        with tool_hub.dblock:
            tool_hub.DB.execute("create table cache(url text primary key, code integer, body blob, expires real)")
            tool_hub.DB.execute("create table lookup_history(id integer primary key autoincrement, tool text not null, query text not null, result text not null, created real not null)")
            tool_hub.DB.commit()
        tool_hub.LOOKUP_REQUESTS.clear()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), tool_hub.H)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)
        tool_hub.DB.close()
        tool_hub.DB, tool_hub.dblock = cls.original_db, cls.original_lock

    def setUp(self):
        tool_hub.LOOKUP_REQUESTS.clear()
        with tool_hub.dblock:
            tool_hub.DB.execute("delete from cache")
            tool_hub.DB.execute("delete from lookup_history")
            tool_hub.DB.commit()

    def get_json(self, path):
        with urlopen(self.base + path, timeout=3) as response:
            return response.status, response.read(), response.headers

    def test_application_entrypoint_is_standalone(self):
        app = Path(__file__).with_name("tool_hub.py")
        result = subprocess.run([sys.executable, str(app), "--help"], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("public lookup tools", result.stdout)
        self.assertIn("--avatar-only", result.stdout)

    def test_root_and_avatar_routes_and_health(self):
        code, body, headers = self.get_json("/")
        self.assertEqual(code, 200)
        self.assertIn(b"Lookup Desk", body)
        self.assertIn(b"const tools=", body)
        code, avatar, _ = self.get_json("/avatar")
        self.assertEqual(code, 200)
        self.assertIn(b"Avatar Finder", avatar)
        self.assertEqual(json.loads(self.get_json("/health")[1])["ok"], True)

    def test_exactly_twenty_cards_map_one_to_one_to_handlers(self):
        match = re.search(r"const tools=(\[.*?\]);let categories=", tool_hub.HUB_HTML, re.S)
        self.assertIsNotNone(match)
        cards = json.loads(match.group(1))
        lookup_ids = {x["id"] for x in cards if x["kind"] == "lookup"}
        self.assertEqual(len(lookup_ids), 20)
        self.assertEqual(lookup_ids, set(lookups.LOOKUP_HANDLERS))

    def test_liquid_glass_controls_and_shared_preferences_exist(self):
        for control in ("themeToggle", "prefsToggle", "accentOptions", "wallOptions", "blurRange", "densityRange", "motionToggle"):
            with self.subTest(control=control):
                self.assertIn(f'id="{control}"', tool_hub.HUB_HTML)
        for preference in ("af-theme", "af-accent", "af-wallpaper", "af-glass-blur", "af-motion"):
            with self.subTest(preference=preference):
                self.assertIn(preference, tool_hub.HUB_HTML)
                self.assertIn(preference, tool_hub.PAGE)
        self.assertIn("classList.contains('dark')", tool_hub.PAGE)

    def test_cache_avoids_repeat_and_history_excludes_api_key(self):
        with patch.object(tool_hub, "perform_lookup", return_value={"lookup": "vt", "domain": "example.com"}) as provider:
            first, cached_first = tool_hub.run_cached_lookup("vt", "example.com", "secret-key")
            second, cached_second = tool_hub.run_cached_lookup("vt", "example.com", "secret-key")
        self.assertFalse(cached_first)
        self.assertTrue(cached_second)
        self.assertEqual(first, second)
        provider.assert_called_once()
        entry = tool_hub.get_lookup_history(tool_hub.list_lookup_history()[0]["id"])
        self.assertNotIn("secret-key", json.dumps(entry))
        self.assertNotIn("secret-key", tool_hub.DB.execute("select url from cache").fetchone()[0])

    def test_history_clear_keeps_cache(self):
        with patch.object(tool_hub, "perform_lookup", return_value={"ok": True}):
            tool_hub.run_cached_lookup("gh-user", "person")
        tool_hub.clear_lookup_history()
        self.assertEqual(tool_hub.list_lookup_history(), [])
        self.assertEqual(tool_hub.DB.execute("select count(*) from cache").fetchone()[0], 1)

    def test_lookup_http_success_then_cached_result(self):
        with patch.object(tool_hub, "perform_lookup", return_value={"lookup": "gh-user", "login": "abc"}) as provider:
            request = Request(self.base + "/lookup/gh-user", data=b'{"query":"abc"}', headers={"Content-Type": "application/json"}, method="POST")
            with urlopen(request, timeout=3) as response:
                first = json.loads(response.read())
            request = Request(self.base + "/lookup/gh-user", data=b'{"query":"abc"}', headers={"Content-Type": "application/json"}, method="POST")
            with urlopen(request, timeout=3) as response:
                second = json.loads(response.read())
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(first["result"]["login"], "abc")
        provider.assert_called_once()

    def test_lookup_http_maps_validation_errors_to_400(self):
        request = Request(self.base + "/lookup/no-such-tool", data=b'{"query":"x"}', headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(Exception) as caught:
            urlopen(request, timeout=3)
        self.assertEqual(caught.exception.code, 400)
        self.assertIn("Unbekanntes Lookup", caught.exception.read().decode())

    def test_lookup_http_rejects_bad_json(self):
        request = Request(self.base + "/lookup/gh-user", data=b"{ ", headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(Exception) as caught:
            urlopen(request, timeout=3)
        self.assertEqual(caught.exception.code, 400)

    def test_history_get_delete_routes(self):
        with patch.object(tool_hub, "perform_lookup", return_value={"ok": True}):
            tool_hub.run_cached_lookup("gh-user", "abc")
        code, body, _ = self.get_json("/lookup/history?limit=4")
        self.assertEqual(code, 200)
        history = json.loads(body)["history"]
        self.assertEqual(len(history), 1)
        request = Request(self.base + "/lookup/history", method="DELETE")
        with urlopen(request, timeout=3) as response:
            self.assertEqual(json.loads(response.read()), {"ok": True})
        self.assertEqual(json.loads(self.get_json("/lookup/history")[1])["history"], [])

    def test_per_client_rate_limit(self):
        with patch.object(tool_hub.time, "monotonic", return_value=1000):
            self.assertTrue(all(tool_hub.allow_lookup_request("unit-client") for _ in range(24)))
            self.assertFalse(tool_hub.allow_lookup_request("unit-client"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
