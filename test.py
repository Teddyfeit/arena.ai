# Avatar Finder – starten mit:  python test.py
# Öffnet automatisch http://localhost:8000
import os, sys, json, re, time, sqlite3, threading, webbrowser, subprocess
import urllib.request, urllib.error, urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

PORT = 8000
MAX_PARALLEL = 10
TRIES = 4
DB_FILE = "cache.db"

os.chdir(os.path.dirname(os.path.abspath(__file__)))

ALLOWED_HOSTS = {
    "users.roblox.com", "avatar.roblox.com", "thumbnails.roblox.com",
    "catalog.roblox.com", "friends.roblox.com", "games.roblox.com",
    "groups.roblox.com", "presence.roblox.com",
}
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AvatarFinder/1.0"
SEM = threading.Semaphore(MAX_PARALLEL)

THUMB_ASSETS = "https://thumbnails.roblox.com/v1/assets?size=150x150&format=Png&isCircular=false&assetIds="
THUMB_OUTFITS = "https://thumbnails.roblox.com/v1/users/outfits?size=150x150&format=Png&isCircular=false&userOutfitIds="
CATALOG_URL = "https://catalog.roblox.com/v1/catalog/items/details"
# Outfit-Inhalte ändern sich selten: 7 Tage merken, damit jede Abfrage nur einmal an Roblox geht
OUTFIT_TTL = 7 * 24 * 3600

# ---------------------------------------------------------------- Datenbank
DB = sqlite3.connect(DB_FILE, check_same_thread=False)
dblock = threading.Lock()
with dblock:
    DB.execute("pragma journal_mode=WAL")
    DB.execute("create table if not exists cache(url text primary key, code integer, body blob, expires real)")
    DB.execute("create table if not exists recent(user_id text primary key, username text, display_name text, avatar_url text, ts real)")
    DB.execute("delete from cache where expires < ?", (time.time() - 86400,))
    DB.commit()


def db_get(key):
    with dblock:
        return DB.execute("select code, body, expires from cache where url=?", (key,)).fetchone()


def db_put(key, code, body, ttl):
    with dblock:
        DB.execute("insert or replace into cache values (?,?,?,?)", (key, code, body, time.time() + ttl))
        DB.commit()


def db_del(*keys):
    with dblock:
        for k in keys:
            DB.execute("delete from cache where url=?", (k,))
        DB.commit()


def recent_list():
    with dblock:
        rows = DB.execute("select user_id, username, display_name, avatar_url from recent order by ts desc limit 10").fetchall()
    return [{"userId": r[0], "username": r[1], "displayName": r[2], "avatarUrl": r[3]} for r in rows]


def recent_save(d):
    uid = str(d.get("userId", ""))
    if not uid.isdigit():
        return
    with dblock:
        DB.execute("insert or replace into recent values (?,?,?,?,?)",
                   (uid, str(d.get("username", ""))[:40], str(d.get("displayName", ""))[:60],
                    str(d.get("avatarUrl", ""))[:300], time.time()))
        DB.execute("delete from recent where user_id not in (select user_id from recent order by ts desc limit 30)")
        DB.commit()


def recent_clear():
    with dblock:
        DB.execute("delete from recent")
        DB.commit()


# ---------------------------------------------------------------- Roblox holen
def upstream(url, payload=None, retry429=True):
    csrf = None
    last = (502, b'{"errors":[{"message":"Roblox nicht erreichbar"}]}')
    for i in range(TRIES):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
        req.add_header("User-Agent", UA)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if csrf:
            req.add_header("x-csrf-token", csrf)
        try:
            with SEM:
                with urllib.request.urlopen(req, timeout=15) as r:
                    return r.status, r.read()
        except urllib.error.HTTPError as e:
            body = e.read()
            code = e.code
            tok = e.headers.get("x-csrf-token")
            if code == 403 and tok and not csrf:
                csrf = tok
                continue
            if code == 429 and not retry429:
                return code, body
            if code == 429 or code >= 500:
                ra = e.headers.get("Retry-After")
                try:
                    wait = min(float(ra), 3) if ra else 1.0 * 2 ** i
                except ValueError:
                    wait = 1.0 * 2 ** i
                last = (code, body)
                time.sleep(wait)
                continue
            return code, body
        except Exception:
            time.sleep(1.0 * 2 ** i)
    return last


locks = {}
locks_guard = threading.Lock()


def cached(key, call, ttl):
    r = db_get(key)
    if r and r[2] > time.time():
        return r[0], r[1], "HIT"
    with locks_guard:
        lk = locks.setdefault(key, threading.Lock())
    with lk:
        r = db_get(key)
        if r and r[2] > time.time():
            return r[0], r[1], "HIT"
        code, body = call()
        if code == 200:
            if b'"Pending"' not in body and b'"Error"' not in body:
                db_put(key, code, body, ttl)
        elif code in (400, 401, 403, 404):
            db_put(key, code, body, 60)
        elif (code == 429 or code >= 500) and r and r[0] == 200:
            return r[0], r[1], "STALE"
        return code, body, "MISS"


def ttl_for(url):
    if "/outfits" in url:
        return 120
    if "presence" in url:
        return 30
    return 600


def allowed(url):
    try:
        p = urllib.parse.urlparse(url)
    except Exception:
        return False
    return p.scheme == "https" and p.hostname in ALLOWED_HOSTS


def log(tag, code, url):
    try:
        p = urllib.parse.urlparse(url)
        print(("[%s] %s %s%s" % (tag, code, p.hostname or "", p.path))[:100])
    except Exception:
        pass


# ---------------------------------------------------------------- Bilder
def thumbs(base, ids, tries=2, wait=0.35):
    """Bilder holen, 'Pending' nachladen. Rückgabe: {id(str): url}"""
    out = {}
    todo = list(dict.fromkeys(str(i) for i in ids))
    for att in range(tries):
        if not todo:
            break
        if att:
            time.sleep(wait)
        rest = []
        for i in range(0, len(todo), 50):
            part = todo[i:i + 50]
            url = base + ",".join(part)
            code, body, _ = cached(url, lambda u=url: upstream(u), 600)
            if code != 200:
                continue
            try:
                rows = json.loads(body).get("data") or []
            except Exception:
                continue
            for x in rows:
                if not isinstance(x, dict):
                    continue
                tid = str(x.get("targetId"))
                if x.get("state") == "Completed" and x.get("imageUrl"):
                    out[tid] = x["imageUrl"]
                elif x.get("state") in ("Pending", "InReview"):
                    rest.append(tid)
        todo = rest
    return out


# ---------------------------------------------------------------- Outfit-Details (schnell: ohne Warten auf Bilder)
def outfit_payload(oid, fresh=False):
    """Liefert sofort die Item-Liste. Bilder und Preise laden Browser bzw. /x/catalog nach."""
    key = "OUTFIT %d" % oid
    durl = "https://avatar.roblox.com/v1/outfits/%d/details" % oid
    if fresh:
        db_del(key, durl)
    r = db_get(key)
    if r and r[2] > time.time():
        return r[0], r[1], "HIT"
    with locks_guard:
        lk = locks.setdefault(key, threading.Lock())
    with lk:
        r = db_get(key)
        if r and r[2] > time.time():
            return r[0], r[1], "HIT"
        code, body, tag = cached(durl, lambda: upstream(durl, retry429=False), OUTFIT_TTL)
        if code != 200:
            return code, body, tag
        try:
            d = json.loads(body)
            if not isinstance(d, dict):
                raise ValueError("kein Objekt")
        except Exception:
            return 502, b'{"error":"Roblox hat keine Item-Details geliefert."}', "MISS"
        items = []
        for a in d.get("assets") or []:
            if not isinstance(a, dict) or not isinstance(a.get("id"), int):
                continue
            items.append({
                "id": a["id"],
                "name": a.get("name") or "Item",
                "type": ((a.get("assetType") or {}).get("name")) or "Item",
                "thumbnail": None,
                "creatorName": None,
                "price": None,
                "priceStatus": None,
            })
        payload = {
            "savedAvatarId": oid,
            "name": d.get("name"),
            "itemCount": len(items),
            "playerAvatarType": d.get("playerAvatarType"),
            "scale": d.get("scale") or d.get("scales"),
            "items": items,
        }
        out = json.dumps(payload).encode("utf-8")
        db_put(key, 200, out, OUTFIT_TTL)
        return 200, out, "MISS"


# ---------------------------------------------------------------- Outfit-Liste (Liste + Bilder)
def warm(ids):
    for i in ids[:4]:
        try:
            outfit_payload(int(i))
        except Exception:
            pass


def outfits_payload(uid, fresh=False):
    key = "OUTFITS %d" % uid
    urls = [
        "https://avatar.roblox.com/v1/users/%d/outfits?itemsPerPage=50&page=1" % uid,
        "https://avatar.roblox.com/v2/avatar/users/%d/outfits?page=1&itemsPerPage=50&isEditable=true" % uid,
    ]
    if fresh:
        db_del(key, *urls)
    r = db_get(key)
    if r and r[2] > time.time():
        return r[0], r[1], "HIT"
    with locks_guard:
        lk = locks.setdefault(key, threading.Lock())
    with lk:
        r = db_get(key)
        if r and r[2] > time.time():
            return r[0], r[1], "HIT"
        rows = None
        last = (502, b'{"error":"Roblox nicht erreichbar"}')
        for u in urls:
            code, body, _ = cached(u, lambda u=u: upstream(u), 120)
            if code == 200:
                try:
                    d = json.loads(body).get("data") or []
                except Exception:
                    d = []
                d = [x for x in d if isinstance(x, dict) and isinstance(x.get("id"), int)]
                if d:
                    rows = d
                    break
                if rows is None:
                    rows = []
            else:
                last = (code, body)
        if rows is None:
            return last[0], last[1], "MISS"
        ids = [x["id"] for x in rows]
        try:
            th = thumbs(THUMB_OUTFITS, ids) if ids else {}
        except Exception:
            th = {}
        data = [{"id": x["id"], "name": x.get("name") or "Outfit", "thumbnail": th.get(str(x["id"]))} for x in rows]
        out = json.dumps({"data": data, "count": len(data)}).encode("utf-8")
        missing = any(x["thumbnail"] is None for x in data)
        db_put(key, 200, out, 15 if missing else 90)
        return 200, out, "MISS"


# ---------------------------------------------------------------- Server
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def out(self, code, body, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if code != 204:
            self.wfile.write(body)

    def jout(self, obj, code=200):
        self.out(code, json.dumps(obj).encode("utf-8"))

    def do_GET(self):
        p = self.path
        try:
            if p in ("/", "/index.html", "/test.html") or p.startswith("/?"):
                return self.out(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            if p == "/favicon.ico":
                return self.out(204, b"")
            if p.startswith("/api/"):
                url = urllib.parse.unquote(p[5:])
                if not allowed(url):
                    return self.jout({"error": "host nicht erlaubt"}, 403)
                code, body, tag = cached(url, lambda: upstream(url), ttl_for(url))
                log(tag, code, url)
                return self.out(code, body)
            u = urllib.parse.urlparse(p)
            q = urllib.parse.parse_qs(u.query)
            fresh = bool(q.get("fresh"))
            if u.path == "/x/recent":
                return self.jout(recent_list())
            if u.path.startswith("/x/outfits/"):
                uid = u.path.rsplit("/", 1)[1]
                if not uid.isdigit() or int(uid) <= 0:
                    return self.jout({"error": "id"}, 400)
                code, body, tag = outfits_payload(int(uid), fresh)
                log(tag, code, "https://lokal/x/outfits/%s" % uid)
                return self.out(code, body)
            if u.path.startswith("/x/outfit/"):
                oid = u.path.rsplit("/", 1)[1]
                if not oid.isdigit() or int(oid) <= 0:
                    return self.jout({"error": "id"}, 400)
                code, body, tag = outfit_payload(int(oid), fresh)
                log(tag, code, "https://lokal/x/outfit/%s" % oid)
                return self.out(code, body)
            if u.path.startswith("/x/presence/"):
                uid = u.path.rsplit("/", 1)[1]
                if not uid.isdigit():
                    return self.jout({"error": "id"}, 400)
                url = "https://presence.roblox.com/v1/presence/users"
                code, body, tag = cached("POST %s %s" % (url, uid),
                                         lambda: upstream(url, {"userIds": [int(uid)]}), 30)
                log(tag, code, url)
                return self.out(code, body)
            if u.path == "/x/username":
                name = (q.get("name") or [""])[0]
                if not re.fullmatch(r"[A-Za-z0-9_]{1,20}", name):
                    return self.jout({"error": "name"}, 400)
                url = "https://users.roblox.com/v1/usernames/users"
                code, body, tag = cached("POST %s %s" % (url, name.lower()),
                                         lambda: upstream(url, {"usernames": [name], "excludeBannedUsers": False}), 600)
                log(tag, code, url)
                return self.out(code, body)
            if u.path == "/x/catalog":
                ids = [i for i in (q.get("ids") or [""])[0].split(",") if i.isdigit()]
                ids = list(dict.fromkeys(ids))[:50]
                if not ids:
                    return self.jout({"data": []})
                payload = {"items": [{"itemType": "Asset", "id": int(i)} for i in ids]}
                code, body, tag = cached("POST %s %s" % (CATALOG_URL, ",".join(sorted(ids))),
                                         lambda: upstream(CATALOG_URL, payload), 600)
                log(tag, code, CATALOG_URL)
                return self.out(code, body)
            self.jout({"error": "nicht gefunden"}, 404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:
            print("Fehler:", e)
            try:
                self.jout({"error": "serverfehler"}, 500)
            except Exception:
                pass

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            if self.path == "/x/recent":
                try:
                    d = json.loads(raw.decode("utf-8") or "{}")
                except Exception:
                    d = {}
                if d.get("clear"):
                    recent_clear()
                else:
                    recent_save(d)
                return self.jout({"ok": True})
            self.jout({"error": "nicht gefunden"}, 404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:
            print("Fehler:", e)


class Srv(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ---------------------------------------------------------------- Die Seite
PAGE = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Avatar Finder</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
html{--acc:#0a84ff;--txt:#1d1d1f;--sub:rgba(60,60,67,.62);--glass:rgba(255,255,255,.58);--glass2:rgba(255,255,255,.45);--solid:rgba(255,255,255,.62);--rim:rgba(255,255,255,.85);--line:rgba(0,0,0,.09);--bg:#dfe5f0;--sh:0 30px 80px rgba(20,30,60,.22);--base:54px;color-scheme:light}
html.dark{--txt:#f5f5f7;--sub:rgba(235,235,245,.6);--glass:rgba(34,35,42,.58);--glass2:rgba(70,72,84,.34);--solid:rgba(255,255,255,.08);--rim:rgba(255,255,255,.16);--line:rgba(255,255,255,.1);--bg:#0b0c10;--sh:0 30px 80px rgba(0,0,0,.6);color-scheme:dark}
body{height:100vh;overflow:hidden;background:var(--bg);color:var(--txt);font:14px/1.45 Inter,-apple-system,"SF Pro Text","Segoe UI",system-ui,sans-serif;-webkit-font-smoothing:antialiased;transition:background .4s}
body::after{content:"";position:fixed;inset:0;pointer-events:none;z-index:200;opacity:.05;mix-blend-mode:overlay;background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='160' height='160'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='.9' numOctaves='2'/></filter><rect width='100%' height='100%' filter='url(%23n)'/></svg>")}
*::-webkit-scrollbar{width:8px;height:8px}
*::-webkit-scrollbar-thumb{background:rgba(128,128,128,.35);border-radius:8px;border:2px solid transparent;background-clip:padding-box}
*::-webkit-scrollbar-track{background:transparent}
button{font:inherit;color:inherit}

.wall{position:fixed;inset:0;overflow:hidden;z-index:0}
.wall i{position:absolute;border-radius:50%;filter:blur(90px);opacity:.6;animation:drift 24s ease-in-out infinite alternate}
.wall .b1{width:46vw;height:46vw;left:-8vw;top:-12vw;background:#6aa8ff}
.wall .b2{width:40vw;height:40vw;right:-10vw;top:10vh;background:#ff9ec4;animation-delay:-8s}
.wall .b3{width:44vw;height:44vw;left:25vw;bottom:-22vw;background:#8fe3c0;animation-delay:-15s}
html.dark .wall i{opacity:.3}

.menubar{position:fixed;top:0;left:0;right:0;height:30px;z-index:50;display:flex;align-items:center;gap:2px;padding:0 10px;background:var(--glass);backdrop-filter:blur(30px) saturate(180%);-webkit-backdrop-filter:blur(30px) saturate(180%);border-bottom:1px solid var(--line);font-size:13px;font-weight:500}
.mb{padding:2px 9px;border-radius:6px;cursor:default;user-select:none;white-space:nowrap;display:flex;align-items:center}
.mb:hover,.mb.on{background:rgba(128,128,128,.2)}
.mb.b{font-weight:700}
.mbr{margin-left:auto;display:flex;align-items:center;gap:10px;font-size:12.5px}
.menubar .iconbtn{width:24px;height:24px}
.menu{position:fixed;top:32px;min-width:240px;padding:5px;border-radius:12px;background:var(--glass);backdrop-filter:blur(40px) saturate(180%);-webkit-backdrop-filter:blur(40px) saturate(180%);border:1px solid var(--line);box-shadow:var(--sh),inset 0 1px 0 var(--rim);z-index:60;display:none}
.menu.show{display:block;animation:pop .14s ease-out}
.mi{padding:5px 10px;border-radius:6px;font-size:13px;cursor:default;display:flex;justify-content:space-between;gap:24px}
.mi .sc{color:var(--sub)}
.mi:hover{background:var(--acc);color:#fff}
.mi:hover .sc{color:rgba(255,255,255,.8)}
.msep{height:1px;background:var(--line);margin:5px 8px}

.win{position:fixed;z-index:10;left:50%;top:46px;width:min(1180px,calc(100vw - 24px));height:calc(100vh - 156px);min-height:440px;border-radius:26px;display:flex;overflow:hidden;background:var(--glass);backdrop-filter:blur(44px) saturate(190%);-webkit-backdrop-filter:blur(44px) saturate(190%);border:1px solid var(--line);box-shadow:var(--sh),inset 0 1px 0 var(--rim);animation:winIn .6s cubic-bezier(.2,.9,.3,1.1) backwards;transition:opacity .3s,transform .35s cubic-bezier(.3,.8,.3,1),left .35s,top .35s,width .35s,height .35s}
.win::before{content:"";position:absolute;inset:0;border-radius:inherit;padding:1px;pointer-events:none;z-index:5;opacity:.75;background:linear-gradient(160deg,rgba(255,255,255,.95),rgba(255,255,255,0) 30%,rgba(255,255,255,0) 70%,rgba(255,255,255,.55));-webkit-mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);-webkit-mask-composite:xor;mask-composite:exclude}
html.dark .win::before{opacity:.28}
.win.drag{transition:none!important}
.win.hide{opacity:0;transform:translateY(70px) scale(.85);pointer-events:none}
.win.zoom{left:12px!important;top:38px!important;width:calc(100vw - 24px)!important;height:calc(100vh - 130px)!important}

.side{width:228px;flex:none;display:flex;flex-direction:column;padding:16px 12px 12px;background:var(--glass2);border-right:1px solid var(--line);transition:width .35s cubic-bezier(.3,.8,.3,1),padding .35s,opacity .25s;overflow:hidden;white-space:nowrap}
.win.collapsed .side{width:0;padding-left:0;padding-right:0;opacity:0;border-right-width:0}
.lights{display:flex;gap:8px;padding:2px 6px 16px}
.lights b{width:13px;height:13px;border-radius:50%;cursor:pointer;display:block;border:1px solid rgba(0,0,0,.12)}
.l1{background:#ff5f57}.l2{background:#febc2e}.l3{background:#28c840}
.nav{position:relative;display:flex;flex-direction:column;gap:2px}
#sel{position:absolute;left:0;right:0;top:0;height:36px;border-radius:10px;background:color-mix(in srgb,var(--acc) 20%,transparent);box-shadow:inset 0 1px 0 var(--rim);transition:transform .4s cubic-bezier(.3,1.2,.4,1),height .3s}
.ni{position:relative;z-index:1;display:flex;align-items:center;gap:10px;height:36px;padding:0 10px;border-radius:10px;cursor:pointer;font-weight:500;user-select:none;transition:background .2s}
.ni:hover:not(.act){background:var(--solid)}
.ni.act{font-weight:650}
.ni .ico{width:24px;height:24px;border-radius:7px;display:grid;place-items:center;color:#fff;flex:none}
.ni .bd{margin-left:auto;font-size:11px;font-weight:600;color:var(--sub);background:var(--solid);padding:1px 7px;border-radius:99px;display:none}
.sidefoot{margin-top:auto;padding:10px 6px 0;display:flex;align-items:center;gap:8px}
.sw{width:18px;height:18px;border-radius:50%;cursor:pointer;border:2px solid transparent;box-shadow:0 0 0 1px var(--line);transition:transform .2s}
.sw:hover{transform:scale(1.15)}
.sw.on{border-color:var(--bg);box-shadow:0 0 0 2px var(--acc)}

.main{flex:1;min-width:0;display:flex;flex-direction:column;position:relative}
.tb{height:58px;flex:none;display:flex;align-items:center;gap:12px;padding:0 18px;border-bottom:1px solid var(--line);user-select:none}
.tb h2{font-size:16px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.iconbtn{width:32px;height:32px;border-radius:50%;display:grid;place-items:center;cursor:pointer;color:var(--sub);background:transparent;border:0;transition:background .2s,color .2s;flex:none}
.iconbtn:hover{background:var(--solid);color:var(--txt)}
.pill{margin-left:auto;display:flex;align-items:center;gap:8px;height:38px;padding:0 5px 0 13px;width:min(340px,50%);border-radius:99px;background:var(--solid);border:1px solid var(--line);box-shadow:inset 0 1px 0 var(--rim);color:var(--sub);transition:box-shadow .25s,border-color .25s}
.pill:focus-within{border-color:var(--acc);box-shadow:0 0 0 3px color-mix(in srgb,var(--acc) 30%,transparent),inset 0 1px 0 var(--rim)}
.pill input{flex:1;min-width:0;border:0;outline:0;background:transparent;color:var(--txt);font:inherit}
.go{height:28px;padding:0 14px;border-radius:99px;border:0;background:var(--acc);color:#fff;font-weight:600;cursor:pointer;transition:transform .2s}
.go:active{transform:scale(.95)}
.spin{display:none;width:15px;height:15px;border:2px solid var(--line);border-top-color:var(--acc);border-radius:50%;animation:rot .7s linear infinite;flex:none}
.pill.busy .spin{display:block}

.content{flex:1;overflow:auto;padding:22px 24px 30px;position:relative}
.tab{display:none}
.tab.show{display:block;animation:fade .35s}

.hero{text-align:center;padding:26px 10px 8px}
.logo{width:84px;height:84px;margin:0 auto 14px;border-radius:24px;background:var(--acc);display:grid;place-items:center;color:#fff;box-shadow:0 14px 34px color-mix(in srgb,var(--acc) 45%,transparent),inset 0 1px 0 rgba(255,255,255,.5)}
.hero h1{font-size:34px;font-weight:800;letter-spacing:-.03em}
.hero p{color:var(--sub);margin-top:6px}
.quick{display:flex;gap:8px;justify-content:center;flex-wrap:wrap;margin-top:18px}
.qb{padding:7px 16px;border-radius:99px;background:var(--solid);border:1px solid var(--line);box-shadow:inset 0 1px 0 var(--rim);cursor:pointer;font-weight:600;transition:transform .2s,background .2s,color .2s}
.qb:hover{background:var(--acc);color:#fff;transform:translateY(-2px)}
.sec{margin:26px 2px 10px;font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--sub);display:flex;justify-content:space-between;align-items:center}
.sec2{margin:18px 0 8px;font-size:12px;font-weight:700;color:var(--sub);text-transform:uppercase;letter-spacing:.06em}
.lnkbtn{background:none;border:0;color:var(--acc);font-weight:600;cursor:pointer;text-transform:none;letter-spacing:0;font-size:13px}
.rec{display:flex;gap:10px;flex-wrap:wrap}
.rchip{display:flex;align-items:center;gap:9px;padding:5px 14px 5px 5px;border-radius:99px;background:var(--solid);border:1px solid var(--line);box-shadow:inset 0 1px 0 var(--rim);cursor:pointer;transition:transform .2s;text-align:left}
.rchip:hover{transform:translateY(-2px)}
.rchip img,.ph2{width:32px;height:32px;border-radius:50%;object-fit:cover;background:var(--line);flex:none}
.rchip i{color:var(--sub);font-style:normal;font-size:12px}

.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:14px}
.grid.w{grid-template-columns:repeat(auto-fill,minmax(270px,1fr))}
.card{position:relative;overflow:hidden;border-radius:20px;background:var(--glass2);border:1px solid var(--line);box-shadow:inset 0 1px 0 var(--rim),0 8px 24px rgba(20,30,60,.07);transition:transform .3s cubic-bezier(.3,.8,.3,1),box-shadow .3s;animation:rise .5s cubic-bezier(.2,.8,.3,1) backwards;animation-delay:calc(var(--i,0)*35ms)}
.card:hover{transform:translateY(-4px);box-shadow:inset 0 1px 0 var(--rim),0 18px 38px rgba(20,30,60,.16)}
.card.prof:hover{transform:none}
.card.clk{cursor:pointer}
.card::after{content:"";position:absolute;inset:0;border-radius:inherit;pointer-events:none;opacity:0;transition:opacity .3s;background:radial-gradient(240px circle at var(--mx,50%) var(--my,50%),rgba(255,255,255,.28),transparent 60%)}
.card:hover::after{opacity:1}
html.dark .card::after{background:radial-gradient(240px circle at var(--mx,50%) var(--my,50%),rgba(255,255,255,.09),transparent 60%)}
.fcard{padding:18px;cursor:pointer}
.fcard .ico{width:40px;height:40px;border-radius:12px;display:grid;place-items:center;color:#fff;margin-bottom:12px}
.fcard b{display:block;font-size:15px}
.fcard p{color:var(--sub);font-size:13px;margin-top:4px}
.th{aspect-ratio:1;background:var(--solid);overflow:hidden;display:grid;place-items:center}
.th img{width:100%;height:100%;object-fit:cover;transition:transform .5s cubic-bezier(.3,.8,.3,1)}
.card:hover .th img{transform:scale(1.08)}
.cb{padding:12px 14px 14px}
.nm{font-weight:650;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ty{color:var(--sub);font-size:12px}
.meta{font-size:12px;color:var(--sub);min-height:17px;margin-top:3px}
.row{display:flex;align-items:center;gap:8px;margin-top:8px}
.lnk{margin-left:auto;color:var(--sub);display:grid;place-items:center;width:26px;height:26px;border-radius:50%;transition:background .2s,color .2s}
.lnk:hover{background:var(--acc);color:#fff}
.chip{display:inline-flex;align-items:center;gap:5px;padding:2px 9px;border-radius:99px;font:500 11.5px "JetBrains Mono",ui-monospace,monospace;background:var(--solid);border:1px solid var(--line);color:var(--sub);cursor:pointer;transition:background .2s,color .2s}
.chip:hover{background:var(--acc);color:#fff;border-color:transparent}
.rowcard{display:flex;gap:14px;padding:14px;align-items:center}
.gi{width:68px;height:68px;border-radius:16px;overflow:hidden;background:var(--solid);flex:none}
.gi img{width:100%;height:100%;object-fit:cover}
.gb{min-width:0;flex:1}
.ph{width:100%;height:100%;min-height:50px;background:var(--line);animation:pulse 1.3s ease-in-out infinite}

.prof{display:flex;gap:26px;padding:24px;flex-wrap:wrap}
.avbox{width:300px;max-width:100%;aspect-ratio:1;border-radius:22px;background:var(--solid);overflow:hidden;flex:none;display:grid;place-items:center;border:1px solid var(--line)}
.avbox img{width:100%;height:100%;object-fit:cover;animation:fade .5s}
.pinfo{flex:1;min-width:260px}
.pinfo h1{font-size:30px;font-weight:800;letter-spacing:-.025em;display:flex;align-items:center;gap:8px;line-height:1.15}
.vb{width:22px;height:22px;border-radius:50%;background:var(--acc);color:#fff;font-size:13px;display:inline-grid;place-items:center}
.sub{color:var(--sub);margin-top:4px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.mt{margin-top:12px}
.desc{margin-top:12px;color:var(--sub);white-space:pre-wrap;max-height:120px;overflow:auto}
.pills{display:flex;flex-wrap:wrap;gap:6px}
.tag{padding:3px 10px;border-radius:99px;background:var(--solid);border:1px solid var(--line);font-size:12px;font-weight:500}
.tag.red{background:#ff453a;color:#fff;border-color:transparent}
.pres{display:inline-flex;align-items:center;gap:7px;padding:3px 11px;border-radius:99px;font-size:12.5px;font-weight:600;background:var(--solid);border:1px solid var(--line)}
.dot{width:8px;height:8px;border-radius:50%;background:#8e8e93;display:block}
.pres.on .dot{background:#0a84ff}.pres.game .dot{background:#30d158}.pres.studio .dot{background:#ff9f0a}
.stats{display:grid;grid-template-columns:repeat(auto-fill,minmax(112px,1fr));gap:10px;margin-top:18px}
.stat{padding:12px 14px;border-radius:16px;background:var(--solid);border:1px solid var(--line)}
.stat b{display:block;font-size:22px;font-weight:800;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.stat span{font-size:12px;color:var(--sub)}
.acts{display:flex;gap:8px;flex-wrap:wrap;margin-top:20px}
.btn{display:inline-flex;align-items:center;gap:6px;height:36px;padding:0 16px;border-radius:99px;background:var(--solid);border:1px solid var(--line);color:var(--txt);font-weight:600;cursor:pointer;text-decoration:none;box-shadow:inset 0 1px 0 var(--rim);transition:transform .2s}
.btn:hover{transform:translateY(-1px)}
.btn:active{transform:scale(.97)}
.btn.pri{background:var(--acc);color:#fff;border-color:transparent}

.state{display:flex;flex-direction:column;align-items:center;gap:12px;padding:70px 20px;color:var(--sub);text-align:center}
.spinner{width:30px;height:30px;border-radius:50%;border:3px solid var(--line);border-top-color:var(--acc);animation:rot .8s linear infinite}
pre.raw{padding:18px;border-radius:18px;background:var(--solid);border:1px solid var(--line);font:12px/1.55 "JetBrains Mono",ui-monospace,monospace;white-space:pre-wrap;word-break:break-all}

#sheetBg{position:fixed;inset:0;z-index:80;display:none;place-items:center;background:rgba(0,0,0,.28);backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px)}
#sheetBg.show{display:grid;animation:fade .2s}
.sheet{width:min(780px,calc(100vw - 24px));max-height:min(84vh,780px);display:flex;flex-direction:column;border-radius:28px;overflow:hidden;background:var(--glass);backdrop-filter:blur(44px) saturate(190%);-webkit-backdrop-filter:blur(44px) saturate(190%);border:1px solid var(--line);box-shadow:var(--sh),inset 0 1px 0 var(--rim);animation:sheetIn .38s cubic-bezier(.2,.9,.3,1.1)}
.sh-head{display:flex;align-items:center;gap:16px;padding:18px 18px 16px 20px;border-bottom:1px solid var(--line);flex:none}
.sh-img{width:84px;height:84px;border-radius:18px;overflow:hidden;background:var(--solid);border:1px solid var(--line);flex:none}
.sh-img img{width:100%;height:100%;object-fit:cover}
.sh-t{flex:1;min-width:0}
.sh-t h3{font-size:21px;font-weight:800;letter-spacing:-.02em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sh-body{overflow:auto;padding:18px 20px 22px}
.sheet .grid{grid-template-columns:repeat(auto-fill,minmax(142px,1fr));gap:12px}
.sheet .state{padding:44px 20px}

.dock{position:fixed;left:50%;bottom:12px;transform:translateX(-50%);z-index:40;display:flex;align-items:flex-end;gap:8px;padding:8px 10px;border-radius:28px;background:var(--glass);backdrop-filter:blur(40px) saturate(190%);-webkit-backdrop-filter:blur(40px) saturate(190%);border:1px solid var(--line);box-shadow:var(--sh),inset 0 1px 0 var(--rim);max-width:calc(100vw - 8px)}
.di{position:relative;width:var(--s,var(--base));height:var(--s,var(--base));border-radius:24%;display:grid;place-items:center;color:#fff;cursor:pointer;flex:none;box-shadow:0 6px 14px rgba(0,0,0,.25),inset 0 1px 0 rgba(255,255,255,.45);transition:width .12s ease-out,height .12s ease-out}
.di::after{content:"";position:absolute;inset:0;border-radius:inherit;background:linear-gradient(rgba(255,255,255,.3),rgba(255,255,255,0) 55%);pointer-events:none}
.di svg{width:52%;height:52%}
.di .tip{position:absolute;bottom:calc(100% + 14px);left:50%;transform:translateX(-50%) scale(.9);padding:4px 11px;border-radius:9px;font-size:12px;font-weight:600;white-space:nowrap;color:var(--txt);background:var(--glass);backdrop-filter:blur(20px);border:1px solid var(--line);opacity:0;pointer-events:none;transition:opacity .15s,transform .15s}
.di:hover .tip{opacity:1;transform:translateX(-50%) scale(1)}
.di.act::before{content:"";position:absolute;bottom:-7px;left:50%;width:4px;height:4px;margin-left:-2px;border-radius:50%;background:var(--txt);opacity:.7}
.dsep{width:1px;height:42px;background:var(--line);align-self:center;margin:0 2px}

#toasts{position:fixed;top:42px;right:14px;z-index:90;display:flex;flex-direction:column;gap:8px;pointer-events:none}
.toast{pointer-events:auto;min-width:240px;max-width:320px;padding:11px 14px;border-radius:16px;background:var(--glass);backdrop-filter:blur(30px) saturate(180%);-webkit-backdrop-filter:blur(30px) saturate(180%);border:1px solid var(--line);box-shadow:var(--sh),inset 0 1px 0 var(--rim);animation:toastIn .4s cubic-bezier(.2,.9,.3,1.2);transition:opacity .3s,transform .3s}
.toast b{display:block;font-size:13px}
.toast span{font-size:12.5px;color:var(--sub);word-break:break-all}
.toast.out{opacity:0;transform:translateX(30px)}
#alertBg{position:fixed;inset:0;z-index:100;display:none;place-items:center;background:rgba(0,0,0,.25);backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px)}
#alertBg.show{display:grid}
.alert{width:min(350px,calc(100vw - 40px));padding:24px 22px 18px;border-radius:26px;text-align:center;background:var(--glass);backdrop-filter:blur(44px) saturate(190%);-webkit-backdrop-filter:blur(44px) saturate(190%);border:1px solid var(--line);box-shadow:var(--sh),inset 0 1px 0 var(--rim);animation:pop .25s cubic-bezier(.2,.9,.3,1.3)}
.alert h3{font-size:17px}
.alert p{color:var(--sub);margin-top:8px;white-space:pre-line;font-size:13.5px}
.alert .bts{display:flex;gap:8px;margin-top:18px}
.alert .bts button{flex:1;height:36px;border-radius:99px;border:0;font-weight:600;cursor:pointer;background:var(--solid);color:var(--txt)}
.alert .bts .pri{background:var(--acc);color:#fff}

@keyframes winIn{from{opacity:0;transform:scale(.95) translateY(14px)}}
@keyframes sheetIn{from{opacity:0;transform:translateY(26px) scale(.96)}}
@keyframes pop{from{opacity:0;transform:scale(.94)}}
@keyframes fade{from{opacity:0}}
@keyframes rise{from{opacity:0;transform:translateY(14px) scale(.98)}}
@keyframes rot{to{transform:rotate(360deg)}}
@keyframes pulse{50%{opacity:.45}}
@keyframes toastIn{from{opacity:0;transform:translateX(40px) scale(.95)}}
@keyframes drift{to{transform:translate(4vw,3vh) scale(1.08)}}

@media(max-width:760px){
 html{--base:36px}
 .side{position:absolute;z-index:6;left:0;top:0;bottom:0;background:var(--glass)}
 .pill{width:auto;flex:1}
 .tb h2{display:none}
 .win{height:calc(100vh - 120px)}
 .prof{padding:16px}
 .dock{gap:5px;padding:6px 7px;border-radius:20px}
 .content{padding:16px}
 .sh-img{width:60px;height:60px}
}
</style>
</head>
<body>
<div class="wall"><i class="b1"></i><i class="b2"></i><i class="b3"></i></div>
<div class="menubar" id="mbar"></div>
<div class="menu" id="menu"></div>

<div class="win" id="win">
 <aside class="side">
  <div class="lights"><b class="l1" id="l1"></b><b class="l2" id="l2"></b><b class="l3" id="l3"></b></div>
  <div class="nav" id="nav"></div>
  <div class="sidefoot" id="swatches"></div>
 </aside>
 <div class="main">
  <div class="tb" id="tb">
   <button class="iconbtn" id="sbBtn" title="Seitenleiste ein/aus"></button>
   <h2 id="ttl">Start</h2>
   <div class="pill" id="pill"><span class="spin"></span><input id="q" placeholder="Username oder ID suchen …" autocomplete="off" spellcheck="false"><button class="go" id="go">Suchen</button></div>
  </div>
  <div class="content" id="content">
   <section class="tab show" id="t-start">
    <div class="hero">
     <div class="logo" id="logo"></div>
     <h1>Avatar Finder</h1>
     <p>Roblox-Profile, Items, Outfits, Spiele und Gruppen – schnell und ohne Anmeldung.</p>
     <div class="quick"><button class="qb" data-q="Roblox">Roblox</button><button class="qb" data-q="builderman">builderman</button><button class="qb" data-q="1">ID 1</button></div>
    </div>
    <div id="recentBox" style="display:none">
     <div class="sec"><span>Zuletzt gesucht</span><button class="lnkbtn" id="recClear">Löschen</button></div>
     <div class="rec" id="recentList"></div>
    </div>
    <div class="sec"><span>Was du hier findest</span></div>
    <div class="grid w" id="feat"></div>
   </section>
   <section class="tab" id="t-profil"></section>
   <section class="tab" id="t-items"></section>
   <section class="tab" id="t-outfits"></section>
   <section class="tab" id="t-games"></section>
   <section class="tab" id="t-groups"></section>
   <section class="tab" id="t-raw"></section>
  </div>
 </div>
</div>

<div class="dock" id="dock"></div>
<div id="sheetBg"><div class="sheet" id="sheet"></div></div>
<div id="toasts"></div>
<div id="alertBg"><div class="alert" id="alertBox"></div></div>

<script>
const $=s=>document.querySelector(s),$$=s=>[...document.querySelectorAll(s)];
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

const ICONS={
 home:'M3 11l9-8 9 8M5 10v10h14V10',
 user:'M12 12a4 4 0 100-8 4 4 0 000 8zM4 21c0-4 4-6 8-6s8 2 8 6',
 shirt:'M8 3L3 6l2 4 3-1v12h8V9l3 1 2-4-5-3c-1 2-2 3-4 3S9 5 8 3z',
 layers:'M12 3l9 5-9 5-9-5 9-5zM3 13l9 5 9-5',
 game:'M6 8h12a4 4 0 014 4v2a3 3 0 01-5 2l-1-2H8l-1 2a3 3 0 01-5-2v-2a4 4 0 014-4zM8 11v3M6.5 12.5h3M16 12h.01M18 13h.01',
 group:'M9 11a3.5 3.5 0 100-7 3.5 3.5 0 000 7zM2 20c0-3.5 3-5.5 7-5.5s7 2 7 5.5M17 11a3 3 0 100-6M18 14.7c2.5.4 4 2 4 4.3',
 code:'M8 7l-5 5 5 5M16 7l5 5-5 5M14 4l-4 16',
 trash:'M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 11v6M14 11v6',
 search:'M11 18a7 7 0 100-14 7 7 0 000 14zM21 21l-5-5',
 sun:'M12 16a4 4 0 100-8 4 4 0 000 8zM12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5L19 19M5 19l1.5-1.5M17.5 6.5L19 5',
 moon:'M20 14A8 8 0 019.9 4 8 8 0 1020 14z',
 copy:'M9 9h11v11H9zM5 15V4h11',
 side:'M3 5h18v14H3zM9 5v14',
 ext:'M14 4h6v6M20 4l-9 9M18 14v6H4V6h6',
 warn:'M12 3l10 18H2L12 3zM12 10v5M12 18h.01',
 x:'M6 6l12 12M18 6L6 18'
};
const ic=(n,s=20)=>`<svg viewBox="0 0 24 24" width="${s}" height="${s}" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="${ICONS[n]}"/></svg>`;

const TABS=[
 {id:'start',label:'Start',icon:'home',color:'#0a84ff',desc:''},
 {id:'profil',label:'Profil',icon:'user',color:'#30d158',desc:'Freunde, Follower, Folgt, Online-Status, frühere Namen und ein großes Profilbild.'},
 {id:'items',label:'Items',icon:'shirt',color:'#ff9f0a',badge:'cItems',desc:'Alles, was der Avatar gerade trägt – mit Ersteller, Preis und Avatar-Maßen.'},
 {id:'outfits',label:'Outfits',icon:'layers',color:'#bf5af2',badge:'cOutfits',desc:'Gespeicherte Outfits. Ein Klick auf ein Outfit zeigt alle Items darin.'},
 {id:'games',label:'Spiele',icon:'game',color:'#ff375f',badge:'cGames',desc:'Öffentliche Spiele mit Icon, Besuchen und Link.'},
 {id:'groups',label:'Gruppen',icon:'group',color:'#5ac8fa',badge:'cGroups',desc:'Gruppen mit Rolle, Mitgliederzahl und Icon.'},
 {id:'raw',label:'Rohdaten',icon:'code',color:'#8e8e93',desc:''}
];
const ACC=[['Blau','#0a84ff'],['Violett','#bf5af2'],['Rosa','#ff375f'],['Orange','#ff9f0a'],['Grün','#30d158'],['Graphit','#8e8e93']];

const win=$('#win');let moved=false;
const rlCache=new Map();
let searchToken=0;

function newExtra(){return{friends:undefined,followers:undefined,following:undefined,history:undefined,presence:undefined,groups:undefined,games:undefined}}
function newData(){return{userId:null,user:null,avatar:undefined,outfits:undefined,outfitsErr:null,retryOutfits:null,outfitThumbs:{},assetThumbs:{},gameThumbs:{},groupThumbs:{},assetMeta:{},extra:newExtra(),currentThumb:null,raw:{}}}
let data=newData();

/* ---------- Sidebar + Dock ---------- */
$('#nav').innerHTML='<div id="sel"></div>'+TABS.map(t=>`<div class="ni" data-tab="${t.id}"><span class="ico" style="background:${t.color}">${ic(t.icon,15)}</span><span>${t.label}</span>${t.badge?`<span class="bd" id="${t.badge}"></span>`:''}</div>`).join('');
$('#dock').innerHTML=TABS.map(t=>`<div class="di" data-tab="${t.id}" style="background:${t.color}">${ic(t.icon,24)}<span class="tip">${t.label}</span></div>`).join('')+'<div class="dsep"></div><div class="di" id="dkTrash" style="background:#636366">'+ic('trash',24)+'<span class="tip">Verlauf &amp; Cache leeren</span></div>';
$('#swatches').innerHTML=ACC.map(a=>`<span class="sw" data-c="${a[1]}" title="${a[0]}" style="background:${a[1]}"></span>`).join('');
$('#logo').innerHTML=ic('user',42);
$('#sbBtn').innerHTML=ic('side',18);
$('#pill').insertAdjacentHTML('afterbegin',ic('search',16));
$('#feat').innerHTML=TABS.filter(t=>t.desc).map((t,i)=>`<div class="card fcard" data-tab="${t.id}" style="--i:${i}"><span class="ico" style="background:${t.color}">${ic(t.icon,20)}</span><b>${t.label}</b><p>${t.desc}</p></div>`).join('');

/* ---------- Menüleiste ---------- */
const MENUS={
 app:[['Über Avatar Finder',()=>showAlert('Avatar Finder','Roblox-Profile ansehen: Items, Outfits, Spiele, Gruppen und mehr.\nDie Daten kommen über deinen lokalen Server (Python) mit SQLite-Cache.')],'-',['Cache im Browser leeren',()=>{rlCache.clear();notify('Cache geleert','Der Browser-Cache ist leer.')}],['Zuletzt gesucht löschen',clearRecent]],
 file:[['Neue Suche',focusSearch,'Strg K'],'-',['ID kopieren',()=>copyText(data.userId)],['Namen kopieren',()=>copyText(data.user&&data.user.name)],['Profil auf Roblox öffnen',()=>data.userId?window.open('https://www.roblox.com/users/'+data.userId+'/profile','_blank'):notify('Kein Profil','Such zuerst einen Spieler.')],'-',['Rohdaten kopieren',()=>copyText(data.user?JSON.stringify(data.raw,null,2):'')]],
 view:[['Dunkelmodus umschalten',()=>setTheme(!document.documentElement.classList.contains('dark'))],['Seitenleiste ein/aus',()=>{win.classList.toggle('collapsed');setTimeout(moveSel,400)}],'-'].concat(ACC.map(a=>['Akzent: '+a[0],()=>setAcc(a[1])])),
 win:[['Minimieren',()=>win.classList.add('hide')],['Zoom',toggleZoom],['Zentrieren',()=>{moved=false;win.classList.remove('zoom');center()}]],
 help:[['Tastenkürzel',()=>showAlert('Tastenkürzel','Strg/⌘ + K  oder  /   →  Suche\nEsc  →  Menüs und Fenster schließen')],['Roblox API Doku',()=>window.open('https://create.roblox.com/docs/cloud/reference','_blank')]]
};
const MB=[['app',ic('user',14)+'&nbsp;<b>Avatar Finder</b>'],['file','Ablage'],['view','Darstellung'],['win','Fenster'],['help','Hilfe']];
$('#mbar').innerHTML=MB.map(m=>`<span class="mb" data-m="${m[0]}">${m[1]}</span>`).join('')+'<div class="mbr"><button class="iconbtn" id="thBtn" title="Hell/Dunkel"></button><span id="clock"></span></div>';
let openMenu=null;
function openM(key,el){
 const items=MENUS[key],m=$('#menu');
 m.innerHTML=items.map((it,i)=>it==='-'?'<div class="msep"></div>':`<div class="mi" data-i="${i}"><span>${esc(it[0])}</span><span class="sc">${esc(it[2]||'')}</span></div>`).join('');
 m._items=items;
 m.style.left=el.getBoundingClientRect().left+'px';
 m.classList.add('show');
 $$('.mb').forEach(x=>x.classList.toggle('on',x===el));
 openMenu=key;
}
function closeM(){$('#menu').classList.remove('show');$$('.mb').forEach(x=>x.classList.remove('on'));openMenu=null}
$$('.mb').forEach(el=>el.addEventListener('mouseenter',()=>{if(openMenu&&openMenu!==el.dataset.m)openM(el.dataset.m,el)}));

/* ---------- Theme / Akzent ---------- */
function setTheme(d){document.documentElement.classList.toggle('dark',d);localStorage.setItem('af-theme',d?'dark':'light');$('#thBtn').innerHTML=ic(d?'sun':'moon',16)}
function setAcc(c){document.documentElement.style.setProperty('--acc',c);localStorage.setItem('af-accent',c);$$('.sw').forEach(s=>s.classList.toggle('on',s.dataset.c===c))}
const savedTheme=localStorage.getItem('af-theme');
setTheme(savedTheme?savedTheme==='dark':matchMedia('(prefers-color-scheme:dark)').matches);
setAcc(localStorage.getItem('af-accent')||'#0a84ff');
$('#thBtn').onclick=()=>setTheme(!document.documentElement.classList.contains('dark'));

/* ---------- Toasts / Alerts / Kopieren ---------- */
function notify(t,m){const d=document.createElement('div');d.className='toast';d.innerHTML=`<b>${esc(t)}</b><span>${esc(m||'')}</span>`;$('#toasts').appendChild(d);setTimeout(()=>{d.classList.add('out');setTimeout(()=>d.remove(),320)},3200)}
function showAlert(title,msg,btns){
 btns=btns||[{t:'OK',pri:true}];
 $('#alertBox').innerHTML=`<h3>${esc(title)}</h3><p>${esc(msg)}</p><div class="bts"></div>`;
 const box=$('#alertBox .bts');
 btns.forEach(b=>{const x=document.createElement('button');x.textContent=b.t;if(b.pri)x.className='pri';x.onclick=()=>{closeAlert();if(b.fn)b.fn()};box.appendChild(x)});
 $('#alertBg').classList.add('show');
}
function closeAlert(){$('#alertBg').classList.remove('show')}
function copyText(t){
 if(!t){notify('Nichts zu kopieren','Such zuerst einen Spieler.');return}
 t=String(t);
 const done=()=>notify('Kopiert',t.length>60?t.slice(0,60)+' …':t);
 function fb(){const a=document.createElement('textarea');a.value=t;a.style.cssText='position:fixed;opacity:0';document.body.appendChild(a);a.select();try{document.execCommand('copy');done()}catch(e){notify('Fehler','Kopieren nicht möglich')}a.remove()}
 if(navigator.clipboard&&window.isSecureContext)navigator.clipboard.writeText(t).then(done,fb);else fb();
}

/* ---------- Fenster ---------- */
function center(){if(moved||win.classList.contains('zoom'))return;win.style.left=Math.max(8,(innerWidth-win.offsetWidth)/2)+'px';win.style.top='46px'}
function showWin(){win.classList.remove('hide')}
function toggleZoom(){win.classList.toggle('zoom');setTimeout(moveSel,400)}
$('#l1').onclick=$('#l2').onclick=()=>{win.classList.add('hide');notify('Fenster minimiert','Klick aufs Dock, um es zurückzuholen.')};
$('#l3').onclick=toggleZoom;
$('#sbBtn').onclick=()=>{win.classList.toggle('collapsed');setTimeout(moveSel,400)};
$('#tb').addEventListener('pointerdown',e=>{
 if(e.button!==0||e.target.closest('input,button,.pill,.iconbtn')||win.classList.contains('zoom'))return;
 const sx=e.clientX,sy=e.clientY,ox=win.offsetLeft,oy=win.offsetTop;
 moved=true;win.classList.add('drag');
 const mv=ev=>{win.style.left=Math.min(Math.max(ox+ev.clientX-sx,-win.offsetWidth+100),innerWidth-100)+'px';win.style.top=Math.min(Math.max(oy+ev.clientY-sy,32),innerHeight-60)+'px'};
 const up=()=>{win.classList.remove('drag');removeEventListener('pointermove',mv);removeEventListener('pointerup',up)};
 addEventListener('pointermove',mv);addEventListener('pointerup',up);
});
$('#tb').addEventListener('dblclick',e=>{if(!e.target.closest('input,button,.pill,.iconbtn'))toggleZoom()});
addEventListener('resize',()=>{center();moveSel()});

/* ---------- Tabs ---------- */
let cur='start';
function moveSel(){const a=$('.ni.act'),s=$('#sel');if(a&&s){s.style.transform='translateY('+a.offsetTop+'px)';s.style.height=a.offsetHeight+'px'}}
function showTab(id){
 cur=id;
 $$('.tab').forEach(s=>s.classList.toggle('show',s.id==='t-'+id));
 $$('[data-tab]').forEach(el=>{if(el.classList.contains('ni')||el.classList.contains('di'))el.classList.toggle('act',el.dataset.tab===id)});
 const t=TABS.find(x=>x.id===id);
 $('#ttl').textContent=t.label+(data.user&&id!=='start'?' · '+(data.user.displayName||data.user.name):'');
 moveSel();
 if(id==='raw')renderRaw();
 $('#content').scrollTop=0;
 if(innerWidth<=760)win.classList.add('collapsed');
}

/* ---------- Dock-Vergrößerung ---------- */
const dock=$('#dock');
dock.addEventListener('mousemove',e=>{
 const base=parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--base'))||54,MAXS=1.75,RANGE=150;
 dock.querySelectorAll('.di').forEach(el=>{
  const r=el.getBoundingClientRect(),d=Math.abs(e.clientX-(r.left+r.width/2));
  const f=Math.pow(Math.cos(Math.min(d/RANGE,1)*Math.PI/2),1.2);
  el.style.setProperty('--s',(base*(1+(MAXS-1)*f)).toFixed(1)+'px');
 });
});
dock.addEventListener('mouseleave',()=>dock.querySelectorAll('.di').forEach(el=>el.style.removeProperty('--s')));
$('#dkTrash').onclick=()=>showAlert('Verlauf & Cache leeren?','Das löscht deine „Zuletzt gesucht“-Liste und den Browser-Cache.',[{t:'Abbrechen'},{t:'Leeren',pri:true,fn:()=>{rlCache.clear();clearRecent()}}]);

/* ---------- Netzwerk ---------- */
async function fetchRawOrig(url){
 const target=url.startsWith('/x/')?url:'/api/'+encodeURIComponent(url);
 const ctl=new AbortController(),to=setTimeout(()=>ctl.abort(),45000);
 let r;
 try{r=await fetch(target,{signal:ctl.signal})}
 catch(e){throw new Error(e.name==='AbortError'?'Zeitüberschreitung – Roblox antwortet gerade nicht.':'Server nicht erreichbar. Läuft das Python-Fenster noch?')}
 finally{clearTimeout(to)}
 if(r.status===404||r.status===400){const e=new Error('Dazu wurde nichts gefunden.');e.fatal=true;e.status=r.status;throw e}
 if(r.status===401||r.status===403){const e=new Error('Zugriff verweigert.');e.fatal=true;e.status=r.status;throw e}
 if(!r.ok){const e=new Error('Roblox meldet Fehler '+r.status+' (evtl. Rate Limit) – gleich nochmal versuchen.');e.status=r.status;throw e}
 return r.json();
}
const RL={max:8,gap:50,ttl:5*60*1000};
let active=0;const queue=[];
function pump(){while(active<RL.max&&queue.length){const j=queue.shift();active++;j.run().then(j.res,j.rej).finally(()=>setTimeout(()=>{active--;pump()},RL.gap))}}
function enqueue(fn,prio){return new Promise((res,rej)=>{const j={run:fn,res,rej};if(prio)queue.unshift(j);else queue.push(j);pump()})}
function fetchRaw(url,fresh,prio){
 const c=rlCache.get(url);
 if(!fresh&&c&&Date.now()-c.t<RL.ttl)return c.p;
 const p=enqueue(()=>fetchRawOrig(url),prio);
 rlCache.set(url,{t:Date.now(),p});
 p.catch(()=>{const x=rlCache.get(url);if(x&&x.p===p)rlCache.delete(url)});
 return p;
}
async function thumbMap(base,ids,tok){
 ids=[...new Set(ids.map(String))];
 const out={};let todo=ids;
 for(let att=0;att<4&&todo.length;att++){
  if(att>0)await new Promise(r=>setTimeout(r,1200));
  if(tok!==searchToken)return out;
  const rest=[];
  for(let i=0;i<todo.length;i+=50){
   const part=todo.slice(i,i+50);
   try{
    const r=await fetchRaw(base+part.join(','),att>0);
    (r.data||[]).forEach(d=>{
     if(d.state==='Completed'&&d.imageUrl)out[d.targetId]=d.imageUrl;
     else if(d.state==='Pending'||d.state==='InReview')rest.push(String(d.targetId));
    });
   }catch(e){part.forEach(x=>rest.push(x))}
  }
  todo=rest;
 }
 return out;
}

/* ---------- Render-Helfer ---------- */
const loadingState=t=>`<div class="state"><div class="spinner"></div><p>${esc(t)}</p></div>`;
const errState=t=>`<div class="state">${ic('warn',34)}<p>${esc(t)}</p></div>`;
const emptyState=t=>`<div class="state">${ic('search',34)}<p>${esc(t)}</p></div>`;
const needUser=()=>`<div class="state">${ic('search',34)}<p>Such zuerst nach einem Spieler.</p><button class="btn pri" onclick="focusSearch()">Suchen</button></div>`;
const L=x=>x==null?x:x.length;
const pc=v=>typeof v==='number'?Math.round(v*100)+'%':null;
function countUp(el,n){
 if(el.dataset.v===String(n))return;
 el.dataset.v=String(n);
 const t0=performance.now(),d=700;
 (function f(t){const k=Math.min(1,Math.max(0,(t-t0)/d)),e=1-Math.pow(1-k,3);el.textContent=Math.round(n*e).toLocaleString('de-DE');if(k<1)requestAnimationFrame(f)})(t0);
}
function setStat(id,v){const el=document.getElementById(id);if(!el)return;if(v===undefined){el.textContent='…';el.dataset.v=''}else if(v===null){el.textContent='–';el.dataset.v=''}else countUp(el,v)}
function presencePill(p){
 if(!p)return '';
 const t=p.userPresenceType;let cls='off',txt='Offline';
 if(t===2){cls='game';txt='im Spiel'+(p.lastLocation?' · '+p.lastLocation:'')}
 else if(t===3){cls='studio';txt='im Studio'}
 else if(t===1){cls='on';txt='Online'}
 else if(p.lastOnline){txt='Offline · zuletzt '+new Date(p.lastOnline).toLocaleString('de-DE',{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'})}
 return `<span class="pres ${cls}"><i class="dot"></i>${esc(txt)}</span>`;
}
const STATS=[['stFriends','Freunde'],['stFollowers','Follower'],['stFollowing','Folgt'],['stItems','Items'],['stOutfits','Outfits'],['stGames','Spiele'],['stGroups','Gruppen']];

function renderProfile(){
 const el=$('#t-profil');
 if(!data.user){el.innerHTML=needUser();return}
 const u=data.user,id=data.userId;
 const created=u.created?new Date(u.created).toLocaleDateString('de-DE',{day:'numeric',month:'long',year:'numeric'}):'–';
 el.innerHTML=`<div class="card prof">
  <div class="avbox" id="avbox"></div>
  <div class="pinfo">
   <h1>${esc(u.displayName||u.name)}${u.hasVerifiedBadge?'<span class="vb" title="Verifiziert">✓</span>':''}</h1>
   <div class="sub">@${esc(u.name)} <button class="chip" data-copy="${esc(id)}" title="ID kopieren">ID ${esc(id)} ${ic('copy',12)}</button></div>
   <div class="mt" id="pfPres"></div>
   ${u.description?`<p class="desc">${esc(u.description)}</p>`:''}
   <div class="mt pills">${u.isBanned?'<span class="tag red">Gesperrt</span>':''}<span class="tag">Beigetreten ${esc(created)}</span></div>
   <div class="stats">${STATS.map(s=>`<div class="stat"><b id="${s[0]}">…</b><span>${s[1]}</span></div>`).join('')}</div>
   <div id="pfHist"></div>
   <div class="acts">
    <a class="btn pri" target="_blank" rel="noopener" href="https://www.roblox.com/users/${esc(id)}/profile">${ic('ext',15)} Auf Roblox öffnen</a>
    <button class="btn" data-copy="${esc(id)}">${ic('copy',15)} ID kopieren</button>
    <button class="btn" data-copy="${esc(u.name)}">Namen kopieren</button>
   </div>
  </div></div>`;
 setProfImg();updExtra();
}
function setProfImg(){const b=$('#avbox');if(!b)return;b.innerHTML=data.currentThumb?`<img src="${esc(data.currentThumb)}" alt="Avatar">`:'<div class="ph"></div>'}
function updExtra(){
 if(!data.user)return;
 const E=data.extra;
 setStat('stFriends',E.friends);setStat('stFollowers',E.followers);setStat('stFollowing',E.following);
 setStat('stItems',data.avatar===undefined?undefined:(data.avatar===null?null:(data.avatar.assets||[]).length));
 setStat('stOutfits',L(data.outfits));
 setStat('stGames',L(E.games));setStat('stGroups',L(E.groups));
 const pp=$('#pfPres');if(pp)pp.innerHTML=presencePill(E.presence);
 const ph=$('#pfHist');
 if(ph)ph.innerHTML=Array.isArray(E.history)&&E.history.length?`<div class="sec2">Frühere Namen</div><div class="pills">${E.history.map(n=>`<span class="tag">${esc(n)}</span>`).join('')}</div>`:'';
 updBadges();
}
function updBadges(){
 const m={cItems:data.avatar&&data.avatar.assets,cOutfits:data.outfits,cGames:data.extra.games,cGroups:data.extra.groups};
 for(const k in m){const el=document.getElementById(k);if(!el)continue;const v=m[k];if(data.user&&Array.isArray(v)){el.textContent=v.length;el.style.display='block'}else el.style.display='none'}
}
function itemCard(x,i){
 const th=data.assetThumbs[x.id];
 return `<div class="card" style="--i:${Math.min(i,14)}"><div class="th" data-aid="${x.id}">${th?`<img src="${esc(th)}" loading="lazy" alt="">`:'<div class="ph"></div>'}</div><div class="cb"><div class="nm" title="${esc(x.name)}">${esc(x.name)}</div><div class="ty">${esc((x.assetType&&x.assetType.name)||'Item')}</div><div class="meta" data-meta="${x.id}"></div><div class="row"><button class="chip" data-copy="${x.id}" title="ID kopieren">${x.id} ${ic('copy',11)}</button><a class="lnk" target="_blank" rel="noopener" href="https://www.roblox.com/catalog/${x.id}" title="Im Katalog öffnen">${ic('ext',14)}</a></div></div></div>`;
}
function applyMeta(){
 $$('[data-meta]').forEach(el=>{
  const m=data.assetMeta[el.dataset.meta];if(!m)return;
  const p=[];
  if(m.creatorName)p.push('von '+m.creatorName);
  if(typeof m.price==='number')p.push(m.price===0?'Gratis':m.price.toLocaleString('de-DE')+' R$');
  else if(m.priceStatus)p.push(m.priceStatus==='Off Sale'?'Nicht verkäuflich':m.priceStatus);
  el.textContent=p.join(' · ');
 });
}
function fillThumbs(){
 $$('#sheet [data-aid]').forEach(el=>{
  const t=data.assetThumbs[el.dataset.aid];
  if(t&&!el.querySelector('img'))el.innerHTML=`<img src="${esc(t)}" alt="">`;
 });
}
function renderItems(){
 const el=$('#t-items');
 if(!data.user){el.innerHTML=needUser();return}
 const a=data.avatar;
 if(a===undefined){el.innerHTML=loadingState('Lade Items …');return}
 if(a===null){el.innerHTML=errState('Items konnten nicht geladen werden.');return}
 const list=a.assets||[],sc=a.scales||{};
 const info=[['Typ',a.playerAvatarType],['Größe',pc(sc.height)],['Breite',pc(sc.width)],['Kopf',pc(sc.head)],['Tiefe',pc(sc.depth)],['Proportion',pc(sc.proportion)]].filter(x=>x[1]);
 el.innerHTML=(info.length?`<div class="pills" style="margin-bottom:16px">${info.map(x=>`<span class="tag">${x[0]}: <b>${esc(x[1])}</b></span>`).join('')}</div>`:'')+
  (list.length?`<div class="grid">${list.map(itemCard).join('')}</div>`:emptyState('Dieser Avatar trägt keine Items.'));
 applyMeta();
}
function renderOutfits(){
 const el=$('#t-outfits');
 if(!data.user){el.innerHTML=needUser();return}
 const o=data.outfits;
 if(o===undefined){el.innerHTML=loadingState('Lade Outfits …');return}
 if(o===null){
  const s=data.outfitsErr&&data.outfitsErr.status;
  const why=(s===401||s===403||s===400)?'Die Outfits dieses Spielers sind privat oder nur für Freunde sichtbar. Roblox zeigt sie nicht öffentlich.'
   :s===404?'Roblox hat für dieses Profil keine Outfits gefunden.'
   :s===429?'Roblox begrenzt gerade die Outfit-Vorschau. Kurz warten und nochmal versuchen.'
   :'Die Outfits konnten gerade nicht von Roblox geladen werden.';
  el.innerHTML=`<div class="state">${ic('warn',34)}<p>${esc(why)}${s?' (Code '+s+')':''}</p><button class="btn pri" id="outRetry">Nochmal versuchen</button></div>`;
  $('#outRetry').onclick=()=>data.retryOutfits&&data.retryOutfits();
  return;
 }
 if(!o.length){el.innerHTML=emptyState('Keine gespeicherten Outfits.');return}
 el.innerHTML=`<div class="grid">${o.map((x,i)=>{const th=data.outfitThumbs[x.id];return `<div class="card clk" data-outfit="${x.id}" style="--i:${Math.min(i,14)}"><div class="th">${th?`<img src="${esc(th)}" loading="lazy" alt="">`:'<div class="ph"></div>'}</div><div class="cb"><div class="nm" title="${esc(x.name)}">${esc(x.name)}</div><div class="ty">Klicken für Items</div><div class="row"><button class="chip" data-copy="${x.id}" title="ID kopieren">${x.id} ${ic('copy',11)}</button></div></div></div>`}).join('')}</div>`;
}
function renderGames(){
 const el=$('#t-games');
 if(!data.user){el.innerHTML=needUser();return}
 const g=data.extra.games;
 if(g===undefined){el.innerHTML=loadingState('Lade Spiele …');return}
 if(g===null){el.innerHTML=errState('Spiele konnten nicht geladen werden.');return}
 if(!g.length){el.innerHTML=emptyState('Keine öffentlichen Spiele.');return}
 el.innerHTML=`<div class="grid w">${g.map((x,i)=>{
  const th=data.gameThumbs[x.id],pid=x.rootPlace&&x.rootPlace.id,yr=x.created?new Date(x.created).getFullYear():'';
  return `<div class="card rowcard" style="--i:${Math.min(i,14)}"><div class="gi">${th?`<img src="${esc(th)}" loading="lazy" alt="">`:'<div class="ph"></div>'}</div><div class="gb"><div class="nm" title="${esc(x.name)}">${esc(x.name)}</div><div class="ty">${(x.placeVisits||0).toLocaleString('de-DE')} Besuche${yr?' · seit '+yr:''}</div><div class="row">${pid?`<button class="chip" data-copy="${pid}" title="Place-ID kopieren">${pid} ${ic('copy',11)}</button><a class="lnk" target="_blank" rel="noopener" href="https://www.roblox.com/games/${pid}">${ic('ext',14)}</a>`:''}</div></div></div>`}).join('')}</div>`;
}
function renderGroups(){
 const el=$('#t-groups');
 if(!data.user){el.innerHTML=needUser();return}
 const g=data.extra.groups;
 if(g===undefined){el.innerHTML=loadingState('Lade Gruppen …');return}
 if(g===null){el.innerHTML=errState('Gruppen konnten nicht geladen werden.');return}
 if(!g.length){el.innerHTML=emptyState('In keiner Gruppe.');return}
 el.innerHTML=`<div class="grid w">${g.map((x,i)=>{
  const gr=x.group,th=data.groupThumbs[gr.id];
  return `<div class="card rowcard" style="--i:${Math.min(i,14)}"><div class="gi">${th?`<img src="${esc(th)}" loading="lazy" alt="">`:'<div class="ph"></div>'}</div><div class="gb"><div class="nm" title="${esc(gr.name)}">${esc(gr.name)}</div><div class="ty">${esc(x.role.name)} · ${(gr.memberCount||0).toLocaleString('de-DE')} Mitglieder</div><div class="row"><button class="chip" data-copy="${gr.id}" title="ID kopieren">${gr.id} ${ic('copy',11)}</button><a class="lnk" target="_blank" rel="noopener" href="https://www.roblox.com/groups/${gr.id}">${ic('ext',14)}</a></div></div></div>`}).join('')}</div>`;
}
function renderRaw(){
 const el=$('#t-raw');
 if(!data.user){el.innerHTML=needUser();return}
 const j=JSON.stringify(data.raw,null,2);
 el.innerHTML=`<div class="acts" style="margin:0 0 14px"><button class="btn" id="rawCopy">${ic('copy',15)} JSON kopieren</button></div><pre class="raw">${esc(j)}</pre>`;
 $('#rawCopy').onclick=()=>copyText(j);
}
function renderAll(){renderProfile();renderItems();renderOutfits();renderGames();renderGroups();updBadges()}

/* ---------- Outfit-Fenster (Items eines Outfits) ---------- */
let sheetTok=0;
function closeSheet(){sheetTok++;$('#sheetBg').classList.remove('show')}
function sheetShell(name,id,inner,info){
 const th=data.outfitThumbs[id];
 return `<div class="sh-head"><div class="sh-img">${th?`<img src="${esc(th)}" alt="">`:'<div class="ph"></div>'}</div><div class="sh-t"><h3 title="${esc(name)}">${esc(name)}</h3><div class="sub"><button class="chip" data-copy="${esc(id)}" title="ID kopieren">Outfit-ID ${esc(id)} ${ic('copy',12)}</button></div>${info||''}</div><button class="iconbtn" data-close="1" title="Schließen">${ic('x',18)}</button></div><div class="sh-body">${inner}</div>`;
}
async function openOutfit(id,fresh){
 id=String(id);
 const D=data,tok=++sheetTok;
 const o=(D.outfits||[]).find(x=>String(x.id)===id);
 const name=o?o.name:'Outfit';
 $('#sheetBg').classList.add('show');
 $('#sheet').innerHTML=sheetShell(name,id,loadingState('Lade Items …'));
 try{
  // Der Server liefert die Item-Liste sofort, Bilder und Preise kommen danach
  const d=await fetchRaw('/x/outfit/'+id+(fresh?'?fresh=1':''),fresh,true);
  if(tok!==sheetTok||data!==D)return;
  D.raw.outfitDetails=D.raw.outfitDetails||{};D.raw.outfitDetails[id]=d;
  const items=d.items||[];
  items.forEach(x=>{
   if(x.thumbnail)D.assetThumbs[x.id]=x.thumbnail;
   if(x.creatorName||typeof x.price==='number'||x.priceStatus)D.assetMeta[x.id]={creatorName:x.creatorName,price:x.price,priceStatus:x.priceStatus};
  });
  const list=items.map(x=>({id:x.id,name:x.name,assetType:{name:x.type}}));
  const sc=d.scale||{};
  const info=[['Typ',d.playerAvatarType],['Größe',pc(sc.height)],['Breite',pc(sc.width)],['Kopf',pc(sc.head)]].filter(x=>x[1]);
  const pills=`<div class="pills" style="margin-top:8px"><span class="tag"><b>${list.length}</b> Items</span>${info.map(x=>`<span class="tag">${x[0]}: <b>${esc(x[1])}</b></span>`).join('')}</div>`;
  $('#sheet').innerHTML=sheetShell(d.name||name,id,list.length?`<div class="grid">${list.map(itemCard).join('')}</div>`:emptyState('Dieses Outfit enthält keine Items.'),pills);
  applyMeta();fillThumbs();
  const ids=[...new Set(list.map(x=>x.id))];
  // Fehlende Bilder nachladen
  const needT=ids.filter(x=>!D.assetThumbs[x]);
  if(needT.length)thumbMap(AS_TH,needT,searchToken).then(m=>{Object.assign(D.assetThumbs,m);if(tok===sheetTok&&data===D)fillThumbs()});
  // Fehlenden Preis/Ersteller nachladen
  const needM=ids.filter(x=>!D.assetMeta[x]).slice(0,50);
  if(needM.length)fetchRaw('/x/catalog?ids='+needM.join(',')).then(r=>{(r.data||[]).forEach(x=>{D.assetMeta[x.id]=x});if(tok===sheetTok&&data===D)applyMeta()}).catch(()=>{});
 }catch(e){
  if(tok!==sheetTok||data!==D)return;
  const s=e.status;
  const why=s===429?'Roblox begrenzt gerade die Item-Details. Kurz warten und nochmal versuchen.'
   :(s===400||s===401||s===403||s===404)?'Roblox gibt für dieses Outfit keine Items frei (privat, gelöscht oder gerade nicht verfügbar).'
   :(e.message||'Die Item-Details konnten gerade nicht geladen werden.');
  $('#sheet').innerHTML=sheetShell(name,id,`<div class="state">${ic('warn',34)}<p>${esc(why)}${s?' (Code '+s+')':''}</p><button class="btn pri" id="shRetry">Nochmal versuchen</button></div>`);
  $('#shRetry').onclick=()=>openOutfit(id,true);
 }
}

/* ---------- Suche ---------- */
const AV_TH='https://thumbnails.roblox.com/v1/users/avatar?size=720x720&format=Png&isCircular=false&userIds=';
const AS_TH='https://thumbnails.roblox.com/v1/assets?size=150x150&format=Png&isCircular=false&assetIds=';
const OU_TH='https://thumbnails.roblox.com/v1/users/outfits?size=150x150&format=Png&isCircular=false&userOutfitIds=';
const GR_TH='https://thumbnails.roblox.com/v1/groups/icons?size=150x150&format=Png&isCircular=false&groupIds=';
const GM_TH='https://thumbnails.roblox.com/v1/games/icons?size=150x150&format=Png&isCircular=false&universeIds=';

function setBusy(v){$('#pill').classList.toggle('busy',v)}
function focusSearch(){closeSheet();showWin();const q=$('#q');q.focus();q.select()}

async function resolveName(name){
 if(/^[A-Za-z0-9_]{1,20}$/.test(name)){
  try{const r=await fetchRaw('/x/username?name='+encodeURIComponent(name));const u=r.data&&r.data[0];if(u)return String(u.id)}catch(e){}
 }
 const r=await fetchRaw('https://users.roblox.com/v1/users/search?keyword='+encodeURIComponent(name)+'&limit=10');
 const m=(r.data||[]).find(u=>u.name.toLowerCase()===name.toLowerCase());
 if(m)return String(m.id);
 throw new Error('Kein Spieler mit dem Namen „'+name+'“ gefunden.');
}

async function search(q){
 q=String(q||'').trim().replace(/^@/,'');
 if(!q){focusSearch();return}
 const tok=++searchToken;setBusy(true);
 try{
  const id=/^\d+$/.test(q)?q:await resolveName(q);
  if(tok!==searchToken)return;
  await loadAll(id,tok);
  if(tok===searchToken&&data.user)$('#q').value=data.user.name;
 }catch(e){
  if(tok===searchToken)showAlert('Das hat nicht geklappt…',e.message||'Unbekannter Fehler');
 }finally{
  if(tok===searchToken)setBusy(false);
 }
}

async function loadAll(id,tok){
 const user=await fetchRaw('https://users.roblox.com/v1/users/'+id);
 if(tok!==searchToken)return;
 closeSheet();
 const uid=String(user.id),D=newData(),E=D.extra,ok=()=>tok===searchToken,upd=()=>{if(ok())updExtra()};
 D.userId=uid;D.user=user;D.raw={user,extra:E};data=D;
 renderAll();showTab('profil');

 // Outfits zuerst (Server liefert Liste + Bilder in einem Rutsch)
 const runOutfits=(retry)=>{
  D.outfits=undefined;D.outfitsErr=null;renderOutfits();upd();
  fetchRaw('/x/outfits/'+uid+(retry?'?fresh=1':''),!!retry,true).then(r=>{
   if(!ok())return;
   const rows=r.data||[];
   D.outfits=rows.map(o=>({id:o.id,name:o.name}));
   rows.forEach(o=>{if(o.thumbnail)D.outfitThumbs[o.id]=o.thumbnail});
   D.raw.outfits=r;renderOutfits();upd();
   const miss=D.outfits.map(o=>o.id).filter(i=>!D.outfitThumbs[i]);
   if(miss.length)thumbMap(OU_TH,miss,tok).then(m=>{if(ok()){Object.assign(D.outfitThumbs,m);renderOutfits()}});
  }).catch(e=>{if(ok()){D.outfits=null;D.outfitsErr=e;renderOutfits();upd()}});
 };
 D.retryOutfits=()=>runOutfits(true);
 runOutfits(false);

 // Avatar-Bild (groß)
 thumbMap(AV_TH,[uid],tok).then(m=>{if(!ok())return;D.currentThumb=m[uid]||null;setProfImg();recentSave()});

 // Items
 fetchRaw('https://avatar.roblox.com/v1/users/'+uid+'/avatar').then(a=>{
  if(!ok())return;
  D.avatar=a;D.raw.avatar=a;renderItems();upd();
  const ids=(a.assets||[]).map(x=>x.id);
  thumbMap(AS_TH,ids,tok).then(m=>{if(ok()){Object.assign(D.assetThumbs,m);renderItems()}});
  if(ids.length)fetchRaw('/x/catalog?ids='+ids.slice(0,50).join(',')).then(r=>{if(!ok())return;(r.data||[]).forEach(x=>{D.assetMeta[x.id]=x});applyMeta()}).catch(()=>{});
 }).catch(()=>{if(ok()){D.avatar=null;renderItems();upd()}});

 // Zahlen
 const cnt=(k,u)=>fetchRaw('https://friends.roblox.com/v1/users/'+uid+u).then(r=>{if(ok()){E[k]=r.count;upd()}}).catch(()=>{if(ok()){E[k]=null;upd()}});
 cnt('friends','/friends/count');cnt('followers','/followers/count');cnt('following','/followings/count');

 // Frühere Namen
 fetchRaw('https://users.roblox.com/v1/users/'+uid+'/username-history?limit=50&sortOrder=Desc').then(r=>{if(ok()){E.history=(r.data||[]).map(x=>x.name);upd()}}).catch(()=>{if(ok()){E.history=null;upd()}});

 // Online-Status
 fetchRaw('/x/presence/'+uid).then(r=>{if(ok()){E.presence=(r.userPresences||[])[0]||null;upd()}}).catch(()=>{if(ok()){E.presence=null;upd()}});

 // Gruppen
 fetchRaw('https://groups.roblox.com/v1/users/'+uid+'/groups/roles').then(r=>{
  if(!ok())return;
  const l=(r.data||[]).filter(x=>x.group&&x.role).sort((a,b)=>b.role.rank-a.role.rank);
  E.groups=l;renderGroups();upd();
  thumbMap(GR_TH,l.map(x=>x.group.id),tok).then(m=>{if(ok()){D.groupThumbs=m;renderGroups()}});
 }).catch(()=>{if(ok()){E.groups=null;renderGroups();upd()}});

 // Spiele
 fetchRaw('https://games.roblox.com/v2/users/'+uid+'/games?accessFilter=Public&limit=50&sortOrder=Desc').then(r=>{
  if(!ok())return;
  const l=r.data||[];E.games=l;renderGames();upd();
  thumbMap(GM_TH,l.map(g=>g.id),tok).then(m=>{if(ok()){D.gameThumbs=m;renderGames()}});
 }).catch(()=>{if(ok()){E.games=null;renderGames();upd()}});
}

/* ---------- Zuletzt gesucht ---------- */
let recent=[];
function renderRecent(){
 const b=$('#recentBox');
 if(!recent.length){b.style.display='none';return}
 b.style.display='block';
 $('#recentList').innerHTML=recent.map(r=>`<button class="rchip" data-q="${esc(r.userId)}">${r.avatarUrl?`<img src="${esc(r.avatarUrl)}" alt="">`:'<span class="ph2"></span>'}<span><b>${esc(r.displayName||r.username)}</b> <i>@${esc(r.username)}</i></span></button>`).join('');
}
async function recentLoad(){try{const r=await fetch('/x/recent');recent=await r.json()}catch(e){recent=[]}renderRecent()}
async function recentSave(){
 const d=data;if(!d.user)return;
 try{await fetch('/x/recent',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({userId:d.userId,username:d.user.name,displayName:d.user.displayName,avatarUrl:d.currentThumb||''})})}catch(e){}
 recentLoad();
}
async function clearRecent(){
 try{await fetch('/x/recent',{method:'POST',headers:{'Content-Type':'application/json'},body:'{"clear":true}'})}catch(e){}
 recent=[];renderRecent();notify('Verlauf gelöscht','„Zuletzt gesucht“ ist leer.');
}
$('#recClear').onclick=clearRecent;

/* ---------- Events ---------- */
document.addEventListener('click',e=>{
 const mb=e.target.closest('.mb');
 if(mb){openMenu===mb.dataset.m?closeM():openM(mb.dataset.m,mb);return}
 const mi=e.target.closest('.mi');
 if(mi){const it=$('#menu')._items[+mi.dataset.i];closeM();if(it&&it[1])it[1]();return}
 closeM();
 if(e.target.id==='sheetBg'||e.target.closest('[data-close]')){closeSheet();return}
 const sw=e.target.closest('.sw');if(sw){setAcc(sw.dataset.c);return}
 const cp=e.target.closest('[data-copy]');if(cp){copyText(cp.dataset.copy);return}
 const oc=e.target.closest('[data-outfit]');if(oc){openOutfit(oc.dataset.outfit);return}
 const q=e.target.closest('[data-q]');if(q){showWin();search(q.dataset.q);return}
 const tb=e.target.closest('[data-tab]');if(tb){showWin();showTab(tb.dataset.tab);return}
});
$('#go').onclick=()=>search($('#q').value);
$('#q').addEventListener('keydown',e=>{if(e.key==='Enter')search($('#q').value)});
addEventListener('keydown',e=>{
 if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){e.preventDefault();focusSearch();return}
 if(e.key==='/'&&!/INPUT|TEXTAREA/.test(document.activeElement.tagName)){e.preventDefault();focusSearch();return}
 if(e.key==='Escape'){closeM();closeAlert();closeSheet();if(document.activeElement&&document.activeElement.blur)document.activeElement.blur()}
});
document.addEventListener('pointermove',e=>{
 const c=e.target.closest&&e.target.closest('.card');
 if(c){const r=c.getBoundingClientRect();c.style.setProperty('--mx',(e.clientX-r.left)+'px');c.style.setProperty('--my',(e.clientY-r.top)+'px')}
});

/* ---------- Start ---------- */
function clock(){$('#clock').textContent=new Date().toLocaleString('de-DE',{weekday:'short',day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'})}
if(innerWidth<=760)win.classList.add('collapsed');
renderAll();showTab('start');recentLoad();center();clock();setInterval(clock,30000);
addEventListener('load',()=>{moveSel();setTimeout(moveSel,300)});
if(location.protocol==='file:')setTimeout(()=>showAlert("So geht's nicht",'Bitte starte die Datei mit Python (python test.py). Dann öffnet sich die Seite von selbst.'),500);
</script>
</body>
</html>
"""



# ---------------------------------------------------------------- App-Fenster
def find_app_browser():
    """Sucht Edge oder Chrome: die starten die Seite ohne Adressleiste (wie eine App)."""
    pf = [os.environ.get("ProgramFiles", r"C:\Program Files"),
          os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
          os.environ.get("LOCALAPPDATA", "")]
    names = [
        r"Microsoft\Edge\Application\msedge.exe",
        r"Google\Chrome\Application\chrome.exe",
    ]
    for base in pf:
        for n in names:
            p = os.path.join(base, n)
            if base and os.path.exists(p):
                return p
    return None


def open_app(url, srv):
    """Öffnet die Seite als App-Fenster. Schließt man das Fenster, beendet sich das Programm."""
    exe = find_app_browser()
    if not exe:
        webbrowser.open(url)
        return
    profile = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_profile")
    try:
        proc = subprocess.Popen([exe, "--app=" + url, "--window-size=1280,860",
                                 "--user-data-dir=" + profile, "--no-first-run"])
    except OSError:
        webbrowser.open(url)
        return
    proc.wait()
    srv.shutdown()


# ---------------------------------------------------------------- Start
def main():
    url = "http://localhost:%d/" % PORT
    try:
        srv = Srv(("127.0.0.1", PORT), H)
    except OSError:
        print("Port %d ist schon belegt – der Server läuft wahrscheinlich schon." % PORT)
        print("Ich öffne nur den Browser.")
        webbrowser.open(url)
        return
    threading.Timer(0.8, open_app, [url, srv]).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
