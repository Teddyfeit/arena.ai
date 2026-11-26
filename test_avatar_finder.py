"""Offline HTTP and UI checks for the standalone Roblox Avatar Finder."""
import json
import sqlite3
import subprocess
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import tool_hub


class AvatarFinderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_db = tool_hub.DB
        cls.original_lock = tool_hub.dblock
        tool_hub.DB = sqlite3.connect(":memory:", check_same_thread=False)
        tool_hub.dblock = threading.Lock()
        with tool_hub.dblock:
            tool_hub.DB.execute("create table cache(url text primary key, code integer, body blob, expires real)")
            tool_hub.DB.execute("create table recent(user_id text primary key, username text, display_name text, avatar_url text, ts real)")
            tool_hub.DB.commit()
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
        with tool_hub.dblock:
            tool_hub.DB.execute("delete from cache")
            tool_hub.DB.execute("delete from recent")
            tool_hub.DB.commit()

    def request(self, path, *, method="GET", body=None):
        data = None if body is None else json.dumps(body).encode()
        req = Request(self.base + path, data=data, method=method,
                      headers={"Content-Type": "application/json"} if data is not None else {})
        return urlopen(req, timeout=4)

    def test_single_file_entrypoint_runs_with_both_finders(self):
        file = Path(__file__).with_name("tool_hub.py")
        result = subprocess.run([sys.executable, str(file), "--help"], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Roblox Avatar Finder", result.stdout)
        self.assertIn("OSINT Toolkit", result.stdout)
        self.assertNotIn("public lookup tools", result.stdout)

    def test_root_and_legacy_avatar_route_serve_same_full_finder(self):
        paths = ("/", "/avatar", "/index.html", "/test.html", "/avatar-finder", "/osint", "/osint/username", "/osint/email", "/osint/csint", "/email-lookup", "/cyber-lookup")
        bodies = []
        for path in paths:
            with self.request(path) as response:
                self.assertEqual(response.status, 200)
                self.assertIn("text/html", response.headers.get("Content-Type", ""))
                bodies.append(response.read())
        self.assertTrue(all(body == bodies[0] for body in bodies))
        page = bodies[0].decode()
        self.assertIn("<title>Avatar Finder</title>", page)
        for feature in ("Outfits", "Gruppen", "Zuletzt gesucht", "Favoriten", "Technische Daten"):
            with self.subTest(feature=feature):
                self.assertIn(feature, page)
        self.assertNotIn("Lookup Desk", page)
        self.assertNotIn("20 Lookup-Tools", page)

    def test_health_reports_both_finder_tools(self):
        with self.request("/health") as response:
            info = json.loads(response.read())
        self.assertEqual(info["app"], "FIELDKIT Avatar Finder + OSINT Toolkit")
        self.assertEqual(info["tools"], ["avatar-finder", "osint-toolkit"])
        self.assertGreaterEqual(info["usernameSites"], 400)

    def test_username_search_returns_public_site_results(self):
        expected = [{"site": "GitHub", "status": "found", "url": "https://github.com/example"}]
        with patch.object(tool_hub, "search_public_username", return_value=expected) as search_sites:
            with self.request("/x/username-search?name=example") as response:
                result = json.loads(response.read())
        self.assertEqual(result, {"username": "example", "results": expected})
        search_sites.assert_called_once_with("example")

    def test_username_probe_confirms_account_from_site_specific_api(self):
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit=-1): return b'{"login":"Example"}'
            def geturl(self): return "https://api.github.com/users/Example"
        site = next(row for row in tool_hub.USERNAME_SITES if row[0] == "GitHub")
        with patch("tool_hub.urllib.request.urlopen", return_value=Response()):
            result = tool_hub.check_username_site(site, "example")
        self.assertEqual(result["status"], "found")
        self.assertEqual(result["url"], "https://github.com/example")

    def test_sherlock_catalog_is_bundled_sfw_and_expands_search(self):
        self.assertGreaterEqual(tool_hub.USERNAME_SITE_COUNT, 400)
        self.assertTrue(tool_hub.SHERLOCK_SITES)
        self.assertTrue(all(not site.get("isNSFW") for site in tool_hub.SHERLOCK_SITES))
        self.assertIn(f"Username auf {tool_hub.USERNAME_SITE_COUNT} Websites suchen", tool_hub.PAGE)

    def test_sherlock_manifest_probe_uses_site_specific_message_rule(self):
        site = {"name": "Example", "url": "https://example.com/u/{}", "urlProbe": "https://example.com/api/{}", "errorType": "message", "errorMsg": "User not found"}
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit=-1): return b'{"profile":"ok"}'
            def geturl(self): return "https://example.com/api/Alice"
        with patch("tool_hub.urllib.request.urlopen", return_value=Response()):
            result = tool_hub.check_sherlock_site(site, "Alice")
        self.assertEqual(result, {"site": "Example", "status": "found", "url": "https://example.com/u/Alice"})

    def test_sherlock_html_200_is_possible_not_false_confirmation(self):
        site = {"name": "Example", "url": "https://example.com/u/{}", "errorType": "status_code"}
        class Response:
            status = 200
            headers = {"Content-Type": "text/html"}
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit=-1): return b'<html>generic page</html>'
            def geturl(self): return "https://example.com/u/Alice"
        with patch("tool_hub.urllib.request.urlopen", return_value=Response()):
            result = tool_hub.check_sherlock_site(site, "Alice")
        self.assertEqual(result["status"], "possible")

    def test_sherlock_manifest_treats_waf_as_unknown(self):
        site = {"name": "Example", "url": "https://example.com/u/{}", "errorType": "status_code"}
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit=-1): return b'<html>please complete the CAPTCHA</html>'
            def geturl(self): return "https://example.com/u/Alice"
        with patch("tool_hub.urllib.request.urlopen", return_value=Response()):
            result = tool_hub.check_sherlock_site(site, "Alice")
        self.assertEqual(result["status"], "unknown")

    def test_username_search_rejects_invalid_names(self):
        with patch.object(tool_hub, "search_public_username") as search_sites:
            with self.assertRaises(HTTPError) as error:
                self.request("/x/username-search?name=bad%20name")
        self.assertEqual(error.exception.code, 400)
        search_sites.assert_not_called()

    def test_email_check_returns_domain_only_and_mx_data(self):
        expected = {"domain": "example.com", "mxStatus": "present", "mxRecords": ["10 mail.example.com"], "disposableDomain": False}
        with patch.object(tool_hub, "check_email_domain", return_value=expected) as check:
            with self.request("/x/email-check?email=user%40example.com") as response:
                result = json.loads(response.read())
        self.assertEqual(result, expected)
        check.assert_called_once_with("user@example.com")

    def test_email_domain_checker_checks_mx_spf_and_dmarc(self):
        def records(name, record_type):
            return {("example.com", "MX"): ["10 mail.example.com"],
                    ("example.com", "TXT"): ['"v=spf1 -all"'],
                    ("_dmarc.example.com", "TXT"): ['"v=DMARC1; p=reject"']}[(name, record_type)]
        with patch("tool_hub.dns_records", side_effect=records) as dns:
            result = tool_hub.check_email_domain("user@example.com")
        self.assertEqual(result["mxStatus"], "present")
        self.assertEqual(result["domain"], "example.com")
        self.assertTrue(result["spfRecords"])
        self.assertTrue(result["dmarcRecords"])
        self.assertEqual(dns.call_count, 3)

    def test_email_check_rejects_invalid_input(self):
        with patch.object(tool_hub, "check_email_domain", side_effect=ValueError("invalid email")) as check:
            with self.assertRaises(HTTPError) as error:
                self.request("/x/email-check?email=not-an-email")
        self.assertEqual(error.exception.code, 400)
        check.assert_called_once_with("not-an-email")

    def test_cyber_lookup_returns_public_dns_and_rdap_data(self):
        expected = {"target": "example.com", "kind": "domain", "rdap": {"name": "EXAMPLE.COM"}, "dns": {"A": ["192.0.2.1"]}}
        with patch.object(tool_hub, "check_cyber_target", return_value=expected) as check:
            with self.request("/x/cyber-check?target=example.com") as response:
                result = json.loads(response.read())
        self.assertEqual(result, expected)
        check.assert_called_once_with("example.com")

    def test_cyber_osint_aggregates_passive_sources_without_scanning(self):
        with patch.object(tool_hub, "_rdap_lookup", return_value={"name": "EXAMPLE.COM"}), \
             patch.object(tool_hub, "dns_records", return_value=["203.0.113.10"]), \
             patch.object(tool_hub, "_certificate_transparency", return_value=["www.example.com"]):
            result = tool_hub.check_cyber_target("example.com")
        self.assertEqual(result["kind"], "domain")
        self.assertEqual(result["rdap"]["name"], "EXAMPLE.COM")
        self.assertIn("www.example.com", result["certificateNames"])
        self.assertIn("Certificate Transparency", result["sources"])
        self.assertIn("no port scan", result["scope"])

    def test_cyber_osint_rejects_private_ips_before_external_requests(self):
        with patch("tool_hub.urllib.request.urlopen") as request:
            with self.assertRaises(ValueError):
                tool_hub.check_cyber_target("192.168.1.10")
        request.assert_not_called()

    def test_avatar_design_and_preferences_remain_in_page(self):
        page = tool_hub.PAGE
        for marker in ("backdrop-filter", "@keyframes", "af-theme", "af-accent", "af-wallpaper", "af-glass-blur", "af-motion"):
            with self.subTest(marker=marker):
                self.assertIn(marker, page)
        self.assertIn("cycleGlass", page)
        self.assertIn("toggleMotion", page)

    def test_initial_picker_offers_only_two_finder_tools(self):
        page = tool_hub.PAGE
        self.assertIn("<h1>Roblox-Profil finden<br><span>ganz einfach.</span></h1>", page)
        self.assertIn('id="toolPickerGate" role="dialog" aria-modal="true"', page)
        self.assertIn("Welches Tool möchtest du benutzen?", page)
        self.assertIn('id="toolPickerCards"', page)
        self.assertIn('class="card fcard launch-card" data-launch-tool="${t.id}"', page)
        self.assertIn("$('#toolPickerCards').innerHTML=APP_TOOLS.map", page)
        self.assertIn("label:'Roblox Avatar Finder'", page)
        self.assertIn("label:'OSINT Toolkit'", page)
        tool_choices = page.split("const APP_TOOLS=[", 1)[1].split("];", 1)[0]
        self.assertEqual(tool_choices.count("{id:"), 2)
        self.assertGreaterEqual(len(tool_hub.USERNAME_SITES), 40)
        self.assertIn('id="usernameToolScreen"', page)
        self.assertIn('data-osint-path="/osint/email"', page)
        self.assertIn('data-osint-path="/osint/csint"', page)
        self.assertIn('id="usernameSearchForm"', page)
        self.assertIn('id="emailCheckForm"', page)
        self.assertIn('id="cyberCheckForm"', page)
        self.assertIn("history.pushState", page)
        self.assertIn("runUsernameSearch()", page)
        self.assertIn("$('#toolPickerGate').classList.add('dismissed')", page)
        self.assertIn("<div class=\"sec\"><span>Was du hier findest</span></div>", page)
        self.assertIn('class="hero avatar-home-head"', page)
        self.assertLess(page.index('id="feat"'), page.index('id="recentBox"'))
        self.assertIn("renderAll();showTab('start')", page)
        self.assertIn("#t-start #feat{grid-template-columns", page)
        self.assertIn("TABS.filter(t=>t.desc).map", page)
        self.assertIn("class=\"card fcard\" data-tab=\"${t.id}\"", page)
        self.assertIn("pendingTool=id", page)
        self.assertIn("const destination=pendingTool||'profil'", page)
        self.assertIn("Danach öffne ich „'+label+'“", page)
        for tool in ("Profil", "Favoriten", "Outfits", "Spiele", "Getragene Items", "Gruppen", "Technische Daten"):
            with self.subTest(tool=tool):
                self.assertIn(f"label:'{tool}'", page)

    def test_sections_are_stacked_and_sidebar_scrolls_without_wheel_paging(self):
        page = tool_hub.PAGE
        sections = ("start", "profil", "favorites", "items", "outfits", "games", "groups", "raw")
        positions = [page.index(f'id="t-{section}"') for section in sections]
        self.assertEqual(positions, sorted(positions))
        self.assertIn(".tab{display:block", page)
        self.assertNotIn(".tab{display:none}", page)
        self.assertIn("content.scrollTo({top:Math.max(0,top)", page)
        self.assertIn("contentArea.addEventListener('scroll'", page)
        self.assertNotIn("addEventListener('wheel'", page)
        self.assertNotIn("tab-wheel-next", page)

    def test_recent_search_api_saves_lists_and_clears_profiles(self):
        with self.request("/x/recent", method="POST", body={
            "userId": "12345", "username": "builderman", "displayName": "Builder Man",
            "avatarUrl": "https://tr.rbxcdn.com/example.png",
        }) as response:
            self.assertEqual(json.loads(response.read()), {"ok": True})
        with self.request("/x/recent") as response:
            rows = json.loads(response.read())
        self.assertEqual(rows[0]["userId"], "12345")
        self.assertEqual(rows[0]["username"], "builderman")
        with self.request("/x/recent", method="POST", body={"removeUserId": "12345"}) as response:
            self.assertEqual(json.loads(response.read()), {"ok": True})
        with self.request("/x/recent") as response:
            self.assertEqual(json.loads(response.read()), [])

    def test_username_lookup_calls_only_roblox_usernames_api(self):
        payload = {"data": [{"id": 156, "name": "builderman", "displayName": "Builderman"}]}
        with patch.object(tool_hub, "upstream", return_value=(200, json.dumps(payload).encode())) as upstream:
            with self.request("/x/username?name=builderman") as response:
                result = json.loads(response.read())
        self.assertEqual(result["data"][0]["id"], 156)
        args = upstream.call_args.args
        self.assertEqual(args[0], "https://users.roblox.com/v1/usernames/users")
        self.assertEqual(args[1]["usernames"], ["builderman"])

    def test_username_route_rejects_invalid_names_without_upstream_call(self):
        with patch.object(tool_hub, "upstream") as upstream:
            with self.assertRaises(HTTPError) as error:
                self.request("/x/username?name=bad%20name")
        self.assertEqual(error.exception.code, 400)
        upstream.assert_not_called()

    def test_provider_proxy_rejects_non_roblox_hosts(self):
        with self.assertRaises(HTTPError) as error:
            self.request("/api/https%3A%2F%2Fexample.org%2F")
        self.assertEqual(error.exception.code, 403)

    def test_outfit_route_rejects_non_numeric_ids(self):
        with self.assertRaises(HTTPError) as error:
            self.request("/x/outfit/not-an-id")
        self.assertEqual(error.exception.code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
