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

    def test_single_file_entrypoint_runs_and_defaults_to_avatar_finder(self):
        file = Path(__file__).with_name("tool_hub.py")
        result = subprocess.run([sys.executable, str(file), "--help"], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Roblox Avatar Finder", result.stdout)
        self.assertNotIn("public lookup tools", result.stdout)

    def test_root_and_legacy_avatar_route_serve_same_full_finder(self):
        paths = ("/", "/avatar", "/index.html", "/test.html")
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

    def test_health_reports_avatar_finder_only(self):
        with self.request("/health") as response:
            info = json.loads(response.read())
        self.assertEqual(info["app"], "FIELDKIT Roblox Avatar Finder")
        self.assertEqual(info["tools"], ["avatar-finder"])

    def test_avatar_design_and_preferences_remain_in_page(self):
        page = tool_hub.PAGE
        for marker in ("backdrop-filter", "@keyframes", "af-theme", "af-accent", "af-wallpaper", "af-glass-blur", "af-motion"):
            with self.subTest(marker=marker):
                self.assertIn(marker, page)
        self.assertIn("cycleGlass", page)
        self.assertIn("toggleMotion", page)

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
