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


def recent_remove(user_id):
    uid = str(user_id or "")
    if not uid.isdigit():
        return
    with dblock:
        DB.execute("delete from recent where user_id=?", (uid,))
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
                elif d.get("removeUserId"):
                    recent_remove(d.get("removeUserId"))
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

/* ---------- UI polish ---------- */
html{--acc:#5865f2;--txt:#20243a;--sub:#70758a;--glass:rgba(250,251,255,.82);--glass2:rgba(255,255,255,.62);--solid:rgba(255,255,255,.74);--rim:rgba(255,255,255,.94);--line:rgba(38,45,75,.1);--bg:#e9edf7;--sh:0 24px 70px rgba(41,52,99,.18)}
html.dark{--txt:#f4f5ff;--sub:#a5aac0;--glass:rgba(25,28,43,.88);--glass2:rgba(37,41,60,.72);--solid:rgba(255,255,255,.075);--rim:rgba(255,255,255,.12);--line:rgba(255,255,255,.1);--bg:#111321;--sh:0 28px 80px rgba(0,0,0,.5)}
body{height:100dvh}
.wall .b1{background:#8798ff}.wall .b2{background:#ffa4cb}.wall .b3{background:#76d9c0}
.menubar{height:36px;padding-inline:clamp(10px,2vw,28px);background:var(--glass);border-bottom-color:var(--line)}
.mb{padding:4px 11px}.mb.b{color:var(--acc)}
.win{top:52px;width:min(1260px,calc(100vw - 48px));height:calc(100dvh - 142px);min-height:0;border-radius:24px;background:var(--glass);box-shadow:var(--sh),inset 0 1px 0 var(--rim)}
.side{width:242px;padding:20px 14px 16px;background:rgba(245,247,253,.55)}
html.dark .side{background:rgba(16,18,30,.35)}
.lights{padding-bottom:22px}.lights b{width:12px;height:12px}
.ni{height:40px;border-radius:11px}.nav{gap:4px}
#sel{height:40px;border-radius:11px;background:color-mix(in srgb,var(--acc) 13%,transparent);box-shadow:inset 3px 0 var(--acc)}
.ni .ico{border-radius:8px}
.tb{height:68px;padding-inline:clamp(14px,2.4vw,30px);gap:14px;background:rgba(255,255,255,.12)}
.tb h2{font-size:15px;letter-spacing:-.01em}
.pill{width:min(440px,58%);height:42px;padding-left:15px;background:var(--solid);box-shadow:0 3px 12px rgba(32,40,80,.04)}
.go{height:32px;padding-inline:18px;border-radius:10px;background:var(--acc);box-shadow:0 5px 12px color-mix(in srgb,var(--acc) 26%,transparent)}
.content{padding:clamp(20px,3.2vw,42px);scrollbar-gutter:stable}
.hero{position:relative;isolation:isolate;overflow:hidden;text-align:left;padding:clamp(25px,4vw,48px);margin:0 0 30px;border:1px solid color-mix(in srgb,var(--acc) 16%,var(--line));border-radius:26px;background:linear-gradient(115deg,color-mix(in srgb,var(--acc) 12%,var(--glass2)),var(--glass2) 65%);box-shadow:inset 0 1px 0 var(--rim)}
.hero::after{content:"";position:absolute;z-index:-1;width:260px;height:260px;right:-65px;top:-115px;border-radius:50%;border:1px solid color-mix(in srgb,var(--acc) 23%,transparent);box-shadow:0 0 0 28px color-mix(in srgb,var(--acc) 4%,transparent),0 0 0 58px color-mix(in srgb,var(--acc) 3%,transparent)}
.logo{width:52px;height:52px;margin:0 0 18px;border-radius:16px;background:linear-gradient(145deg,var(--acc),color-mix(in srgb,var(--acc) 62%,#b67aff));box-shadow:0 10px 24px color-mix(in srgb,var(--acc) 25%,transparent)}
.logo svg{width:27px;height:27px}
.hero h1{font-size:clamp(28px,4vw,42px);letter-spacing:-.045em;line-height:1.05}
.hero p{font-size:14px;max-width:540px;margin-top:10px;line-height:1.65}
.quick{justify-content:flex-start;gap:9px;margin-top:22px}
.qb{padding:8px 14px;border-radius:11px;font-size:12.5px;background:var(--solid);box-shadow:none}
.qb:hover{box-shadow:0 7px 18px color-mix(in srgb,var(--acc) 18%,transparent)}
.sec{margin:28px 2px 13px;color:var(--txt);font-size:12px;letter-spacing:.04em}
.sec>span{color:var(--sub)}
.grid{gap:14px}.grid.w{grid-template-columns:repeat(auto-fill,minmax(min(240px,100%),1fr))}
.card{border-radius:17px;background:var(--glass2);box-shadow:inset 0 1px 0 var(--rim),0 4px 18px rgba(20,30,60,.045)}
.card:hover{box-shadow:inset 0 1px 0 var(--rim),0 14px 28px rgba(30,40,80,.12)}
.fcard{padding:19px;min-height:150px}
.fcard .ico{width:38px;height:38px;border-radius:12px;margin-bottom:16px}
.fcard b{font-size:14px}.fcard p{line-height:1.55;margin-top:6px}
.rchip{border-radius:13px;padding:7px 13px 7px 7px;background:var(--glass2);box-shadow:none}
.rchip img,.ph2{width:34px;height:34px}
.prof{gap:clamp(18px,3vw,34px);padding:clamp(18px,3vw,30px)}
.avbox{width:min(270px,34%);border-radius:20px}
.pinfo h1{font-size:clamp(25px,3vw,34px)}
.stat{border-radius:13px}.stat b{font-size:20px}
.btn{border-radius:11px;box-shadow:none}.btn.pri{box-shadow:0 6px 14px color-mix(in srgb,var(--acc) 24%,transparent)}
.sheet{border-radius:22px}.dock{bottom:14px;gap:7px;padding:9px 12px;border-radius:20px;background:var(--glass);box-shadow:0 12px 36px rgba(25,35,70,.16),inset 0 1px 0 var(--rim)}
.di{border-radius:16px;box-shadow:0 4px 10px rgba(0,0,0,.14),inset 0 1px 0 rgba(255,255,255,.45)}
.iconbtn:focus-visible,.go:focus-visible,.btn:focus-visible,.qb:focus-visible,.ni:focus-visible,.di:focus-visible,.lnkbtn:focus-visible{outline:3px solid color-mix(in srgb,var(--acc) 60%,transparent);outline-offset:2px}
button{ -webkit-tap-highlight-color:transparent }

@media(max-width:760px){
 html{--base:38px}
 .menubar{height:34px}.mb:not(:first-child):not([data-m="help"]){display:none}
 .win{top:42px!important;left:8px!important;width:calc(100vw - 16px)!important;height:calc(100dvh - 112px)!important;border-radius:20px}
 .side{background:var(--glass);box-shadow:8px 0 24px rgba(0,0,0,.08)}
 .tb{height:62px;padding-inline:12px;gap:8px}.tb h2{display:block;font-size:13px;max-width:92px}
 .pill{width:auto;flex:1;height:40px;padding-left:10px;gap:6px}.pill input{font-size:12px}.go{padding-inline:12px;font-size:12px}
 .content{padding:16px 14px 24px}
 .hero{padding:24px 20px;border-radius:20px;margin-bottom:24px}.hero::after{right:-150px}
 .logo{width:44px;height:44px;margin-bottom:15px;border-radius:14px}
 .hero p{max-width:90%;font-size:13px}
 .quick{gap:7px;margin-top:17px}.qb{padding:7px 11px}
 .sec{margin-top:22px}
 .grid{grid-template-columns:repeat(auto-fill,minmax(145px,1fr));gap:10px}
 .grid.w{grid-template-columns:1fr}
 .fcard{min-height:unset;padding:16px}.fcard .ico{margin-bottom:10px}
 .prof{padding:14px;gap:17px}.avbox{width:130px;max-width:38%;border-radius:16px}.pinfo{min-width:min(180px,55%)}
 .dock{gap:5px;padding:6px 8px;bottom:8px;max-width:calc(100vw - 12px);overflow-x:auto}
 .di{border-radius:12px}.di .tip{display:none}.dsep{height:30px;margin:0}
 .sh-head{padding:14px}.sh-body{padding:14px}
}
@media(max-width:430px){
 .menubar{padding-inline:6px}.mb{padding-inline:7px;font-size:11px}.mbr{gap:3px;font-size:11px}
 #clock{display:none}
 .win{height:calc(100dvh - 100px)!important}
 .tb{gap:4px}.tb h2{max-width:55px;font-size:12px}.iconbtn{width:29px;height:29px}
 .pill{gap:4px}.pill input{font-size:11px}.go{padding-inline:9px}
 .content{padding:12px 10px 18px}
 .hero{padding:21px 17px}.hero h1{font-size:29px}.hero::after{width:190px;height:190px;right:-125px;top:-90px}
 .qb{font-size:11px;padding:7px 9px}
 .dock{gap:4px}.di{width:32px;height:32px}.dsep{height:25px}
 .prof{display:block}.avbox{width:150px;max-width:100%;margin:0 auto 18px}.pinfo{min-width:0}.pinfo h1{font-size:25px}
 .stats{grid-template-columns:repeat(2,minmax(0,1fr))}
}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{scroll-behavior:auto!important;animation-duration:.01ms!important;animation-iteration-count:1!important;transition-duration:.01ms!important}}

/* ---------- Dark Roblox profile dashboard, inspired by the reference ---------- */
html{--acc:#168df5;--txt:#f2f3fb;--sub:#a1a4b9;--glass:rgba(33,34,54,.96);--glass2:rgba(49,50,72,.88);--solid:rgba(89,91,117,.34);--rim:rgba(255,255,255,.11);--line:rgba(223,225,255,.18);--bg:#191a2b;--sh:0 24px 64px rgba(0,0,0,.46);color-scheme:dark;background:#191a2b}
html.dark{--txt:#f2f3fb;--sub:#a1a4b9;--glass:rgba(33,34,54,.96);--glass2:rgba(49,50,72,.88);--solid:rgba(89,91,117,.34);--rim:rgba(255,255,255,.11);--line:rgba(223,225,255,.18);--bg:#191a2b;--sh:0 24px 64px rgba(0,0,0,.46);color-scheme:dark}
html:not(.dark){--txt:#25283a;--sub:#686d80;--glass:rgba(244,246,253,.97);--glass2:rgba(255,255,255,.88);--solid:rgba(92,103,139,.12);--rim:rgba(255,255,255,.9);--line:rgba(51,61,99,.15);--bg:#e8ebf4;--sh:0 24px 64px rgba(34,43,77,.18);color-scheme:light}
html:not(.dark) body{background:radial-gradient(ellipse at 90% 100%,rgba(255,172,86,.18),transparent 26%),radial-gradient(ellipse at 28% 110%,rgba(156,91,255,.16),transparent 30%),#e8ebf4;color:var(--txt)}
html:not(.dark) .side{background:linear-gradient(180deg,rgba(77,80,111,.78),rgba(48,50,75,.94));color:#fff}
html:not(.dark) .profile-cover{background:linear-gradient(110deg,rgba(74,66,91,.83),rgba(43,52,79,.94) 68%,rgba(31,83,132,.7));color:#f2f3fb}
html:not(.dark) .overview-panel,html:not(.dark) .avatar-panel,html:not(.dark) .wearing-panel{background:linear-gradient(145deg,rgba(54,54,76,.9),rgba(39,40,59,.94));color:#f2f3fb}
html:not(.dark) .overview-outfit{color:#f2f3fb}

body{height:100dvh;background:radial-gradient(ellipse at 90% 100%,rgba(255,172,86,.13),transparent 26%),radial-gradient(ellipse at 28% 110%,rgba(156,91,255,.22),transparent 30%),#191a2b;color:var(--txt)}
body::after{opacity:.025}.wall{display:none}.menubar{display:none}.dock{display:none}
.win,.win.zoom{min-height:0;border-radius:23px;background:rgba(27,28,45,.52);backdrop-filter:blur(40px) saturate(170%);-webkit-backdrop-filter:blur(40px) saturate(170%);border:1px solid rgba(255,255,255,.21);box-shadow:0 0 0 1px rgba(0,0,0,.2),var(--sh);animation:none}
.win::before{display:none}.side{width:178px;padding:12px 9px 12px;background:linear-gradient(180deg,rgba(88,88,119,.51),rgba(43,43,65,.78));border-right:1px solid rgba(255,255,255,.18)}
.lights{height:30px;padding:2px 5px 14px;gap:7px}.lights b{width:11px;height:11px}
.nav{gap:3px;overflow-y:auto;overflow-x:hidden}.navlabel{position:relative;z-index:1;margin:2px 6px 4px;color:#a5a5b8;font-size:10px;font-weight:700;letter-spacing:.09em;text-transform:uppercase}.navlabel-last{margin-top:17px}
.ni{height:34px;padding:0 8px;gap:9px;border-radius:9px;font-size:12px;color:#e9e9f3}.ni .ico{width:20px;height:20px;border-radius:7px}.ni.act{background:linear-gradient(95deg,rgba(32,144,255,.85),rgba(38,133,231,.42));box-shadow:0 4px 12px rgba(17,126,240,.2)}
#sel{display:none}.navshortcuts{position:relative;z-index:1;display:grid;gap:2px;padding:0 3px}.navshortcuts button{display:flex;align-items:center;gap:8px;height:26px;padding:0 6px;border:0;border-radius:7px;background:transparent;color:#e0e1ec;text-align:left;font:500 11px Inter,system-ui,sans-serif;cursor:pointer}.navshortcuts button:hover{background:rgba(255,255,255,.08)}.navshortcuts i{width:7px;height:7px;border-radius:50%;background:#23d67a;box-shadow:0 0 9px currentColor}.navshortcuts button:nth-child(2) i{background:#248cff}.navshortcuts button:nth-child(3) i{background:#ffab32}
.sidefoot{padding:12px 6px 5px;gap:7px;flex-wrap:wrap}.sidefoot::before{content:'Accent color';width:100%;color:#aaaabc;font-size:10px;font-weight:600;margin-bottom:1px}.sw{width:13px;height:13px;border-width:1px}
.main{background:transparent}.tb{height:42px;flex:none;padding:0 14px;border-bottom:1px solid rgba(255,255,255,.12);background:rgba(24,25,42,.36);position:relative;gap:8px}
#sbBtn{display:none}#ttl{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);font-size:11px;line-height:1.15;text-align:center;max-width:34%;color:#f6f5ff}.pill{margin-left:auto;width:228px;height:30px;padding:0 5px 0 11px;border:1px solid rgba(255,255,255,.23);background:rgba(255,255,255,.07);box-shadow:none;color:#b9bbcf;gap:6px}.pill input{font-size:11px}.pill input::placeholder{color:#a5a7bb}.pill>svg{width:13px;height:13px;flex:none}.go{width:25px;height:25px;padding:0;border-radius:50%;font-size:0;position:relative;box-shadow:0 3px 10px rgba(20,140,255,.35)}.go::after{content:'↑';font-size:17px;line-height:1;font-weight:600}
.content{padding:12px 18px 18px;scrollbar-gutter:stable;scrollbar-color:rgba(202,204,230,.28) transparent}.tab.show{animation:fade .2s}.profile-dashboard{display:flex;flex-direction:column;gap:12px;max-width:1440px;margin:0 auto}
.profile-cover{position:relative;display:grid;grid-template-columns:78px minmax(0,1fr) auto;grid-template-areas:'avatar identity actions' 'avatar facts actions' 'description description description' 'history history history';column-gap:16px;row-gap:5px;padding:15px 17px 13px;border-radius:17px;background:linear-gradient(110deg,rgba(74,66,91,.83),rgba(43,52,79,.94) 68%,rgba(31,83,132,.7));border:1px solid rgba(255,255,255,.29);box-shadow:inset 0 1px rgba(255,255,255,.16),0 8px 24px rgba(0,0,0,.18)}
.profile-cover .avbox{grid-area:avatar;width:78px;height:78px;max-width:none;align-self:center;border-radius:17px;background:rgba(255,255,255,.88);border:1px solid rgba(255,255,255,.35);box-shadow:0 8px 22px rgba(0,0,0,.22)}.profile-cover .avbox img{object-fit:cover}.profile-identity{grid-area:identity;align-self:end;min-width:0}.eyebrow{font-size:8px;letter-spacing:.14em;color:#c1c4d8;font-weight:700;margin-bottom:3px}.profile-identity h1{font-size:clamp(20px,2.5vw,28px);font-weight:800;letter-spacing:-.035em;line-height:1.06;white-space:nowrap;text-overflow:ellipsis;overflow:hidden}.profile-handle{font-size:11px;margin-top:4px;gap:7px}.profile-handle .chip{font-size:9px}.profile-facts{grid-area:facts;display:flex;align-items:center;gap:7px;flex-wrap:wrap;margin-top:4px}.fact-pill{height:23px;padding:0 9px;display:inline-flex;align-items:center;gap:6px;border-radius:99px;border:1px solid rgba(255,255,255,.22);background:rgba(24,25,42,.27);color:#e0dfec;font-size:10px;white-space:nowrap}.fact-pill i{width:7px;height:7px;border-radius:50%;background:#ff4c8b;box-shadow:0 0 8px currentColor}.fact-pill b{font-size:10px;color:#fff}.fact-green i{background:#43de88}.fact-blue i{background:#278dff}.fact-violet i{background:#bd6cff}.profile-actions{grid-area:actions;display:flex;flex-direction:column;justify-content:center;gap:6px;min-width:126px}.profile-actions .btn{height:29px;justify-content:center;padding:0 11px;border-radius:99px;font-size:10px}.profile-actions .btn.pri{background:linear-gradient(#42a7ff,#1684ed);box-shadow:0 4px 12px rgba(27,138,246,.3)}.profile-presence{display:none}.profile-description{grid-area:description;max-height:34px;overflow:hidden;color:#c4c5d4;font-size:10px;line-height:1.5}.profile-history{grid-area:history}.profile-history:empty{display:none}
.profile-tabs{display:flex;align-items:center;gap:5px;min-height:32px;padding:3px 6px;border-radius:99px;background:rgba(49,49,67,.78);border:1px solid rgba(255,255,255,.2);overflow-x:auto;scrollbar-width:none}.profile-tabs::-webkit-scrollbar{display:none}.profile-tab{display:inline-flex;align-items:center;gap:5px;padding:5px 10px;border:1px solid transparent;border-radius:99px;background:transparent;color:#e2e2ee;font:600 10px Inter,system-ui,sans-serif;white-space:nowrap;cursor:pointer}.profile-tab svg{color:#d4d4e3}.profile-tab span{padding:1px 5px;border-radius:99px;background:rgba(255,255,255,.13);color:#d0d0df;font-size:8px}.profile-tab.active,.profile-tab:hover{background:rgba(255,255,255,.17);border-color:rgba(255,255,255,.28);box-shadow:inset 0 1px rgba(255,255,255,.12)}
.overview-panel,.avatar-panel,.wearing-panel{border-radius:17px;background:linear-gradient(145deg,rgba(54,54,76,.9),rgba(39,40,59,.94));border:1px solid rgba(255,255,255,.23);box-shadow:inset 0 1px rgba(255,255,255,.08),0 8px 18px rgba(0,0,0,.14)}.overview-panel{padding:10px 11px 11px;min-width:0}.section-heading{min-height:24px;display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:6px}.section-heading h2{display:flex;align-items:center;gap:7px;min-width:0;font-size:12px;font-weight:750;letter-spacing:-.01em}.section-icon{width:19px;height:19px;border-radius:7px;display:grid;place-items:center;color:#fff;flex:none}.section-icon.pink{background:#fa4777}.section-icon.cyan{background:#21bed0}.heading-count{font-size:10px;color:#afb0c2;font-weight:500}.text-action{display:inline-flex;align-items:center;gap:4px;background:none;border:0;color:#aeb9cb;font:600 9px Inter,system-ui,sans-serif;white-space:nowrap;cursor:pointer}.text-action:hover{color:#fff}.outfit-carousel{display:flex;gap:9px;overflow-x:auto;scrollbar-width:thin;scrollbar-color:rgba(211,213,230,.3) transparent;padding:0 0 3px}.overview-outfit{width:132px;min-width:132px;overflow:hidden;border-radius:15px;background:linear-gradient(140deg,rgba(123,121,143,.5),rgba(75,77,103,.72));border:1px solid rgba(255,255,255,.23);cursor:pointer;animation:rise .3s backwards;animation-delay:calc(var(--i)*25ms)}.overview-outfit:hover{transform:translateY(-2px);border-color:rgba(255,255,255,.42)}.outfit-visual{height:116px;position:relative;display:grid;place-items:center;overflow:hidden;background:radial-gradient(ellipse at 50% 85%,rgba(47,151,255,.2),transparent 50%),rgba(255,255,255,.035)}.outfit-visual img{width:100%;height:100%;object-fit:contain;transition:transform .25s}.overview-outfit:hover .outfit-visual img{transform:scale(1.05)}.outfit-label{position:absolute;top:8px;left:8px;padding:3px 7px;border-radius:99px;background:rgba(223,83,145,.69);color:#ffe9f5;font-size:7px;font-weight:800;letter-spacing:.05em}.overview-card-copy{padding:7px 9px 8px}.overview-card-copy>b{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:10px}.overview-card-id{display:flex;align-items:center;justify-content:space-between;gap:5px;margin-top:4px}.overview-card-id span{max-width:calc(100% - 24px);padding:2px 6px;overflow:hidden;text-overflow:ellipsis;border-radius:99px;background:rgba(38,136,226,.2);color:#a9d3ff;font:500 8px 'JetBrains Mono',monospace}.overview-card-id button{width:20px;height:20px;border:1px solid rgba(255,255,255,.17);border-radius:50%;display:grid;place-items:center;background:rgba(255,255,255,.08);color:#d9d9e5;cursor:pointer}.overview-state{min-height:80px;display:flex;align-items:center;justify-content:center;gap:8px;color:#adafc1;font-size:10px}.overview-state .spinner{width:17px;height:17px;border-width:2px}
.overview-lower{display:grid;grid-template-columns:minmax(190px,.72fr) minmax(0,1.6fr);gap:11px;min-height:230px}.avatar-panel,.wearing-panel{min-width:0;padding:10px 11px}.avatar-panel{display:flex;flex-direction:column;min-height:220px}.live-dot{width:8px;height:8px;border-radius:50%;background:#38df80;box-shadow:0 0 9px #38df80}.live-label{padding:3px 7px;border-radius:99px;background:rgba(180,94,232,.28);color:#dfaaff;font-size:8px;font-weight:800;letter-spacing:.06em}.avatar-stage{flex:1;min-height:155px;position:relative;display:grid;place-items:center;overflow:hidden;border-radius:12px;background:radial-gradient(ellipse at 50% 98%,rgba(150,99,241,.34),transparent 48%),linear-gradient(180deg,rgba(255,255,255,.025),rgba(255,255,255,.04))}.avatar-stage img{width:100%;height:100%;max-height:220px;object-fit:contain}.avatar-caption{padding:5px 1px 0;color:#a1a2b5;font-size:9px}.wearing-panel{display:flex;flex-direction:column}.wearing-list{overflow:auto;min-height:0;scrollbar-width:thin;scrollbar-color:rgba(211,213,230,.2) transparent}.wearing-row{min-height:44px;display:flex;align-items:center;gap:9px;padding:5px 7px;border-bottom:1px solid rgba(255,255,255,.08)}.wearing-row:last-child{border-bottom:0}.wearing-thumb{width:32px;height:32px;border-radius:9px;overflow:hidden;background:rgba(255,255,255,.09);flex:none}.wearing-thumb img{width:100%;height:100%;object-fit:cover}.wearing-copy{min-width:0;flex:1}.wearing-copy b{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:10px;font-weight:650}.wearing-copy span{display:block;color:#9fa2b8;font-size:8px;margin-top:2px}.wearing-id{max-width:96px;padding:4px 7px;overflow:hidden;text-overflow:ellipsis;border:1px solid rgba(255,255,255,.12);border-radius:99px;background:rgba(54,148,230,.17);color:#add5ff;font:500 8px 'JetBrains Mono',monospace;white-space:nowrap;cursor:pointer}.wearing-id:hover{background:rgba(42,145,245,.35)}
/* Reuse this dark glass language in detail tabs and the outfit modal. */
.card{color:var(--txt)}.state{color:#a1a4b9}.btn{background:rgba(255,255,255,.09);border-color:rgba(255,255,255,.18)}.btn.pri{background:#168df5}.chip,.tag{background:rgba(255,255,255,.09);border-color:rgba(255,255,255,.14);color:#d3d6e7}.ph{background:linear-gradient(110deg,rgba(255,255,255,.04) 30%,rgba(255,255,255,.12) 45%,rgba(255,255,255,.04) 60%);background-size:200% 100%;animation:shimmer 1.6s linear infinite}@keyframes shimmer{to{background-position:-200% 0}}
@media(max-width:760px){
 .win,.win.zoom{top:0!important;left:0!important;width:100vw!important;height:100dvh!important;border-radius:0}
 .side{position:relative;width:150px;min-width:150px;padding:9px 7px 10px;box-shadow:none}.navlabel{margin-left:5px}.ni{height:32px;padding:0 6px;font-size:10px}.ni .ico{width:18px;height:18px}.navshortcuts button{font-size:10px}
 .tb{height:40px;padding:0 9px}.pill{width:min(210px,48vw)}.content{padding:10px 11px 14px}
 .profile-cover{grid-template-columns:64px minmax(0,1fr) auto;column-gap:10px;padding:12px}.profile-cover .avbox{width:64px;height:64px;border-radius:13px}.profile-actions{min-width:105px}.profile-actions .btn{font-size:9px;padding-inline:7px}.profile-facts{gap:4px}.fact-pill{font-size:8px;padding-inline:6px;height:20px}.profile-tabs{gap:2px}.profile-tab{padding:5px 7px;font-size:9px}.overview-outfit{width:120px;min-width:120px}.outfit-visual{height:100px}.overview-lower{grid-template-columns:minmax(150px,.7fr) minmax(0,1.3fr);min-height:210px}.wearing-row{gap:6px;padding-inline:4px}.wearing-id{max-width:72px}
}
@media(max-width:520px){
 .side{width:62px;min-width:62px;padding:9px 5px}.lights{gap:5px;padding-left:2px}.lights b{width:9px;height:9px}.navlabel,.navshortcuts,.sidefoot::before{display:none}.nav{align-items:center}.ni{width:42px;justify-content:center;padding:0}.ni>span:not(.ico):not(.bd){display:none}.ni .bd{display:none!important}.sidefoot{justify-content:center;padding:8px 0;gap:5px}.sw{width:10px;height:10px}
 .tb{height:40px}.tb h2{display:none}.pill{width:190px;max-width:calc(100vw - 90px)}.content{padding:8px}
 .profile-dashboard{gap:8px}.profile-cover{grid-template-columns:54px minmax(0,1fr);grid-template-areas:'avatar identity' 'facts facts' 'actions actions' 'description description' 'history history';gap:6px;padding:10px}.profile-cover .avbox{width:54px;height:54px}.profile-identity h1{font-size:19px}.profile-actions{flex-direction:row;min-width:0}.profile-actions .btn{flex:1;height:27px}.profile-facts{margin-top:2px}.fact-pill{height:19px;font-size:8px}.profile-tabs{min-height:30px}.profile-tab{font-size:8px;padding:4px 6px}.profile-tab svg{display:none}
 .overview-panel,.avatar-panel,.wearing-panel{padding:8px}.overview-panel{min-height:185px}.overview-outfit{width:112px;min-width:112px}.outfit-visual{height:96px}.overview-lower{grid-template-columns:1fr;min-height:0}.avatar-panel{min-height:215px}.avatar-stage{min-height:160px}.wearing-panel{max-height:265px}.wearing-row{min-height:40px}
}

/* macOS-inspired Liquid Glass finish: floating app window, glass menu bar and Dock */
html{--glass:rgba(31,33,52,.52);--glass2:rgba(47,49,72,.46);--solid:rgba(105,110,145,.2);--rim:rgba(255,255,255,.27);--line:rgba(226,230,255,.18);--sh:0 28px 80px rgba(0,0,0,.46)}
html.dark{--glass:rgba(31,33,52,.52);--glass2:rgba(47,49,72,.46);--solid:rgba(105,110,145,.2);--rim:rgba(255,255,255,.27);--line:rgba(226,230,255,.18);--sh:0 28px 80px rgba(0,0,0,.46)}
body{background:radial-gradient(ellipse at 12% 33%,rgba(68,131,239,.34),transparent 34%),radial-gradient(ellipse at 87% 42%,rgba(230,103,166,.2),transparent 30%),radial-gradient(ellipse at 55% 105%,rgba(49,208,171,.19),transparent 34%),#111321}
.wall{display:block}.wall i{filter:blur(115px);opacity:.46}.wall .b1{background:#548cff}.wall .b2{background:#e989be}.wall .b3{background:#46d7b2}
body::after{opacity:.035}
.menubar{display:flex;top:0;height:30px;padding:0 14px;gap:2px;background:rgba(26,28,43,.42);backdrop-filter:blur(38px) saturate(185%);-webkit-backdrop-filter:blur(38px) saturate(185%);border-bottom:1px solid rgba(255,255,255,.14);box-shadow:0 1px rgba(255,255,255,.035);color:#f4f5ff}
.mb{padding:3px 9px;font-size:11px}.mb:first-child{font-weight:650}.mb:hover,.mb.on{background:rgba(255,255,255,.12)}.mbr{gap:10px;font-size:10px;color:#e4e5f0}.menubar .iconbtn{width:24px;height:24px;color:#d7d9e8}
.menu{top:32px;background:rgba(43,45,66,.66);backdrop-filter:blur(42px) saturate(190%);-webkit-backdrop-filter:blur(42px) saturate(190%);border-color:rgba(255,255,255,.19);box-shadow:0 18px 48px rgba(0,0,0,.35),inset 0 1px rgba(255,255,255,.16)}
.win,.win.zoom{width:min(1320px,calc(100vw - 40px))!important;height:calc(100dvh - 132px)!important;min-height:410px;border-radius:23px;background:rgba(28,30,49,.48);backdrop-filter:blur(48px) saturate(185%);-webkit-backdrop-filter:blur(48px) saturate(185%);border:1px solid rgba(255,255,255,.25);box-shadow:0 30px 100px rgba(0,0,0,.48),inset 0 1px 0 rgba(255,255,255,.23);animation:winIn .55s cubic-bezier(.2,.9,.3,1.1) backwards}
.win.zoom{left:12px!important;top:38px!important;width:calc(100vw - 24px)!important;height:calc(100dvh - 86px)!important}
.win::before{display:block;opacity:.58;background:linear-gradient(155deg,rgba(255,255,255,.78),rgba(255,255,255,0) 27%,rgba(255,255,255,0) 72%,rgba(255,255,255,.3))}
.side{background:linear-gradient(165deg,rgba(105,108,143,.34),rgba(52,54,78,.3));backdrop-filter:blur(34px) saturate(160%);-webkit-backdrop-filter:blur(34px) saturate(160%);border-right:1px solid rgba(255,255,255,.16)}
.main{background:rgba(25,27,44,.13)}.tb{background:rgba(21,23,39,.22);backdrop-filter:blur(25px) saturate(155%);-webkit-backdrop-filter:blur(25px) saturate(155%);border-bottom-color:rgba(255,255,255,.15)}
.pill{background:rgba(255,255,255,.105);backdrop-filter:blur(24px) saturate(170%);-webkit-backdrop-filter:blur(24px) saturate(170%);border-color:rgba(255,255,255,.2)}
.profile-cover{background:linear-gradient(112deg,rgba(97,88,126,.53),rgba(47,62,94,.54) 68%,rgba(39,105,153,.43));backdrop-filter:blur(34px) saturate(165%);-webkit-backdrop-filter:blur(34px) saturate(165%);border-color:rgba(255,255,255,.29);box-shadow:inset 0 1px rgba(255,255,255,.23),0 10px 32px rgba(0,0,0,.16)}
.overview-panel,.avatar-panel,.wearing-panel{background:linear-gradient(145deg,rgba(87,88,117,.43),rgba(42,44,67,.54));backdrop-filter:blur(30px) saturate(155%);-webkit-backdrop-filter:blur(30px) saturate(155%);border-color:rgba(255,255,255,.2);box-shadow:inset 0 1px rgba(255,255,255,.14),0 10px 30px rgba(0,0,0,.12)}
.overview-outfit{background:linear-gradient(145deg,rgba(170,169,193,.27),rgba(88,91,123,.35));backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);border-color:rgba(255,255,255,.23)}
.profile-tabs{background:rgba(62,64,88,.37);backdrop-filter:blur(28px) saturate(175%);-webkit-backdrop-filter:blur(28px) saturate(175%);border-color:rgba(255,255,255,.2)}
.dock{display:flex;bottom:10px;padding:8px 10px;gap:7px;border-radius:23px;background:rgba(244,246,255,.16);backdrop-filter:blur(42px) saturate(190%);-webkit-backdrop-filter:blur(42px) saturate(190%);border-color:rgba(255,255,255,.28);box-shadow:0 12px 38px rgba(0,0,0,.3),inset 0 1px 0 rgba(255,255,255,.32)}
.di{box-shadow:0 8px 18px rgba(0,0,0,.27),inset 0 1px 0 rgba(255,255,255,.56)}
#toolbarTheme{color:#d8daeb;background:rgba(255,255,255,.045)}#toolbarTheme:hover{background:rgba(255,255,255,.15)}
.toast,.sheet,.alert{background:rgba(38,40,60,.7);backdrop-filter:blur(42px) saturate(185%);-webkit-backdrop-filter:blur(42px) saturate(185%);border-color:rgba(255,255,255,.2)}
html:not(.dark){--glass:rgba(243,246,255,.57);--glass2:rgba(243,246,255,.38);--solid:rgba(255,255,255,.25);--rim:rgba(255,255,255,.65);--line:rgba(49,55,89,.17)}
html:not(.dark) body{background:radial-gradient(ellipse at 12% 33%,rgba(68,131,239,.29),transparent 34%),radial-gradient(ellipse at 87% 42%,rgba(230,103,166,.18),transparent 30%),radial-gradient(ellipse at 55% 105%,rgba(49,208,171,.17),transparent 34%),#dce3f0}
/* Smooth window motion and a subtle animated pointer glow */
.win{transition:opacity .28s ease,transform .42s cubic-bezier(.2,.8,.2,1),left .42s cubic-bezier(.2,.8,.2,1),top .42s cubic-bezier(.2,.8,.2,1),width .42s cubic-bezier(.2,.8,.2,1),height .42s cubic-bezier(.2,.8,.2,1),border-radius .35s ease;will-change:left,top,width,height,transform}
.win.drag{transition:none!important;will-change:left,top}.tb{cursor:grab}.win.drag .tb{cursor:grabbing}.tb input,.tb button{cursor:auto}
.card,.ni,.di,.btn,.iconbtn,.profile-tab,.overview-outfit,.qb,.sw,.fact-pill{transition:transform .32s cubic-bezier(.2,.8,.2,1),width .18s ease,height .18s ease,background-color .3s ease,border-color .3s ease,box-shadow .32s ease,color .25s ease}
.cursor-aura{position:fixed;z-index:199;left:0;top:0;width:240px;height:240px;border-radius:50%;pointer-events:none;opacity:0;background:radial-gradient(circle,rgba(112,169,255,.16),rgba(129,118,255,.08) 30%,rgba(110,171,255,.025) 54%,transparent 72%);filter:blur(3px);mix-blend-mode:screen;transform:translate(-50%,-50%);transition:opacity .25s ease;will-change:left,top}
.cursor-aura.visible{opacity:1}.cursor-aura::after{content:"";position:absolute;left:50%;top:50%;width:34px;height:34px;transform:translate(-50%,-50%);border:1px solid rgba(211,230,255,.5);border-radius:50%;box-shadow:0 0 18px rgba(112,169,255,.27),inset 0 0 8px rgba(255,255,255,.12)}
.cursor-ripple{position:fixed;z-index:201;left:0;top:0;width:20px;height:20px;border:1.5px solid rgba(189,220,255,.92);border-radius:50%;pointer-events:none;box-shadow:0 0 18px rgba(74,157,255,.54);transform:translate(-50%,-50%) scale(.35);animation:cursorRipple .62s cubic-bezier(.12,.7,.3,1) forwards}
@keyframes cursorRipple{to{opacity:0;transform:translate(-50%,-50%) scale(3.4)}}
html:not(.dark) body{background:radial-gradient(ellipse at 10% 27%,rgba(104,151,246,.36),transparent 34%),radial-gradient(ellipse at 88% 32%,rgba(255,160,203,.28),transparent 32%),radial-gradient(ellipse at 57% 105%,rgba(104,224,197,.24),transparent 36%),#e8edf7}
html:not(.dark) .menubar{background:rgba(248,250,255,.48);border-color:rgba(49,59,93,.14);color:#24293c;box-shadow:0 1px rgba(255,255,255,.65)}
html:not(.dark) .mb{color:#2d3247}html:not(.dark) .mbr,html:not(.dark) .menubar .iconbtn{color:#373c50}
html:not(.dark) .menu{background:rgba(247,249,255,.74);border-color:rgba(56,65,100,.18);color:#262b3f}
html:not(.dark) .win{background:rgba(242,246,255,.46);border-color:rgba(255,255,255,.75);box-shadow:0 30px 100px rgba(46,57,95,.25),inset 0 1px 0 rgba(255,255,255,.88)}
html:not(.dark) .win::before{opacity:.85}
html:not(.dark) .side{background:linear-gradient(160deg,rgba(255,255,255,.49),rgba(230,236,249,.32));border-right-color:rgba(59,69,103,.14);color:#30364a}
html:not(.dark) .navlabel,html:not(.dark) .sidefoot::before{color:#727a90}
html:not(.dark) .ni,html:not(.dark) .navshortcuts button{color:#34394d}
html:not(.dark) .ni.act{color:#fff;background:linear-gradient(100deg,rgba(31,133,243,.88),rgba(88,158,248,.57))}
html:not(.dark) .navshortcuts button:hover{background:rgba(69,89,139,.1)}
html:not(.dark) .tb{background:rgba(255,255,255,.23);border-bottom-color:rgba(50,62,97,.14)}
html:not(.dark) #ttl{color:#262c40}html:not(.dark) #toolbarTheme{color:#424a62;background:rgba(255,255,255,.35)}
html:not(.dark) .pill{background:rgba(255,255,255,.49);border-color:rgba(54,66,104,.16);color:#687187}
html:not(.dark) .pill input{color:#272d42}html:not(.dark) .pill input::placeholder{color:#747d94}
html:not(.dark) .profile-cover{background:linear-gradient(112deg,rgba(255,255,255,.69),rgba(238,243,255,.55) 68%,rgba(205,226,250,.52));border-color:rgba(255,255,255,.82);box-shadow:inset 0 1px rgba(255,255,255,.9),0 10px 32px rgba(43,57,96,.1);color:#252b40}
html:not(.dark) .eyebrow{color:#778099}html:not(.dark) .profile-handle{color:#687188}html:not(.dark) .profile-description{color:#5c667e}
html:not(.dark) .fact-pill{background:rgba(255,255,255,.52);border-color:rgba(69,81,117,.15);color:#454d63}
html:not(.dark) .fact-pill b{color:#20263a}html:not(.dark) .profile-tabs{background:rgba(255,255,255,.43);border-color:rgba(67,78,111,.17)}
html:not(.dark) .profile-tab{color:#414960}html:not(.dark) .profile-tab svg{color:#59647e}html:not(.dark) .profile-tab.active,html:not(.dark) .profile-tab:hover{background:rgba(255,255,255,.78);border-color:rgba(255,255,255,.92);box-shadow:0 3px 12px rgba(45,60,99,.09)}
html:not(.dark) .profile-tab span{background:rgba(81,99,144,.1);color:#58627a}
html:not(.dark) .overview-panel,html:not(.dark) .avatar-panel,html:not(.dark) .wearing-panel{background:linear-gradient(145deg,rgba(255,255,255,.62),rgba(236,241,251,.5));border-color:rgba(255,255,255,.82);box-shadow:inset 0 1px rgba(255,255,255,.9),0 10px 28px rgba(43,57,96,.08);color:#242a3e}
html:not(.dark) .section-heading h2{color:#282e43}html:not(.dark) .heading-count,html:not(.dark) .text-action{color:#69738b}html:not(.dark) .text-action:hover{color:#252b40}
html:not(.dark) .overview-outfit{background:linear-gradient(145deg,rgba(255,255,255,.72),rgba(220,229,246,.65));border-color:rgba(255,255,255,.9);color:#2b3044;box-shadow:0 5px 14px rgba(39,53,89,.08)}
html:not(.dark) .outfit-visual{background:radial-gradient(ellipse at 50% 85%,rgba(47,151,255,.13),transparent 50%),rgba(255,255,255,.28)}
html:not(.dark) .overview-card-id button{background:rgba(255,255,255,.65);color:#47516a;border-color:rgba(73,89,129,.16)}
html:not(.dark) .overview-state,html:not(.dark) .avatar-caption{color:#6b748a}html:not(.dark) .avatar-stage{background:radial-gradient(ellipse at 50% 98%,rgba(150,99,241,.17),transparent 48%),linear-gradient(180deg,rgba(255,255,255,.3),rgba(224,232,247,.4))}
html:not(.dark) .wearing-row{border-color:rgba(48,61,97,.1)}html:not(.dark) .wearing-copy span{color:#727d94}
html:not(.dark) .wearing-id{background:rgba(50,139,222,.1);color:#2f6599;border-color:rgba(49,120,185,.16)}
html:not(.dark) .dock{background:rgba(248,250,255,.5);border-color:rgba(255,255,255,.85);box-shadow:0 12px 38px rgba(40,53,88,.18),inset 0 1px 0 rgba(255,255,255,.94)}
html:not(.dark) .di{box-shadow:0 7px 17px rgba(45,57,91,.2),inset 0 1px 0 rgba(255,255,255,.7)}
html:not(.dark) .cursor-aura{mix-blend-mode:multiply;background:radial-gradient(circle,rgba(78,135,250,.15),rgba(130,125,255,.07) 34%,transparent 72%)}
@media (hover:none),(pointer:coarse){.cursor-aura,.cursor-ripple{display:none}}
@media(prefers-reduced-motion:reduce){.cursor-aura,.cursor-ripple{display:none}.win,.card,.ni,.di,.btn,.iconbtn,.profile-tab,.overview-outfit{transition-duration:.01ms!important;animation-duration:.01ms!important}}
@media(max-width:760px){.menubar{display:none}.dock{display:none}.win,.win.zoom{left:0!important;top:0!important;width:100vw!important;height:100dvh!important;min-height:0;border-radius:0}.win::before{display:none}.side{background:rgba(74,76,104,.58)}}
@media(max-height:620px) and (min-width:761px){.win,.win.zoom{height:calc(100dvh - 92px)!important;min-height:0}.dock{bottom:5px}}

/* Apple-like motion system: every surface eases in, responds to hover and respects reduced motion. */
:root{--ease-out:cubic-bezier(.22,1,.36,1);--ease-spring:cubic-bezier(.2,.9,.25,1.18);--glass-stroke:rgba(255,255,255,.34)}
@keyframes app-enter{from{opacity:0;transform:translateY(12px) scale(.985);filter:blur(5px)}to{opacity:1;transform:translateY(0) scale(1);filter:blur(0)}}
@keyframes glass-settle{0%{opacity:0;transform:translateY(18px) scale(.975);filter:blur(8px)}65%{opacity:1;transform:translateY(-2px) scale(1.006);filter:blur(0)}100%{transform:translateY(0) scale(1)}}
@keyframes menu-glass-in{from{opacity:0;transform:translateY(-7px) scale(.97);filter:blur(6px)}to{opacity:1;transform:translateY(0) scale(1);filter:blur(0)}}
@keyframes sheen-pass{0%{transform:translateX(-130%) skewX(-18deg);opacity:0}25%{opacity:.55}100%{transform:translateX(180%) skewX(-18deg);opacity:0}}
@keyframes aurora-breathe{0%,100%{opacity:.34}50%{opacity:.56}}
@keyframes icon-pop{0%{transform:scale(.82)}65%{transform:scale(1.12)}100%{transform:scale(1)}}

body,.win,.side,.tb,.profile-cover,.overview-panel,.avatar-panel,.wearing-panel,.profile-tabs,.dock,.pill,.menu,.sheet,.alert,.toast{transition:background-color .42s ease,border-color .42s ease,color .32s ease,box-shadow .42s ease,backdrop-filter .42s ease}
.wall i{animation:drift 29s ease-in-out infinite alternate,aurora-breathe 11s ease-in-out infinite}
.win{isolation:isolate;transform-origin:50% 40%}
.win:not(.drag){transition:opacity .3s ease,transform .48s var(--ease-spring),left .44s var(--ease-out),top .44s var(--ease-out),width .44s var(--ease-out),height .44s var(--ease-out),border-radius .42s var(--ease-out),box-shadow .42s ease,background-color .4s ease}
.win.zoom{border-radius:18px}
.win.hide{transform:translateY(45px) scale(.96);filter:blur(8px)}
.win::before{transition:opacity .4s ease}
.tb{cursor:grab}.win.drag .tb{cursor:grabbing}
.menubar{transition:background-color .4s ease,border-color .4s ease,backdrop-filter .4s ease}
.mb{transition:background-color .2s ease,color .2s ease,transform .2s var(--ease-spring)}
.mb:active{transform:scale(.96)}
.menu.show{animation:menu-glass-in .25s var(--ease-spring) both;transform-origin:top left}
.mi{transition:background .18s ease,color .18s ease,padding .2s var(--ease-out)}
.mi:hover{padding-left:14px}
.navshortcuts button{transition:background .22s ease,transform .25s var(--ease-spring),color .2s ease}
.navshortcuts button:hover{transform:translateX(3px)}
.ni{transition:color .25s ease,background .28s ease,transform .3s var(--ease-spring),box-shadow .28s ease}
.ni:hover{transform:translateX(2px)}.ni.act{animation:icon-pop .42s var(--ease-spring) both}
.ni .ico{transition:transform .35s var(--ease-spring),filter .25s ease}.ni:hover .ico{transform:rotate(-7deg) scale(1.1);filter:brightness(1.1)}
.tb h2{transition:opacity .2s ease,transform .35s var(--ease-out)}
.pill{transition:width .35s var(--ease-out),background .3s ease,border-color .3s ease,box-shadow .35s ease,transform .3s var(--ease-spring)}
.pill:focus-within{transform:translateY(-1px)}
.go{overflow:hidden;transition:transform .25s var(--ease-spring),box-shadow .28s ease,filter .25s ease}.go:hover{filter:brightness(1.12);transform:scale(1.06)}.go:active{transform:scale(.9)}
.toolbar-theme{transition:transform .38s var(--ease-spring),background .25s ease,color .25s ease}.toolbar-theme:hover{transform:rotate(18deg) scale(1.08)}
.tab.show{animation:app-enter .42s var(--ease-out) both}
.profile-dashboard{animation:app-enter .52s var(--ease-out) both}
.profile-cover{overflow:hidden;animation:glass-settle .64s var(--ease-spring) both;transform-origin:50% 0}
.profile-cover::after{content:"";position:absolute;inset:-20% auto -20% -42%;width:30%;pointer-events:none;background:linear-gradient(90deg,transparent,rgba(255,255,255,.16),transparent);filter:blur(10px);transform:translateX(-130%) skewX(-18deg)}
.profile-cover:hover::after{animation:sheen-pass .95s var(--ease-out) both}
.profile-cover .avbox{transition:transform .42s var(--ease-spring),box-shadow .35s ease}.profile-cover:hover .avbox{transform:translateY(-2px) scale(1.035);box-shadow:0 12px 30px rgba(0,0,0,.22)}
.profile-facts{animation:app-enter .5s .08s var(--ease-out) both}.fact-pill{transition:transform .24s var(--ease-spring),background-color .25s ease,box-shadow .25s ease}.fact-pill:hover{transform:translateY(-2px);box-shadow:0 5px 12px rgba(0,0,0,.09)}
.profile-tabs{animation:app-enter .5s .08s var(--ease-out) both}.profile-tab{position:relative;overflow:hidden;transition:background .24s ease,border-color .24s ease,transform .3s var(--ease-spring),box-shadow .26s ease,color .25s ease}.profile-tab:hover{transform:translateY(-1px)}.profile-tab:active{transform:scale(.96)}
.overview-panel{animation:app-enter .55s .12s var(--ease-out) both}.avatar-panel,.wearing-panel{animation:app-enter .6s .18s var(--ease-out) both}
.overview-outfit{transform-origin:50% 90%;transition:transform .35s var(--ease-spring),border-color .28s ease,box-shadow .32s ease,background .3s ease}.overview-outfit:hover{transform:translateY(-5px) scale(1.025);box-shadow:0 16px 30px rgba(0,0,0,.18)}.overview-outfit:active{transform:translateY(-1px) scale(.985)}
.outfit-visual img,.wearing-thumb img{transition:transform .48s var(--ease-spring),filter .3s ease}.overview-outfit:hover .outfit-visual img{transform:scale(1.09) rotate(-1deg);filter:drop-shadow(0 9px 10px rgba(0,0,0,.2))}
.section-icon{transition:transform .35s var(--ease-spring),box-shadow .3s ease}.section-heading:hover .section-icon{transform:rotate(-8deg) scale(1.08);box-shadow:0 4px 12px rgba(0,0,0,.18)}
.wearing-row{transition:background .24s ease,transform .25s var(--ease-spring),padding .25s ease}.wearing-row:hover{background:rgba(255,255,255,.07);transform:translateX(3px)}.wearing-row:hover .wearing-thumb img{transform:scale(1.1)}
.btn,.chip,.text-action,.lnkbtn{transition:transform .25s var(--ease-spring),background .25s ease,border-color .25s ease,box-shadow .28s ease,color .24s ease}.btn:hover{transform:translateY(-2px);box-shadow:0 7px 18px rgba(0,0,0,.12)}.btn:active,.chip:active{transform:scale(.96)}
.card{transition:transform .35s var(--ease-spring),box-shadow .35s ease,border-color .3s ease,background .35s ease}.fcard:hover{transform:translateY(-5px) scale(1.012)}
.sheet{animation:glass-settle .42s var(--ease-spring) both}.alert{animation:glass-settle .32s var(--ease-spring) both}.toast{animation:app-enter .35s var(--ease-spring) both}
.dock{transform:translateX(-50%);transition:transform .35s var(--ease-spring),background .35s ease,border-color .35s ease,box-shadow .35s ease}.dock:hover{transform:translateX(-50%) translateY(-4px)}
.di{transform-origin:50% 100%;transition:width .16s ease-out,height .16s ease-out,transform .25s var(--ease-spring),filter .25s ease,box-shadow .25s ease}.di:hover{transform:translateY(-5px) scale(1.08);filter:brightness(1.08)}.di:active{transform:translateY(-2px) scale(.94)}
.sw:hover{transform:scale(1.25)}
.cursor-aura{transition:opacity .3s ease,filter .3s ease}.cursor-aura.visible{filter:blur(0)}
button:focus-visible,a:focus-visible,[tabindex]:focus-visible{outline:2px solid color-mix(in srgb,var(--acc) 68%,white);outline-offset:3px}
/* Selectable animated wallpapers and a consistent glass material across the whole app */
:root{--wall-base-dark:#101321;--wall-base-light:#e8edf7;--wall-a:#548cff;--wall-b:#e989be;--wall-c:#46d7b2;--wall-d:#896aff}
body{background-color:var(--wall-base-dark);background-image:none;transition:background-color 1s ease}
html:not(.dark) body{background-color:var(--wall-base-light);background-image:none}
.wall i{background:var(--wall-a);transition:background-color 1.4s ease,opacity 1.2s ease}
.wall .b1{background:var(--wall-a)}.wall .b2{background:var(--wall-b)}.wall .b3{background:var(--wall-c)}
.wall::before{content:"";position:absolute;inset:-28%;pointer-events:none;opacity:.25;filter:blur(85px);background:conic-gradient(from 20deg at 50% 50%,color-mix(in srgb,var(--wall-d) 43%,transparent),transparent 27%,color-mix(in srgb,var(--wall-b) 38%,transparent) 48%,transparent 72%,color-mix(in srgb,var(--wall-c) 40%,transparent));animation:wall-liquid 38s ease-in-out infinite alternate;transition:opacity 1s ease}
@keyframes wall-liquid{0%{transform:translate3d(-2%,1%,0) rotate(-8deg) scale(.96)}50%{transform:translate3d(2%,-1%,0) rotate(5deg) scale(1.04)}100%{transform:translate3d(-1%,-2%,0) rotate(12deg) scale(1.08)}}
.sidefoot{display:flex;flex-direction:column;align-items:stretch;gap:10px;padding:11px 5px 5px}.sidefoot::before{display:none;content:none}
.palette-block{display:flex;flex-direction:column;gap:5px}.palette-label{font-size:9px;font-weight:650;color:rgba(235,238,255,.69);letter-spacing:.03em}.palette-row{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.sw{width:13px;height:13px;padding:0;flex:none;border:1.5px solid rgba(255,255,255,.38);border-radius:50%;box-shadow:0 1px 5px rgba(0,0,0,.2);cursor:pointer}
.sw.on{box-shadow:0 0 0 2px rgba(255,255,255,.8),0 0 0 4px color-mix(in srgb,var(--acc) 65%,transparent)}
.wallpaper-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:5px}
.wallpick{height:17px;min-width:0;padding:0;border:1px solid rgba(255,255,255,.32);border-radius:6px;background:linear-gradient(135deg,var(--wall-a),var(--wall-b) 48%,var(--wall-c));box-shadow:inset 0 1px rgba(255,255,255,.45),0 2px 5px rgba(0,0,0,.16);cursor:pointer;transition:transform .28s var(--ease-spring),box-shadow .28s ease,border-color .28s ease}
.wallpick::after{content:"";display:block;width:100%;height:100%;border-radius:inherit;background:linear-gradient(150deg,rgba(255,255,255,.25),transparent 60%)}
.wallpick:hover{transform:translateY(-2px) scale(1.08);box-shadow:0 4px 9px rgba(0,0,0,.22)}.wallpick.on{border:2px solid #fff;box-shadow:0 0 0 2px color-mix(in srgb,var(--acc) 75%,transparent),0 3px 9px rgba(0,0,0,.25)}
.card,.stat,.tag,.chip,.rchip,.btn,.raw,.pres,.pill,.menu,.sheet,.alert,.toast,.profile-tabs{border-color:var(--glass-stroke);box-shadow:inset 0 1px 0 rgba(255,255,255,.17),0 8px 26px rgba(0,0,0,.09);backdrop-filter:blur(28px) saturate(175%);-webkit-backdrop-filter:blur(28px) saturate(175%)}
.card{background:linear-gradient(145deg,rgba(255,255,255,.12),rgba(255,255,255,.045));}
html:not(.dark) .palette-label{color:#5c6680}
html:not(.dark) .sw{border-color:rgba(41,53,85,.26)}html:not(.dark) .sw.on{box-shadow:0 0 0 2px rgba(255,255,255,.95),0 0 0 4px color-mix(in srgb,var(--acc) 70%,transparent)}
html:not(.dark) .wallpick{border-color:rgba(55,70,107,.28)}html:not(.dark) .wallpick.on{border-color:#fff}
html:not(.dark) .card,html:not(.dark) .stat,html:not(.dark) .tag,html:not(.dark) .chip,html:not(.dark) .rchip,html:not(.dark) .btn,html:not(.dark) .raw,html:not(.dark) .pres{background:linear-gradient(145deg,rgba(255,255,255,.67),rgba(242,246,255,.43));border-color:rgba(255,255,255,.76);box-shadow:inset 0 1px 0 rgba(255,255,255,.85),0 8px 24px rgba(45,59,97,.08);color:#2f354a}
html:not(.dark) .btn.pri{background:linear-gradient(135deg,#258df5,#56a8ff);color:#fff;border-color:rgba(255,255,255,.45)}
html:not(.dark) .wall::before{opacity:.3}
@media(max-width:520px){.palette-label{display:none}.sidefoot{padding:6px 1px;gap:6px}.palette-block{gap:3px}.palette-row{justify-content:center;gap:3px}.wallpaper-grid{grid-template-columns:repeat(3,minmax(0,1fr));gap:3px}.wallpick{height:13px;border-radius:4px}.sw{width:9px;height:9px}}
.sh-t .sub{display:flex;align-items:center;gap:7px;flex-wrap:wrap}
@media(prefers-reduced-motion:reduce){.wall::before,.wall i{animation:none!important;transition:none!important}}
/* Premium home screen: layered glass, intentional type scale, and a soft 3D centerpiece. */
.hero{display:grid;grid-template-columns:minmax(0,1.05fr) minmax(250px,.8fr);align-items:center;gap:clamp(18px,4vw,64px);min-height:320px;padding:clamp(28px,5vw,62px);border-radius:30px;background:linear-gradient(120deg,color-mix(in srgb,var(--acc) 16%,var(--glass2)),color-mix(in srgb,var(--glass2) 76%,transparent) 55%,rgba(151,132,255,.09));border-color:rgba(255,255,255,.27);box-shadow:inset 0 1px 0 rgba(255,255,255,.34),0 22px 64px rgba(8,14,35,.16);isolation:isolate}
.hero::before{content:"";position:absolute;z-index:-1;inset:0;border-radius:inherit;background:linear-gradient(115deg,rgba(255,255,255,.09),transparent 38%,rgba(255,255,255,.025));pointer-events:none}
.hero::after{z-index:-1;width:310px;height:310px;right:3%;top:50%;transform:translateY(-50%);border-color:rgba(255,255,255,.18);box-shadow:0 0 0 26px rgba(255,255,255,.025),0 0 0 62px rgba(255,255,255,.02),0 0 0 105px rgba(255,255,255,.014)}
.hero-copy{position:relative;z-index:2;max-width:600px;animation:app-enter .65s var(--ease-out) both}
.hero-eyebrow{display:flex;align-items:center;gap:9px;margin-bottom:17px;color:color-mix(in srgb,var(--txt) 65%,var(--acc));font-size:10px;font-weight:800;letter-spacing:.17em}
.hero-eyebrow i{width:8px;height:8px;border-radius:50%;background:#51e2a1;box-shadow:0 0 14px rgba(81,226,161,.8);animation:live-pulse 2s ease-in-out infinite}
@keyframes live-pulse{50%{box-shadow:0 0 0 5px rgba(81,226,161,.11),0 0 20px rgba(81,226,161,.65);opacity:.75}}
.hero h1{max-width:720px;font-size:clamp(38px,5.4vw,68px);font-weight:780;line-height:.99;letter-spacing:-.065em;text-wrap:balance}
.hero h1 span{background:linear-gradient(100deg,#fff 2%,#c8d6ff 42%,#a4b7ff 78%,#f4c5e8);background-clip:text;-webkit-background-clip:text;color:transparent}
.hero p{max-width:490px;margin-top:17px;color:color-mix(in srgb,var(--txt) 72%,transparent);font-size:15px;line-height:1.7}
.hero .quick{justify-content:flex-start;gap:9px;margin-top:24px}
.hero .qb{height:36px;padding:0 15px;border-radius:12px;border-color:rgba(255,255,255,.19);background:rgba(255,255,255,.095);color:var(--txt);backdrop-filter:blur(18px);-webkit-backdrop-filter:blur(18px);box-shadow:inset 0 1px rgba(255,255,255,.15),0 5px 15px rgba(0,0,0,.08)}
.hero .qb:hover{background:var(--acc);border-color:transparent;color:#fff;box-shadow:0 8px 22px color-mix(in srgb,var(--acc) 32%,transparent)}
.hero-caption{display:flex;align-items:center;gap:7px;margin-top:18px;color:color-mix(in srgb,var(--txt) 57%,transparent);font-size:10px;font-weight:550}.hero-caption b{opacity:.5}.hero-spark{color:#ffd37e;font-size:14px}
.hero-art{position:relative;z-index:1;display:grid;place-items:center;min-width:0;height:280px;perspective:900px;isolation:isolate;animation:app-enter .8s .1s var(--ease-out) both}
.hero-art::before{content:"";position:absolute;z-index:-1;width:72%;height:72%;border-radius:50%;background:radial-gradient(circle,rgba(103,154,255,.27),rgba(160,111,255,.13) 40%,transparent 72%);filter:blur(28px);animation:hero-aura 7s ease-in-out infinite alternate}
@keyframes hero-aura{to{transform:scale(1.22);opacity:.68}}
.hero-orbit{position:absolute;border:1px solid rgba(220,231,255,.24);border-radius:50%;transform:rotateX(66deg) rotateZ(-25deg);box-shadow:0 0 24px rgba(132,171,255,.09),inset 0 0 20px rgba(255,255,255,.025)}
.orbit-one{width:245px;height:245px;animation:orbit-turn 22s linear infinite}.orbit-two{width:198px;height:198px;transform:rotateX(68deg) rotateZ(39deg);border-color:rgba(218,195,255,.3);animation:orbit-turn 17s linear infinite reverse}.orbit-three{width:286px;height:286px;transform:rotateX(72deg) rotateZ(80deg);border-style:dashed;border-color:rgba(217,228,255,.12);animation:orbit-turn 32s linear infinite}
@keyframes orbit-turn{to{rotate:1turn}}
.hero-crystal{position:relative;display:grid;place-items:center;width:136px;height:136px;border:1px solid rgba(255,255,255,.55);border-radius:38px;background:linear-gradient(145deg,rgba(255,255,255,.4),rgba(255,255,255,.08) 42%,rgba(116,154,255,.23));backdrop-filter:blur(25px) saturate(165%);-webkit-backdrop-filter:blur(25px) saturate(165%);box-shadow:inset 0 2px 2px rgba(255,255,255,.52),inset 0 -14px 34px rgba(77,109,210,.14),0 28px 64px rgba(0,0,0,.25),0 0 45px rgba(124,161,255,.22);transform:rotate(-9deg) rotateY(-13deg);animation:crystal-float 5.2s ease-in-out infinite}
.hero-crystal::before{content:"";position:absolute;inset:8px;border:1px solid rgba(255,255,255,.22);border-radius:31px;pointer-events:none}
.hero-crystal-inner{display:grid;place-items:center;width:75px;height:75px;border-radius:25px;background:linear-gradient(145deg,#67b3ff,#626bff 58%,#b46aff);color:#fff;box-shadow:0 12px 30px rgba(58,112,255,.42),inset 0 1px 1px rgba(255,255,255,.6)}.hero-crystal-inner svg{width:43px;height:43px;stroke-width:1.55}
@keyframes crystal-float{0%,100%{transform:translateY(0) rotate(-9deg) rotateY(-13deg)}50%{transform:translateY(-9px) rotate(-5deg) rotateY(8deg)}}
.hero-float{position:absolute;display:flex;align-items:center;gap:9px;padding:10px 13px;border:1px solid rgba(255,255,255,.29);border-radius:14px;background:rgba(35,39,62,.48);backdrop-filter:blur(26px) saturate(175%);-webkit-backdrop-filter:blur(26px) saturate(175%);box-shadow:inset 0 1px rgba(255,255,255,.2),0 12px 30px rgba(0,0,0,.2);color:#f4f5ff;white-space:nowrap}
.hero-float b,.hero-float small{display:block}.hero-float b{font-size:10px}.hero-float small{margin-top:2px;color:rgba(235,238,255,.64);font-size:8px}.float-top{top:24px;right:-2px;animation:float-card 5.5s ease-in-out infinite}.float-bottom{left:-18px;bottom:25px;animation:float-card 6.5s .8s ease-in-out infinite reverse}.float-dot{width:8px;height:8px;border-radius:50%;background:#40e091;box-shadow:0 0 11px rgba(64,224,145,.8)}.float-glyph{display:grid;place-items:center;width:26px;height:26px;border-radius:9px;background:rgba(184,112,255,.25);color:#e8c9ff;font-size:16px}
@keyframes float-card{0%,100%{translate:0 0}50%{translate:0 -7px}}
.hero-glint{position:absolute;width:4px;height:4px;border-radius:50%;background:#fff;box-shadow:0 0 12px 3px rgba(210,223,255,.66);animation:glint 3s ease-in-out infinite}.glint-one{top:34px;left:16%}.glint-two{right:12%;bottom:52px;animation-delay:1.4s}@keyframes glint{0%,100%{opacity:.25;scale:.7}50%{opacity:1;scale:1.4}}
.sec{margin-top:30px}.sec>span{font-size:10px;letter-spacing:.11em}
#recentBox{padding:0 2px}.rec{gap:9px}.rchip{border-radius:14px;padding:6px 12px 6px 6px;background:rgba(255,255,255,.09);backdrop-filter:blur(22px);-webkit-backdrop-filter:blur(22px);box-shadow:inset 0 1px rgba(255,255,255,.14),0 4px 14px rgba(0,0,0,.06)}.rchip:hover{transform:translateY(-3px) scale(1.015);box-shadow:0 10px 24px rgba(0,0,0,.13)}
.fcard{position:relative;min-height:164px;padding:21px;border-radius:19px;background:linear-gradient(145deg,rgba(255,255,255,.12),rgba(255,255,255,.045));backdrop-filter:blur(28px) saturate(160%);-webkit-backdrop-filter:blur(28px) saturate(160%);box-shadow:inset 0 1px rgba(255,255,255,.2),0 13px 30px rgba(0,0,0,.1)}
.fcard::before{content:"";position:absolute;inset:0;border-radius:inherit;pointer-events:none;background:radial-gradient(320px circle at var(--mx,50%) var(--my,0%),color-mix(in srgb,var(--acc) 14%,transparent),transparent 58%);opacity:0;transition:opacity .35s ease}.fcard:hover::before{opacity:1}.fcard .ico{position:relative;width:42px;height:42px;border:1px solid rgba(255,255,255,.23);box-shadow:0 8px 19px color-mix(in srgb,var(--acc) 19%,transparent)}.fcard b{font-size:14px;letter-spacing:-.01em}.fcard p{font-size:11px;line-height:1.65;color:color-mix(in srgb,var(--txt) 64%,transparent)}
html:not(.dark) .hero{background:linear-gradient(120deg,rgba(255,255,255,.7),rgba(242,247,255,.52) 55%,rgba(220,228,255,.48));border-color:rgba(255,255,255,.87);box-shadow:inset 0 1px 0 rgba(255,255,255,.95),0 22px 58px rgba(62,82,137,.11)}
html:not(.dark) .hero h1 span{background-image:linear-gradient(100deg,#327fe4,#7673ed 56%,#c264bb)}html:not(.dark) .hero p{color:#646e85}html:not(.dark) .hero-eyebrow{color:#64708a}html:not(.dark) .hero-caption{color:#717c94}
html:not(.dark) .hero .qb{background:rgba(255,255,255,.56);border-color:rgba(255,255,255,.86);color:#38435e;box-shadow:inset 0 1px rgba(255,255,255,.95),0 5px 13px rgba(56,73,119,.09)}html:not(.dark) .hero .qb:hover{background:var(--acc);color:#fff}
html:not(.dark) .hero-crystal{background:linear-gradient(145deg,rgba(255,255,255,.78),rgba(255,255,255,.33) 42%,rgba(116,154,255,.25));border-color:rgba(255,255,255,.9);box-shadow:inset 0 2px 2px rgba(255,255,255,.95),0 28px 58px rgba(80,103,169,.17),0 0 45px rgba(124,161,255,.18)}
html:not(.dark) .hero-float{background:rgba(255,255,255,.64);border-color:rgba(255,255,255,.9);color:#303950;box-shadow:inset 0 1px rgba(255,255,255,.95),0 12px 30px rgba(63,81,132,.14)}html:not(.dark) .hero-float small{color:#78839a}
html:not(.dark) .fcard{background:linear-gradient(145deg,rgba(255,255,255,.66),rgba(236,243,255,.48));box-shadow:inset 0 1px rgba(255,255,255,.95),0 13px 30px rgba(62,82,137,.09)}html:not(.dark) .fcard p{color:#68738a}
@media(max-width:760px){.hero{grid-template-columns:minmax(0,1fr) minmax(180px,.66fr);min-height:270px;padding:clamp(22px,4vw,36px);gap:12px}.hero-art{height:220px}.hero-crystal{width:112px;height:112px;border-radius:32px}.hero-crystal-inner{width:64px;height:64px}.orbit-one{width:196px;height:196px}.orbit-two{width:160px;height:160px}.orbit-three{width:226px;height:226px}.float-top{right:-8px}.float-bottom{left:-8px}.hero h1{font-size:clamp(34px,5vw,50px)}}
@media(max-width:520px){.hero{grid-template-columns:1fr;min-height:0;padding:25px 20px}.hero-art{height:140px;margin-top:8px}.hero-orbit.orbit-three{display:none}.orbit-one{width:150px;height:150px}.orbit-two{width:125px;height:125px}.hero-crystal{width:91px;height:91px;border-radius:27px}.hero-crystal-inner{width:53px;height:53px;border-radius:18px}.hero-crystal-inner svg{width:32px;height:32px}.hero-float{padding:7px 9px}.float-top{top:4px;right:4px}.float-bottom{left:0;bottom:0}.hero h1{font-size:clamp(34px,10vw,48px)}.hero p{margin-top:12px;font-size:13px}.hero .quick{margin-top:17px}}
@supports not (backdrop-filter:blur(1px)){.hero,.fcard,.rchip,.hero-float{background:rgba(40,43,68,.92)}}
@supports not (backdrop-filter:blur(1px)){.win,.side,.menubar,.dock,.profile-cover,.overview-panel,.avatar-panel,.wearing-panel,.profile-tabs,.menu,.sheet,.alert,.toast{background-color:rgba(31,33,52,.94)}}
@media(max-width:760px){.win.zoom{left:0!important;top:0!important;width:100vw!important;height:100dvh!important;border-radius:0}.win:not(.drag){transition:opacity .28s ease,transform .35s var(--ease-spring)}.dock:hover{transform:translateX(-50%)}.overview-outfit:hover,.fcard:hover{transform:translateY(-2px)}}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation-duration:.01ms!important;animation-iteration-count:1!important;scroll-behavior:auto!important;transition-duration:.01ms!important;transition-delay:0ms!important}}
/* Sidebar refinements + persisted, keyboard-accessible width control */
.side{width:var(--sidebar-width,178px);min-width:var(--sidebar-width,178px);background:linear-gradient(155deg,rgba(255,255,255,.105),rgba(255,255,255,.035) 45%,rgba(255,255,255,.055));backdrop-filter:blur(42px) saturate(165%);-webkit-backdrop-filter:blur(42px) saturate(165%);transition:width .38s var(--ease-out),min-width .38s var(--ease-out),background .35s ease,border-color .35s ease}
.side .lights{flex:none;padding:3px 6px 20px}.side .nav{flex:none;gap:5px}
.navlabel{margin:4px 8px 7px;font-size:9px;letter-spacing:.14em;color:rgba(229,233,255,.56)}.navlabel-last{margin-top:20px}
.ni{height:38px;padding:0 9px;border:1px solid transparent;border-radius:12px;font-size:11.5px;font-weight:580}
.ni .ico{width:22px;height:22px;border-radius:8px;box-shadow:inset 0 1px rgba(255,255,255,.38),0 3px 8px rgba(0,0,0,.12)}
.ni.act{background:linear-gradient(105deg,color-mix(in srgb,var(--acc) 83%,white 8%),color-mix(in srgb,var(--acc) 52%,rgba(255,255,255,.12)));border-color:rgba(255,255,255,.22);box-shadow:inset 0 1px rgba(255,255,255,.28),0 7px 19px color-mix(in srgb,var(--acc) 25%,transparent)}
.navshortcuts{gap:4px}.navshortcuts button{height:29px;border:1px solid transparent;border-radius:9px;padding:0 8px;font-size:10.5px}.navshortcuts button:hover{border-color:rgba(255,255,255,.09);background:rgba(255,255,255,.075)}
.sidefoot{position:relative;margin-top:auto;padding:12px 7px 7px;gap:11px;border-top:1px solid rgba(255,255,255,.1);background:linear-gradient(180deg,rgba(255,255,255,.025),rgba(255,255,255,.055));border-radius:13px 13px 0 0}
.palette-label{font-size:8px;text-transform:uppercase;letter-spacing:.12em;color:rgba(235,238,255,.53)}
.palette-row{gap:7px}.sw{width:14px;height:14px}.wallpaper-grid{gap:6px}.wallpick{height:21px;border-radius:7px}
.side-resize{position:relative;z-index:15;flex:0 0 8px;width:8px;margin:0 -4px;display:flex;align-items:center;justify-content:center;cursor:col-resize;touch-action:none;user-select:none}
.side-resize::before{content:"";position:absolute;inset:0 -3px;}
.side-resize span{position:relative;width:3px;height:42px;border-radius:99px;background:linear-gradient(180deg,transparent,rgba(255,255,255,.68) 22%,rgba(255,255,255,.68) 78%,transparent);opacity:.24;transform:scaleY(.72);transition:opacity .22s ease,transform .3s var(--ease-spring),width .2s ease;box-shadow:0 0 12px rgba(150,190,255,.28)}
.side-resize:hover span,.side-resize:focus-visible span,.side-resize.dragging span{opacity:1;transform:scaleY(1)}.side-resize.dragging span{width:4px;height:70px}
.side-resize:focus-visible{outline:0}.side-resize:focus-visible span{box-shadow:0 0 0 3px color-mix(in srgb,var(--acc) 65%,transparent),0 0 14px rgba(150,190,255,.4)}
html:not(.dark) .side{background:linear-gradient(155deg,rgba(255,255,255,.82),rgba(235,240,251,.54) 52%,rgba(222,231,249,.62));border-right-color:rgba(72,88,130,.16);box-shadow:inset -1px 0 rgba(255,255,255,.75)}
html:not(.dark) .navlabel{color:#77819a}html:not(.dark) .ni{color:#384159}html:not(.dark) .ni.act{color:#fff}
html:not(.dark) .navshortcuts button{color:#4d5871}html:not(.dark) .navshortcuts button:hover{background:rgba(66,95,161,.08);border-color:rgba(66,95,161,.12)}
html:not(.dark) .sidefoot{border-top-color:rgba(67,80,118,.14);background:linear-gradient(180deg,rgba(255,255,255,.26),rgba(255,255,255,.52))}
html:not(.dark) .side-resize span{background:linear-gradient(180deg,transparent,rgba(62,87,145,.55) 22%,rgba(62,87,145,.55) 78%,transparent);box-shadow:0 0 12px rgba(87,125,205,.22)}
@media(max-width:760px){.side{width:150px!important;min-width:150px!important}.side-resize{display:none}}
@media(max-width:520px){.side{width:62px!important;min-width:62px!important}.sidefoot{padding:6px 1px;gap:6px}.palette-label{display:none}}
/* Directional tab paging and finishing motion for secondary controls/content. */
@keyframes tab-wheel-next{from{opacity:0;transform:translateY(30px) scale(.99);filter:blur(5px)}to{opacity:1;transform:translateY(0) scale(1);filter:blur(0)}}
@keyframes tab-wheel-prev{from{opacity:0;transform:translateY(-30px) scale(.99);filter:blur(5px)}to{opacity:1;transform:translateY(0) scale(1);filter:blur(0)}}
#content{overscroll-behavior:contain;scroll-behavior:smooth}
#content.scroll-next .tab.show{animation:tab-wheel-next .48s var(--ease-out) both}
#content.scroll-prev .tab.show{animation:tab-wheel-prev .48s var(--ease-out) both}
.tab.show>.state,.tab.show>pre.raw,.tab.show>.grid,.tab.show>.pills{animation:app-enter .42s var(--ease-out) both}
.ni{animation:app-enter .42s calc(var(--i,0)*35ms) backwards}
.ni.act{animation:icon-pop .42s var(--ease-spring) both}
.navshortcuts button{animation:app-enter .38s calc(var(--i,0)*40ms) backwards}.navshortcuts button:nth-child(2){animation-delay:40ms}.navshortcuts button:nth-child(3){animation-delay:80ms}
.palette-block{animation:app-enter .44s var(--ease-out) both}.palette-block:nth-child(2){animation-delay:.1s}
.sw,.wallpick{transform-origin:center;transition:transform .28s var(--ease-spring),box-shadow .3s ease,filter .25s ease,border-color .28s ease}
.sw:active,.wallpick:active{transform:scale(.88)}
.wallpick:nth-child(2){animation-delay:35ms}.wallpick:nth-child(3){animation-delay:70ms}.wallpick:nth-child(4){animation-delay:105ms}.wallpick:nth-child(5){animation-delay:140ms}
.rchip,.stat,.tag,.pres,.raw,.wearing-row,.overview-card-id button{transition:transform .28s var(--ease-spring),background .28s ease,border-color .28s ease,box-shadow .3s ease,color .24s ease}
.stat:hover{transform:translateY(-3px);box-shadow:inset 0 1px rgba(255,255,255,.2),0 12px 24px rgba(0,0,0,.13)}
.rchip:active,.wallpick:active,.sw:active{filter:brightness(1.1)}
.raw{animation:app-enter .42s var(--ease-out) both}
.state{animation:app-enter .38s var(--ease-out) both}
@media(max-width:760px){#content.scroll-next .tab.show,#content.scroll-prev .tab.show{animation-duration:.36s}}
@media(prefers-reduced-motion:reduce){#content{scroll-behavior:auto}#content.scroll-next .tab.show,#content.scroll-prev .tab.show,.tab.show>.state,.tab.show>pre.raw,.tab.show>.grid,.tab.show>.pills,.palette-block,.ni,.navshortcuts button{animation:none!important}}
.collection-tools{display:flex;align-items:center;gap:9px;margin:0 0 14px;flex-wrap:wrap}.collection-search{height:36px;min-width:min(240px,100%);flex:1;display:flex;align-items:center;gap:8px;padding:0 11px;border:1px solid var(--line);border-radius:11px;background:var(--glass2);color:var(--sub)}.collection-search input{min-width:0;flex:1;border:0;outline:0;background:transparent;color:var(--txt);font:inherit;font-size:12px}.collection-tools select{height:36px;max-width:190px;padding:0 10px;border:1px solid var(--line);border-radius:11px;background:var(--glass2);color:var(--txt);font:inherit;font-size:11px}.collection-tools select option{color:#222}.tool-btn{width:36px;height:36px;display:grid;place-items:center;border:1px solid var(--line);border-radius:11px;background:var(--glass2);color:var(--txt);cursor:pointer}.export-csv{height:36px;padding:0 11px;font-size:11px}.collection-grid.list-layout{grid-template-columns:1fr!important;gap:8px}.collection-grid.list-layout .card{display:flex;align-items:center;min-height:82px}.collection-grid.list-layout .th,.collection-grid.list-layout .gi{width:78px;height:78px;min-width:78px;flex:none;border-radius:12px}.collection-grid.list-layout .cb,.collection-grid.list-layout .gb{flex:1;min-width:0}.collection-grid.list-layout .rowcard .gb{padding:10px 14px}.filter-empty{padding:20px;text-align:center;color:var(--sub);font-size:13px}.rchip{padding:0;gap:0}.rchip-open{min-width:0;display:flex;align-items:center;gap:9px;padding:5px 8px 5px 5px;border:0;background:transparent;color:var(--txt);cursor:pointer;text-align:left}.rchip-open span:not(.ph2){min-width:0}.rchip-open b{font-size:12px}.recent-remove{width:27px;height:27px;margin:0 5px 0 2px;display:grid;place-items:center;border:0;border-radius:50%;background:transparent;color:var(--sub);cursor:pointer}.recent-remove:hover{background:rgba(255,90,110,.14);color:#ff6b81}.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}.profile-tool-row{display:flex;justify-content:center;gap:5px}.profile-tool-row .tool-btn{width:29px;height:29px;border-radius:9px}.profile-actions .favorite-toggle{border:1px solid rgba(255,255,255,.26);background:rgba(255,255,255,.12);color:#fff}.profile-actions .favorite-toggle.saved{background:rgba(255,196,65,.2);border-color:rgba(255,205,91,.45);color:#ffe08b}.favorites-empty{min-height:250px;padding:36px;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center}.favorite-empty-icon{width:56px;height:56px;display:grid;place-items:center;border-radius:18px;background:rgba(255,190,65,.15);color:#ffbd4a}.favorites-empty h2{margin:14px 0 5px;font-size:20px}.favorites-empty p{max-width:360px;margin-bottom:16px;color:var(--sub);font-size:12px}.favorites-head{display:flex;align-items:center;justify-content:space-between;margin:0 0 14px}.favorites-head h2{font-size:20px}.favorites-head h2 span{color:var(--sub);font-size:14px}.favorites-grid{grid-template-columns:repeat(auto-fill,minmax(230px,1fr))}.favorite-card{min-height:84px;padding:12px;display:flex;align-items:center;gap:12px;cursor:pointer}.favorite-avatar{width:50px;height:50px;flex:none;overflow:hidden;border-radius:15px;background:var(--glass2)}.favorite-avatar img{width:100%;height:100%;object-fit:cover}.favorite-copy{min-width:0;flex:1;display:flex;flex-direction:column}.favorite-copy b,.favorite-copy span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.favorite-copy b{font-size:13px}.favorite-copy span{color:var(--sub);font-size:10px}.favorite-remove{width:30px;height:30px;flex:none;display:grid;place-items:center;border:0;border-radius:9px;background:transparent;color:var(--sub);cursor:pointer}.favorite-remove:hover{background:rgba(255,90,110,.14);color:#ff6b81}.discord-embed-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:0 0 10px;padding:12px 15px;border:1px solid rgba(99,112,220,.2);border-radius:12px;background:linear-gradient(110deg,rgba(84,97,178,.14),rgba(255,255,255,.035))}.discord-embed-head b{display:block;color:var(--txt);font-size:12px;letter-spacing:.08em}.discord-embed-head span{display:block;margin-top:4px;color:var(--sub);font-size:11px}.discord-embed-head .btn{flex:none;height:30px;font-size:10px}#discordRadar{display:block;width:100%;height:calc(100vh - 175px);min-height:570px;border:1px solid rgba(99,112,220,.2);border-radius:13px;background:#0b0c11;box-shadow:0 14px 34px rgba(0,0,0,.16)}
@media(max-width:520px){.discord-embed-head{padding:10px;align-items:flex-start}.discord-embed-head .btn{font-size:9px;padding-inline:8px}#discordRadar{height:calc(100vh - 155px);min-height:630px}}
</style>
</head>
<body>
<div class="wall"><i class="b1"></i><i class="b2"></i><i class="b3"></i></div>
<div class="cursor-aura" id="cursorAura" aria-hidden="true"></div>
<div class="menubar" id="mbar"></div>
<div class="menu" id="menu"></div>

<div class="win" id="win">
 <aside class="side">
  <div class="lights"><b class="l1" id="l1"></b><b class="l2" id="l2"></b><b class="l3" id="l3"></b></div>
  <div class="nav" id="nav"></div>
  <div class="sidefoot" id="swatches"></div>
 </aside>
 <div class="side-resize" id="sideResize" role="separator" aria-orientation="vertical" aria-label="Resize sidebar" aria-valuemin="168" aria-valuemax="340" aria-valuenow="178" tabindex="0" title="Drag to resize · double-click for size presets"><span></span></div>
 <div class="main">
  <div class="tb" id="tb">
   <button class="iconbtn" id="sbBtn" title="Seitenleiste ein/aus"></button>
   <a class="iconbtn" id="deskBtn" href="/" title="Zur Tool-Auswahl" aria-label="Zur Tool-Auswahl"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m3 11 9-8 9 8M5 10v10h14V10M9 20v-6h6v6"/></svg></a>
   <h2 id="ttl">Start</h2>
   <div class="pill" id="pill"><span class="spin"></span><input id="q" placeholder="Username or User ID" autocomplete="off" spellcheck="false"><button class="go" id="go" aria-label="Search">Search</button></div><button class="iconbtn toolbar-theme" id="toolbarTheme" title="Toggle light and dark theme" aria-label="Toggle theme"></button>
  </div>
  <div class="content" id="content">
   <section class="tab show" id="t-start">
    <div class="hero">
     <div class="hero-copy">
      <div class="hero-eyebrow"><i></i> ROBLOX PROFILE EXPLORER</div>
      <h1>Every profile.<br><span>More to discover.</span></h1>
      <p>Explore avatars, saved outfits, games and groups — all in one beautifully clear view.</p>
      <div class="quick"><button class="qb" data-q="Roblox">Roblox</button><button class="qb" data-q="builderman">builderman</button><button class="qb" data-q="1">ID 1</button></div>
      <div class="hero-caption"><span class="hero-spark">✦</span> No login required <b>·</b> Search by username or ID</div>
     </div>
     <div class="hero-art" aria-hidden="true">
      <div class="hero-orbit orbit-one"></div><div class="hero-orbit orbit-two"></div><div class="hero-orbit orbit-three"></div>
      <div class="hero-crystal"><div class="hero-crystal-inner" id="logo"></div></div>
      <div class="hero-float float-top"><span class="float-dot green"></span><span><b>Live presence</b><small>Profile insights</small></span></div>
      <div class="hero-float float-bottom"><span class="float-glyph">✦</span><span><b>Outfits &amp; items</b><small>One quick search</small></span></div>
      <div class="hero-glint glint-one"></div><div class="hero-glint glint-two"></div>
     </div>
    </div>
    <div id="recentBox" style="display:none">
     <div class="sec"><span>Zuletzt gesucht</span><button class="lnkbtn" id="recClear">Löschen</button></div>
     <div class="rec" id="recentList"></div>
    </div>
    <div class="sec"><span>Was du hier findest</span></div>
    <div class="grid w" id="feat"></div>
   </section>
   <section class="tab" id="t-profil"></section>
   <section class="tab" id="t-discord"><div class="discord-embed-head"><div><b>DISCORD NAME RADAR</b><span>Watchlist, manual statuses, reminders and settings — in the same workspace.</span></div><a class="btn" href="/discord" target="_blank" rel="noopener">Open standalone ↗</a></div><iframe id="discordRadar" src="/discord?embed=1" title="Discord Name Radar"></iframe></section>
   <section class="tab" id="t-favorites"></section>
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
 at:'M12 3a9 9 0 1 0 9 9v1a2 2 0 1 1-2-2v1a7 7 0 1 1-7-7M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0z',
 ext:'M14 4h6v6M20 4l-9 9M18 14v6H4V6h6',
 warn:'M12 3l10 18H2L12 3zM12 10v5M12 18h.01',
 x:'M6 6l12 12M18 6L6 18',
 star:'M12 3.5l2.6 5.3 5.9.9-4.25 4.1 1 5.8L12 16.8 6.75 19.6l1-5.8L3.5 9.7l5.9-.9L12 3.5z',
 share:'M12 16V4M7 9l5-5 5 5M5 14v5h14v-5',
 download:'M12 3v12M7 10l5 5 5-5M5 19h14',
 refresh:'M20 7v5h-5M4 17v-5h5M5.5 9A7 7 0 0118 6l2 2M6 16l-2 2 2 2M4 18a7 7 0 0012.5-3',
 grid:'M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4zM14 14h6v6h-6z',
 list:'M8 6h12M8 12h12M8 18h12M4 6h.01M4 12h.01M4 18h.01'
};
const ic=(n,s=20)=>`<svg viewBox="0 0 24 24" width="${s}" height="${s}" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="${ICONS[n]}"/></svg>`;

const TABS=[
 {id:'start',label:'Start',icon:'home',color:'#308cff',desc:''},
 {id:'profil',label:'Profile',icon:'user',color:'#b55bfa',desc:'Freunde, Follower, Status, frühere Namen und Profilbild.'},
 {id:'discord',label:'Name Radar',icon:'at',color:'#626ff3',desc:'Keep a private Discord username watchlist with notes, priorities and manual reminders.'},
 {id:'favorites',label:'Favorites',icon:'star',color:'#ff4d78',badge:'cFavorites',desc:''},
 {id:'outfits',label:'Saved outfits',icon:'layers',color:'#ff4d78',badge:'cOutfits',desc:'Gespeicherte Outfits. Klicke ein Outfit an, um alle Items zu sehen.'},
 {id:'games',label:'Store looks',icon:'game',color:'#ff9f32',desc:'Öffentliche Spiele und Experiences dieses Spielers.'},
 {id:'items',label:'Items',icon:'shirt',color:'#21bed0',badge:'cItems',desc:'Getragene Items mit Ersteller, Preis und Avatar-Maßen.'},
 {id:'groups',label:'Groups',icon:'group',color:'#438fff',badge:'cGroups',desc:'Gruppen, Rollen und Mitgliederzahlen.'},
 {id:'raw',label:'Raw data',icon:'code',color:'#8e909f',desc:''}
];
const ACC=[['Blue','#168df5'],['Violett','#bf5af2'],['Rosa','#ff4d78'],['Orange','#ff9f32'],['Grün','#30d158'],['Graphit','#8e8e93']];
const BACKGROUNDS=[
 {id:'aurora',name:'Aurora',baseDark:'#101321',baseLight:'#e8edf7',colors:['#548cff','#e989be','#46d7b2','#896aff']},
 {id:'ocean',name:'Ocean',baseDark:'#0c1724',baseLight:'#e6f0f7',colors:['#1e9fff','#29d4ce','#6588ff','#72e1ca']},
 {id:'sunset',name:'Sunset',baseDark:'#211321',baseLight:'#f5e9ec',colors:['#ff776d','#ffb24a','#e45f9a','#a963e8']},
 {id:'sakura',name:'Sakura',baseDark:'#1c1423',baseLight:'#f4eaf3',colors:['#ff8eb5','#bd8cff','#ffcfec','#788fff']},
 {id:'forest',name:'Forest',baseDark:'#101b1a',baseLight:'#e6f0e9',colors:['#42c883','#8ad26b','#43c7b8','#b1d66d']}
];

const win=$('#win');let moved=false;
const sideResize=$('#sideResize');
function setSidebarWidth(value,save=true){
 if(innerWidth<=760)return;
 const max=Math.max(220,Math.min(340,win.clientWidth-440));
 const width=Math.round(Math.max(168,Math.min(max,Number(value)||178)));
 document.documentElement.style.setProperty('--sidebar-width',width+'px');
 sideResize.setAttribute('aria-valuemax',String(Math.round(max)));
 sideResize.setAttribute('aria-valuenow',String(width));
 if(save)localStorage.setItem('af-sidebar-width',String(width));
}
sideResize.addEventListener('pointerdown',e=>{
 if(e.button!==0||innerWidth<=760)return;
 e.preventDefault();e.stopPropagation();
 const startX=e.clientX,startWidth=$('.side').getBoundingClientRect().width;
 sideResize.classList.add('dragging');sideResize.setPointerCapture(e.pointerId);
 const move=ev=>setSidebarWidth(startWidth+ev.clientX-startX);
 const up=()=>{sideResize.classList.remove('dragging');sideResize.removeEventListener('pointermove',move);sideResize.removeEventListener('pointerup',up)};
 sideResize.addEventListener('pointermove',move);sideResize.addEventListener('pointerup',up,{once:true});
});
sideResize.addEventListener('keydown',e=>{
 if(e.key==='ArrowLeft'||e.key==='ArrowRight'){
  e.preventDefault();const now=parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--sidebar-width'))||178;
  setSidebarWidth(now+(e.key==='ArrowRight'?12:-12));
 }
});
sideResize.addEventListener('dblclick',()=>{
 const now=parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--sidebar-width'))||178;
 setSidebarWidth(now<210?250:now<285?320:178);
});
const rlCache=new Map();
let searchToken=0;

function newExtra(){return{friends:undefined,followers:undefined,following:undefined,history:undefined,presence:undefined,groups:undefined,games:undefined}}
function newData(){return{userId:null,user:null,avatar:undefined,outfits:undefined,outfitsErr:null,retryOutfits:null,outfitThumbs:{},assetThumbs:{},gameThumbs:{},groupThumbs:{},assetMeta:{},extra:newExtra(),currentThumb:null,raw:{}}}
let data=newData();
function readFavorites(){try{const x=JSON.parse(localStorage.getItem('af-favorites')||'[]');return Array.isArray(x)?x:[]}catch(e){return[]}}
let favorites=readFavorites();
function saveFavorites(){try{localStorage.setItem('af-favorites',JSON.stringify(favorites))}catch(e){}renderFavorites();updateFavoriteButton();updBadges()}
function isFavorite(id){return favorites.some(x=>String(x.userId)===String(id))}

/* ---------- Sidebar + Dock ---------- */
$('#nav').innerHTML='<div id="sel"></div><div class="navlabel">Discover</div>'+TABS.map((t,i)=>`<div class="ni" data-tab="${t.id}" style="--i:${i}"><span class="ico" style="background:${t.color}">${ic(t.icon,15)}</span><span>${t.label}</span>${t.badge?`<span class="bd" id="${t.badge}"></span>`:''}</div>`).join('')+'<div class="navlabel navlabel-last">Try it</div><div class="navshortcuts"><button data-q="Roblox"><i></i>Roblox</button><button data-q="builderman"><i></i>builderman</button><button data-q="1"><i></i>ID 1</button></div>';
$('#dock').innerHTML=TABS.map(t=>`<div class="di" data-tab="${t.id}" style="background:${t.color}">${ic(t.icon,24)}<span class="tip">${t.label}</span></div>`).join('')+'<div class="dsep"></div><div class="di" id="dkTrash" style="background:#636366">'+ic('trash',24)+'<span class="tip">Verlauf &amp; Cache leeren</span></div>';
$('#swatches').innerHTML=`<div class="palette-block"><span class="palette-label">Accent</span><div class="palette-row">${ACC.map(a=>`<button class="sw" data-c="${a[1]}" title="${a[0]}" aria-label="${a[0]} accent color" style="background:${a[1]}"></button>`).join('')}</div></div><div class="palette-block"><span class="palette-label">Wallpaper</span><div class="wallpaper-grid">${BACKGROUNDS.map(b=>`<button class="wallpick" data-wall="${b.id}" title="${b.name}" aria-label="${b.name} wallpaper" style="--wall-a:${b.colors[0]};--wall-b:${b.colors[1]};--wall-c:${b.colors[2]};--wall-d:${b.colors[3]}"></button>`).join('')}</div></div>`;
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
function setTheme(d){document.documentElement.classList.toggle('dark',d);localStorage.setItem('af-theme',d?'dark':'light');const icon=ic(d?'sun':'moon',16);if($('#thBtn'))$('#thBtn').innerHTML=icon;if($('#toolbarTheme'))$('#toolbarTheme').innerHTML=icon}
function setAcc(c){document.documentElement.style.setProperty('--acc',c);localStorage.setItem('af-accent',c);$$('.sw').forEach(s=>s.classList.toggle('on',s.dataset.c===c))}
function setBackground(id){
 const bg=BACKGROUNDS.find(x=>x.id===id)||BACKGROUNDS[0],root=document.documentElement;
 root.dataset.wallpaper=bg.id;
 root.style.setProperty('--wall-base-dark',bg.baseDark);root.style.setProperty('--wall-base-light',bg.baseLight);
 ['a','b','c','d'].forEach((key,i)=>root.style.setProperty('--wall-'+key,bg.colors[i]));
 localStorage.setItem('af-wallpaper',bg.id);
 $$('.wallpick').forEach(b=>{const active=b.dataset.wall===bg.id;b.classList.toggle('on',active);b.setAttribute('aria-pressed',active?'true':'false')});
}
const savedTheme=localStorage.getItem('af-theme');
setTheme(savedTheme?savedTheme==='dark':false);
setAcc(localStorage.getItem('af-accent')||'#168df5');
setBackground(localStorage.getItem('af-wallpaper')||'aurora');
$('#thBtn').onclick=()=>setTheme(!document.documentElement.classList.contains('dark'));
$('#toolbarTheme').onclick=()=>setTheme(!document.documentElement.classList.contains('dark'));

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
function center(){
 if(win.classList.contains('zoom'))return;
 const maxLeft=Math.max(8,innerWidth-win.offsetWidth-8),maxTop=Math.max(36,innerHeight-win.offsetHeight-72);
 const oldLeft=parseFloat(win.style.left),oldTop=parseFloat(win.style.top);
 const left=moved&&Number.isFinite(oldLeft)?oldLeft:(innerWidth-win.offsetWidth)/2;
 const top=moved&&Number.isFinite(oldTop)?oldTop:46;
 win.style.left=Math.max(8,Math.min(maxLeft,left))+'px';
 win.style.top=Math.max(36,Math.min(maxTop,top))+'px';
}
function showWin(){win.classList.remove('hide')}
function toggleZoom(){
 const wasZoomed=win.classList.contains('zoom');
 win.classList.toggle('zoom',!wasZoomed);
 if(wasZoomed)setTimeout(center,430);
 setTimeout(moveSel,430);
}
$('#l1').onclick=$('#l2').onclick=()=>{win.classList.add('hide');notify('Fenster minimiert','Klick aufs Dock, um es zurückzuholen.')};
$('#l3').onclick=toggleZoom;
$('#sbBtn').onclick=()=>{win.classList.toggle('collapsed');setTimeout(moveSel,400)};
$('#tb').addEventListener('pointerdown',e=>{
 if(e.button!==0||e.target.closest('input,button,.pill,.iconbtn')||win.classList.contains('zoom'))return;
 const sx=e.clientX,sy=e.clientY,ox=win.offsetLeft,oy=win.offsetTop;
 let dragging=false;
 const mv=ev=>{
  const dx=ev.clientX-sx,dy=ev.clientY-sy;
  if(!dragging&&Math.hypot(dx,dy)<4)return;
  dragging=true;moved=true;win.classList.add('drag');
  const maxLeft=Math.max(8,innerWidth-win.offsetWidth-8),maxTop=Math.max(36,innerHeight-win.offsetHeight-72);
  win.style.left=Math.max(8,Math.min(maxLeft,ox+dx))+'px';
  win.style.top=Math.max(36,Math.min(maxTop,oy+dy))+'px';
 };
 const up=()=>{win.classList.remove('drag');removeEventListener('pointermove',mv);removeEventListener('pointerup',up)};
 addEventListener('pointermove',mv);addEventListener('pointerup',up,{once:true});
});
$('#tb').addEventListener('dblclick',e=>{if(!e.target.closest('input,button,.pill,.iconbtn'))toggleZoom()});
addEventListener('resize',()=>{if(innerWidth>760)setSidebarWidth(localStorage.getItem('af-sidebar-width')||getComputedStyle(document.documentElement).getPropertyValue('--sidebar-width')||178,false);center();moveSel()});

/* ---------- Tabs ---------- */
let cur='start';
function moveSel(){const a=$('.ni.act'),s=$('#sel');if(a&&s){s.style.transform='translateY('+a.offsetTop+'px)';s.style.height=a.offsetHeight+'px'}}
function showTab(id,direction){
 const content=$('#content'),changed=id!==cur;
 if(changed&&direction){
  content.classList.remove('scroll-next','scroll-prev');
  void content.offsetWidth;
  content.classList.add(direction>0?'scroll-next':'scroll-prev');
  setTimeout(()=>content.classList.remove('scroll-next','scroll-prev'),560);
 }
 cur=id;
 $$('.tab').forEach(s=>s.classList.toggle('show',s.id==='t-'+id));
 $$('[data-tab]').forEach(el=>{if(el.classList.contains('ni')||el.classList.contains('di'))el.classList.toggle('act',el.dataset.tab===id)});
 const t=TABS.find(x=>x.id===id);
 $('#ttl').textContent=t.label+(data.user&&id!=='start'?' · '+(data.user.displayName||data.user.name):'');
 moveSel();
 if(id==='raw')renderRaw();
 if(id==='favorites')renderFavorites();
 content.scrollTop=0;
}

/* Scroll-chaining: scroll a page normally, then glide to the next tab at its edge. */
let tabWheel=0,tabWheelLock=0;
const contentArea=$('#content');
function canScrollInDirection(target,delta){
 let node=target instanceof Element?target:null;
 while(node&&node!==contentArea){
  const style=getComputedStyle(node),scrollable=/(auto|scroll)/.test(style.overflowY)&&node.scrollHeight>node.clientHeight+2;
  if(scrollable){
   const atTop=node.scrollTop<=1,atBottom=node.scrollTop+node.clientHeight>=node.scrollHeight-2;
   if((delta<0&&!atTop)||(delta>0&&!atBottom))return true;
  }
  node=node.parentElement;
 }
 const atTop=contentArea.scrollTop<=1,atBottom=contentArea.scrollTop+contentArea.clientHeight>=contentArea.scrollHeight-2;
 return (delta<0&&!atTop)||(delta>0&&!atBottom);
}
contentArea.addEventListener('wheel',e=>{
 if(e.ctrlKey||e.metaKey||Math.abs(e.deltaY)<2||Math.abs(e.deltaX)>Math.abs(e.deltaY))return;
 if(e.target.closest('input,textarea,select,[contenteditable="true"],.sheet,.menu')||$('#sheetBg').classList.contains('show')||$('#alertBg').classList.contains('show'))return;
 if(Date.now()<tabWheelLock){e.preventDefault();return}
 if(canScrollInDirection(e.target,e.deltaY)){tabWheel=0;return}
 e.preventDefault();tabWheel+=e.deltaY;
 if(Math.abs(tabWheel)<54)return;
 const direction=tabWheel>0?1:-1,index=TABS.findIndex(t=>t.id===cur),next=TABS[index+direction];
 tabWheel=0;
 if(!next)return;
 tabWheelLock=Date.now()+620;
 showTab(next.id,direction);
},{passive:false});

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
 const created=u.created?new Date(u.created).toLocaleDateString('en-US',{month:'short',day:'numeric',year:'numeric'}):'–';
 el.innerHTML=`<div class="profile-dashboard">
  <section class="profile-cover card">
   <div class="avbox" id="avbox"></div>
   <div class="profile-identity">
    <div class="eyebrow">ROBLOX PROFILE</div>
    <h1>${esc(u.displayName||u.name)}${u.hasVerifiedBadge?'<span class="vb" title="Verified">✓</span>':''}</h1>
    <div class="sub profile-handle">@${esc(u.name)} <button class="chip" data-copy="${esc(id)}" title="Copy ID">ID ${esc(id)} ${ic('copy',12)}</button></div>
    <div class="profile-facts">
     <span class="fact-pill fact-pink"><i></i><b id="stOutfits">…</b> saved outfits</span>
     <span class="fact-pill fact-green"><i></i><b id="stFriends">…</b> friends</span>
     <span class="fact-pill fact-blue"><i></i><b id="stFollowers">…</b> followers</span>
     <span class="fact-pill fact-violet"><i></i>${esc(created)} joined</span>
    </div>
    <div class="profile-presence" id="pfPres"></div>
   </div>
   <div class="profile-actions">
    <a class="btn pri" target="_blank" rel="noopener" href="https://www.roblox.com/users/${esc(id)}/profile">${ic('ext',15)} View on Roblox</a>
    <button class="btn favorite-toggle" id="favoriteToggle" data-fav-current="1"></button>
    <div class="profile-tool-row">
     <button class="tool-btn" data-action="share-profile" title="Copy profile link" aria-label="Copy profile link">${ic('share',14)}</button>
     <button class="tool-btn" data-action="export-profile" title="Download profile JSON" aria-label="Download profile JSON">${ic('download',14)}</button>
     <button class="tool-btn" data-action="refresh-profile" title="Refresh profile data" aria-label="Refresh profile data">${ic('refresh',14)}</button>
     <button class="tool-btn" data-action="copy-display-name" title="Copy display name" aria-label="Copy display name">${ic('copy',14)}</button>
    </div>
   </div>
   ${u.description?`<p class="profile-description">${esc(u.description)}</p>`:''}
   <div id="pfHist" class="profile-history"></div>
   ${u.isBanned?'<span class="tag red">Account banned</span>':''}
  </section>
  <nav class="profile-tabs" aria-label="Profile sections">
   <button class="profile-tab active" data-tab="profil">${ic('user',14)} Overview</button>
   <button class="profile-tab" data-tab="outfits">${ic('layers',14)} Saved outfits <span id="ovOutfitBadge">–</span></button>
   <button class="profile-tab" data-tab="games">${ic('game',14)} Store looks <span id="ovGamesBadge">–</span></button>
   <button class="profile-tab" data-tab="items">${ic('shirt',14)} Items <span id="ovItemsBadge">–</span></button>
   <button class="profile-tab" data-tab="raw">${ic('code',14)} Raw data</button>
  </nav>
  <section class="overview-panel card">
   <div class="section-heading"><h2><span class="section-icon pink">${ic('layers',14)}</span>Saved outfits <span class="heading-count" id="ovOutfitHeading">—</span></h2><button class="text-action" data-tab="outfits">View all ${ic('ext',13)}</button></div>
   <div class="outfit-carousel" id="ovOutfits"></div>
  </section>
  <section class="overview-lower">
   <div class="avatar-panel card">
    <div class="section-heading"><h2><span class="live-dot"></span>Avatar preview</h2><span class="live-label">LIVE</span></div>
    <div class="avatar-stage" id="ovAvatar"></div>
    <div class="avatar-caption">Current avatar appearance</div>
   </div>
   <div class="wearing-panel card">
    <div class="section-heading"><h2><span class="section-icon cyan">${ic('shirt',14)}</span>Wearing now</h2><button class="text-action" data-tab="items">All items ${ic('ext',13)}</button></div>
    <div class="wearing-list" id="ovWearing"></div>
   </div>
  </section>
 </div>`;
 setProfImg();updateFavoriteButton();updExtra();renderOverviewSections();
}
function updateFavoriteButton(){
 const b=$('#favoriteToggle');if(!b||!data.user)return;
 const saved=isFavorite(data.userId);b.classList.toggle('saved',saved);
 b.innerHTML=`${ic('star',14)} ${saved?'Saved to favorites':'Add to favorites'}`;
 b.setAttribute('aria-pressed',saved?'true':'false');
}
function toggleCurrentFavorite(){
 if(!data.user)return;
 const id=String(data.userId),index=favorites.findIndex(x=>String(x.userId)===id);
 if(index>=0){favorites.splice(index,1);notify('Removed from favorites',data.user.displayName||data.user.name)}
 else{favorites.unshift({userId:id,username:data.user.name,displayName:data.user.displayName||data.user.name,avatarUrl:data.currentThumb||''});notify('Added to favorites',data.user.displayName||data.user.name)}
 saveFavorites();
}
function removeFavorite(id){favorites=favorites.filter(x=>String(x.userId)!==String(id));saveFavorites();notify('Favorite removed','The profile was removed from your saved list.')}
function renderFavorites(){
 const el=$('#t-favorites');if(!el)return;
 if(!favorites.length){el.innerHTML=`<div class="favorites-empty card"><div class="favorite-empty-icon">${ic('star',25)}</div><h2>Your favorites live here</h2><p>Save profiles with the star button. They stay in this browser for next time.</p><button class="btn pri" data-tab="start">Find a profile</button></div>`;return}
 el.innerHTML=`<div class="favorites-head"><div><div class="eyebrow">YOUR COLLECTION</div><h2>Favorite profiles <span>${favorites.length}</span></h2></div><button class="btn" data-action="clear-favorites">Clear all</button></div><div class="grid favorites-grid">${favorites.map((f,i)=>`<article class="favorite-card card" data-q="${esc(f.userId)}" style="--i:${i}"><div class="favorite-avatar">${f.avatarUrl?`<img src="${esc(f.avatarUrl)}" loading="lazy" alt="">`:'<span class="ph"></span>'}</div><div class="favorite-copy"><b>${esc(f.displayName||f.username)}</b><span>@${esc(f.username)} · ID ${esc(f.userId)}</span></div><button class="favorite-remove" data-unfavorite="${esc(f.userId)}" title="Remove favorite" aria-label="Remove favorite">${ic('x',14)}</button></article>`).join('')}</div>`;
}
function setProfImg(){
 const b=$('#avbox');if(b)b.innerHTML=data.currentThumb?`<img src="${esc(data.currentThumb)}" alt="${esc(data.user&&data.user.displayName||'Avatar')}">`:'<div class="ph"></div>';
 renderOverviewSections();
}
function renderOverviewSections(){
 const outfitBox=$('#ovOutfits'),wearBox=$('#ovWearing'),avatarBox=$('#ovAvatar');
 if(!outfitBox||!wearBox||!avatarBox)return;
 const outfits=data.outfits, assets=data.avatar&&data.avatar.assets||[];
 const countText=Array.isArray(outfits)?outfits.length:(outfits===null?'!':'…');
 ['ovOutfitBadge','ovOutfitHeading'].forEach(id=>{const n=document.getElementById(id);if(n)n.textContent=countText});
 const gamesBadge=$('#ovGamesBadge');if(gamesBadge)gamesBadge.textContent=Array.isArray(data.extra.games)?data.extra.games.length:'…';
 const itemsBadge=$('#ovItemsBadge');if(itemsBadge)itemsBadge.textContent=data.avatar&&Array.isArray(data.avatar.assets)?assets.length:'…';
 if(outfits===undefined){outfitBox.innerHTML='<div class="overview-state"><span class="spinner"></span>Loading saved outfits…</div>'}
 else if(outfits===null){outfitBox.innerHTML='<div class="overview-state">Saved outfits could not be loaded.</div>'}
 else if(!outfits.length){outfitBox.innerHTML='<div class="overview-state">No public saved outfits for this player.</div>'}
 else outfitBox.innerHTML=outfits.slice(0,8).map((o,i)=>{
  const th=data.outfitThumbs[o.id];
  return `<article class="overview-outfit card clk" data-outfit="${esc(o.id)}" style="--i:${i}"><div class="outfit-visual">${th?`<img src="${esc(th)}" loading="lazy" alt="">`:'<div class="ph"></div>'}<span class="outfit-label">SAVED OUTFIT</span></div><div class="overview-card-copy"><b title="${esc(o.name)}">${esc(o.name)}</b><div class="overview-card-id"><span>${esc(o.id)}</span><button data-copy="${esc(o.id)}" title="Copy outfit ID">${ic('copy',12)}</button></div></div></article>`
 }).join('');
 avatarBox.innerHTML=data.currentThumb?`<img src="${esc(data.currentThumb)}" alt="Current Roblox avatar">`:'<div class="ph"></div>';
 if(data.avatar===undefined){wearBox.innerHTML='<div class="overview-state"><span class="spinner"></span>Loading avatar items…</div>'}
 else if(data.avatar===null){wearBox.innerHTML='<div class="overview-state">Avatar items are unavailable.</div>'}
 else if(!assets.length){wearBox.innerHTML='<div class="overview-state">No avatar items to show.</div>'}
 else wearBox.innerHTML=assets.slice(0,7).map(x=>{
  const th=data.assetThumbs[x.id],type=x.assetType&&x.assetType.name||'Item';
  return `<div class="wearing-row"><div class="wearing-thumb">${th?`<img src="${esc(th)}" loading="lazy" alt="">`:'<div class="ph"></div>'}</div><div class="wearing-copy"><b title="${esc(x.name)}">${esc(x.name)}</b><span>${esc(type)}</span></div><button class="wearing-id" data-copy="${esc(x.id)}" title="Copy item ID">${esc(x.id)}</button></div>`
 }).join('');
}
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
 renderOverviewSections();
}
function updBadges(){
 const m={cItems:data.avatar&&data.avatar.assets,cOutfits:data.outfits,cGames:data.extra.games,cGroups:data.extra.groups,cFavorites:favorites};
 for(const k in m){const el=document.getElementById(k);if(!el)continue;const v=m[k];if(Array.isArray(v)&&(k==='cFavorites'||data.user)){el.textContent=v.length;el.style.display='block'}else el.style.display='none'}
}
const collectionPrefs={
 items:{query:'',sort:'name-asc',layout:'grid'},outfits:{query:'',sort:'name-asc',layout:'grid'},
 games:{query:'',sort:'visits-desc',layout:'grid'},groups:{query:'',sort:'members-desc',layout:'grid'}
};
function collectionTools(key,options){
 const p=collectionPrefs[key];
 return `<div class="collection-tools"><label class="collection-search"><span class="sr-only">Filter ${key}</span>${ic('search',14)}<input type="search" data-collection-filter="${key}" value="${esc(p.query)}" placeholder="Filter ${key}…"></label><select data-collection-sort="${key}" aria-label="Sort ${key}">${options.map(o=>`<option value="${o[0]}"${p.sort===o[0]?' selected':''}>${o[1]}</option>`).join('')}</select><button class="tool-btn" data-layout-toggle="${key}" title="Toggle grid or list view" aria-label="Toggle view">${ic(p.layout==='grid'?'list':'grid',15)}</button>${key==='items'?`<button class="btn export-csv" data-action="export-items-csv">${ic('download',14)} CSV</button>`:''}</div>`;
}
function applyCollectionTools(key){
 const section=$('#t-'+key),p=collectionPrefs[key];if(!section||!p)return;
 const grid=section.querySelector('.collection-grid');if(!grid)return;
 grid.classList.toggle('list-layout',p.layout==='list');
 const cards=[...grid.querySelectorAll('[data-list-item]')],q=p.query.toLowerCase().trim();
 cards.forEach(c=>c.style.display=(!q||(c.dataset.search||'').toLowerCase().includes(q))?'':'none');
 const sorters={
  'name-asc':(a,b)=>(a.dataset.name||'').localeCompare(b.dataset.name||''),
  'name-desc':(a,b)=>(b.dataset.name||'').localeCompare(a.dataset.name||''),
  'type-asc':(a,b)=>(a.dataset.kind||'').localeCompare(b.dataset.kind||'')||(a.dataset.name||'').localeCompare(b.dataset.name||''),
  'id-desc':(a,b)=>(Number(b.dataset.id)||0)-(Number(a.dataset.id)||0),
  'visits-desc':(a,b)=>(Number(b.dataset.visits)||0)-(Number(a.dataset.visits)||0),
  'members-desc':(a,b)=>(Number(b.dataset.members)||0)-(Number(a.dataset.members)||0)
 };
 cards.sort(sorters[p.sort]||sorters['name-asc']).forEach(c=>grid.appendChild(c));
 let empty=section.querySelector('.filter-empty');
 if(q&&!cards.some(c=>c.style.display!=='none')){if(!empty){empty=document.createElement('div');empty.className='filter-empty';empty.textContent='No matches found.';grid.after(empty)}empty.hidden=false}
 else if(empty)empty.hidden=true;
 const toggle=section.querySelector(`[data-layout-toggle="${key}"]`);if(toggle)toggle.innerHTML=ic(p.layout==='grid'?'list':'grid',15);
}
function itemCard(x,i){
 const th=data.assetThumbs[x.id],name=x.name||'Item',type=x.assetType&&x.assetType.name||'Item';
 return `<div class="card" data-list-item="1" data-id="${esc(x.id)}" data-name="${esc(name)}" data-kind="${esc(type)}" data-search="${esc(name+' '+type+' '+x.id)}" style="--i:${Math.min(i,14)}"><div class="th" data-aid="${x.id}">${th?`<img src="${esc(th)}" loading="lazy" alt="">`:'<div class="ph"></div>'}</div><div class="cb"><div class="nm" title="${esc(name)}">${esc(name)}</div><div class="ty">${esc(type)}</div><div class="meta" data-meta="${x.id}"></div><div class="row"><button class="chip" data-copy="${x.id}" title="ID kopieren">${x.id} ${ic('copy',11)}</button><a class="lnk" target="_blank" rel="noopener" href="https://www.roblox.com/catalog/${x.id}" title="Im Katalog öffnen">${ic('ext',14)}</a></div></div></div>`;
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
 if(a===undefined){el.innerHTML=loadingState('Lade Items …');renderOverviewSections();return}
 if(a===null){el.innerHTML=errState('Items konnten nicht geladen werden.');renderOverviewSections();return}
 const list=a.assets||[],sc=a.scales||{};
 const info=[['Typ',a.playerAvatarType],['Größe',pc(sc.height)],['Breite',pc(sc.width)],['Kopf',pc(sc.head)],['Tiefe',pc(sc.depth)],['Proportion',pc(sc.proportion)]].filter(x=>x[1]);
 el.innerHTML=(info.length?`<div class="pills" style="margin-bottom:16px">${info.map(x=>`<span class="tag">${x[0]}: <b>${esc(x[1])}</b></span>`).join('')}</div>`:'')+
  (list.length?`${collectionTools('items',[['name-asc','Name A–Z'],['name-desc','Name Z–A'],['type-asc','Item type'],['id-desc','Newest ID']])}<div class="grid collection-grid">${list.map(itemCard).join('')}</div>`:emptyState('Dieser Avatar trägt keine Items.'));
 applyMeta();applyCollectionTools('items');renderOverviewSections();
}
function renderOutfits(){
 const el=$('#t-outfits');
 if(!data.user){el.innerHTML=needUser();return}
 const o=data.outfits;
 if(o===undefined){el.innerHTML=loadingState('Lade Outfits …');renderOverviewSections();return}
 if(o===null){
  const s=data.outfitsErr&&data.outfitsErr.status;
  const why=(s===401||s===403||s===400)?'Die Outfits dieses Spielers sind privat oder nur für Freunde sichtbar. Roblox zeigt sie nicht öffentlich.'
   :s===404?'Roblox hat für dieses Profil keine Outfits gefunden.'
   :s===429?'Roblox begrenzt gerade die Outfit-Vorschau. Kurz warten und nochmal versuchen.'
   :'Die Outfits konnten gerade nicht von Roblox geladen werden.';
  el.innerHTML=`<div class="state">${ic('warn',34)}<p>${esc(why)}${s?' (Code '+s+')':''}</p><button class="btn pri" id="outRetry">Nochmal versuchen</button></div>`;
  $('#outRetry').onclick=()=>data.retryOutfits&&data.retryOutfits();
  renderOverviewSections();return;
 }
 if(!o.length){el.innerHTML=emptyState('Keine gespeicherten Outfits.');renderOverviewSections();return}
 el.innerHTML=`${collectionTools('outfits',[['name-asc','Name A–Z'],['name-desc','Name Z–A'],['id-desc','Newest ID']])}<div class="grid collection-grid">${o.map((x,i)=>{const th=data.outfitThumbs[x.id];return `<div class="card clk" data-list-item="1" data-name="${esc(x.name||'')}" data-search="${esc((x.name||'')+' '+x.id)}" data-id="${esc(x.id)}" data-outfit="${x.id}" style="--i:${Math.min(i,14)}"><div class="th">${th?`<img src="${esc(th)}" loading="lazy" alt="">`:'<div class="ph"></div>'}</div><div class="cb"><div class="nm" title="${esc(x.name)}">${esc(x.name)}</div><div class="ty">Klicken für Items</div><div class="row"><button class="chip" data-copy="${x.id}" title="ID kopieren">${x.id} ${ic('copy',11)}</button></div></div></div>`}).join('')}</div>`;
 applyCollectionTools('outfits');renderOverviewSections();
}
function renderGames(){
 const el=$('#t-games');
 if(!data.user){el.innerHTML=needUser();return}
 const g=data.extra.games;
 if(g===undefined){el.innerHTML=loadingState('Lade Spiele …');return}
 if(g===null){el.innerHTML=errState('Spiele konnten nicht geladen werden.');return}
 if(!g.length){el.innerHTML=emptyState('Keine öffentlichen Spiele.');return}
 el.innerHTML=`${collectionTools('games',[['visits-desc','Most visited'],['name-asc','Name A–Z'],['name-desc','Name Z–A']])}<div class="grid w collection-grid">${g.map((x,i)=>{
  const th=data.gameThumbs[x.id],pid=x.rootPlace&&x.rootPlace.id,yr=x.created?new Date(x.created).getFullYear():'';
  return `<div class="card rowcard" data-list-item="1" data-name="${esc(x.name||'')}" data-search="${esc((x.name||'')+' '+x.id)}" data-visits="${Number(x.placeVisits)||0}" style="--i:${Math.min(i,14)}"><div class="gi">${th?`<img src="${esc(th)}" loading="lazy" alt="">`:'<div class="ph"></div>'}</div><div class="gb"><div class="nm" title="${esc(x.name)}">${esc(x.name)}</div><div class="ty">${(x.placeVisits||0).toLocaleString('de-DE')} Besuche${yr?' · seit '+yr:''}</div><div class="row">${pid?`<button class="chip" data-copy="${pid}" title="Place-ID kopieren">${pid} ${ic('copy',11)}</button><a class="lnk" target="_blank" rel="noopener" href="https://www.roblox.com/games/${pid}">${ic('ext',14)}</a>`:''}</div></div></div>`}).join('')}</div>`;
 applyCollectionTools('games');
}
function renderGroups(){
 const el=$('#t-groups');
 if(!data.user){el.innerHTML=needUser();return}
 const g=data.extra.groups;
 if(g===undefined){el.innerHTML=loadingState('Lade Gruppen …');return}
 if(g===null){el.innerHTML=errState('Gruppen konnten nicht geladen werden.');return}
 if(!g.length){el.innerHTML=emptyState('In keiner Gruppe.');return}
 el.innerHTML=`${collectionTools('groups',[['members-desc','Most members'],['name-asc','Name A–Z'],['name-desc','Name Z–A']])}<div class="grid w collection-grid">${g.map((x,i)=>{
  const gr=x.group,th=data.groupThumbs[gr.id];
  return `<div class="card rowcard" data-list-item="1" data-name="${esc(gr.name||'')}" data-search="${esc((gr.name||'')+' '+(x.role.name||'')+' '+gr.id)}" data-members="${Number(gr.memberCount)||0}" style="--i:${Math.min(i,14)}"><div class="gi">${th?`<img src="${esc(th)}" loading="lazy" alt="">`:'<div class="ph"></div>'}</div><div class="gb"><div class="nm" title="${esc(gr.name)}">${esc(gr.name)}</div><div class="ty">${esc(x.role.name)} · ${(gr.memberCount||0).toLocaleString('de-DE')} Mitglieder</div><div class="row"><button class="chip" data-copy="${gr.id}" title="ID kopieren">${gr.id} ${ic('copy',11)}</button><a class="lnk" target="_blank" rel="noopener" href="https://www.roblox.com/groups/${gr.id}">${ic('ext',14)}</a></div></div></div>`}).join('')}</div>`;
 applyCollectionTools('groups');
}
function renderRaw(){
 const el=$('#t-raw');
 if(!data.user){el.innerHTML=needUser();return}
 const j=JSON.stringify(data.raw,null,2);
 el.innerHTML=`<div class="acts" style="margin:0 0 14px"><button class="btn" id="rawCopy">${ic('copy',15)} JSON kopieren</button></div><pre class="raw">${esc(j)}</pre>`;
 $('#rawCopy').onclick=()=>copyText(j);
}
function renderAll(){renderProfile();renderFavorites();renderItems();renderOutfits();renderGames();renderGroups();updBadges()}

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
 thumbMap(AV_TH,[uid],tok).then(m=>{if(!ok())return;D.currentThumb=m[uid]||null;if(isFavorite(uid)&&D.currentThumb){const f=favorites.find(x=>String(x.userId)===String(uid));if(f){f.avatarUrl=D.currentThumb;saveFavorites()}}setProfImg();recentSave()});

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
 $('#recentList').innerHTML=recent.map(r=>`<div class="rchip"><button class="rchip-open" data-q="${esc(r.userId)}">${r.avatarUrl?`<img src="${esc(r.avatarUrl)}" alt="">`:'<span class="ph2"></span>'}<span><b>${esc(r.displayName||r.username)}</b> <i>@${esc(r.username)}</i></span></button><button class="recent-remove" data-remove-recent="${esc(r.userId)}" title="Remove from history" aria-label="Remove ${esc(r.username)} from search history">${ic('x',11)}</button></div>`).join('');
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
async function recentRemove(id){
 try{await fetch('/x/recent',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({removeUserId:id})})}catch(e){}
 recent=recent.filter(r=>String(r.userId)!==String(id));renderRecent();notify('Removed from history','The profile was removed from recent searches.');
}
function downloadBlob(name,content,type){const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([content],{type}));a.download=name;document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(a.href),1000)}
function exportProfile(){if(!data.user)return;const payload={user:data.user,userId:data.userId,avatar:data.avatar,outfits:data.outfits,counts:data.extra,raw:data.raw,exportedAt:new Date().toISOString()};downloadBlob(`roblox-profile-${data.userId}.json`,JSON.stringify(payload,null,2),'application/json');notify('Profile exported','JSON download started.')}
function exportItemsCsv(){const rows=[['id','name','type'],...(data.avatar?.assets||[]).map(x=>[x.id,x.name||'',x.assetType?.name||''])];const csv=rows.map(r=>r.map(v=>'"'+String(v).replaceAll('"','""')+'"').join(',')).join('\r\n');downloadBlob(`roblox-items-${data.userId||'avatar'}.csv`,csv,'text/csv;charset=utf-8');notify('CSV exported','Avatar items downloaded.')}
async function shareProfile(){if(!data.user)return;const url=`https://www.roblox.com/users/${data.userId}/profile`;if(navigator.share){try{await navigator.share({title:`${data.user.displayName||data.user.name} on Roblox`,url})}catch(e){}}else{await copyText(url)}}
$('#recClear').onclick=clearRecent;

/* ---------- Events ---------- */
document.addEventListener('click',e=>{
 const mb=e.target.closest('.mb');
 if(mb){openMenu===mb.dataset.m?closeM():openM(mb.dataset.m,mb);return}
 const mi=e.target.closest('.mi');
 if(mi){const it=$('#menu')._items[+mi.dataset.i];closeM();if(it&&it[1])it[1]();return}
 closeM();
 if(e.target.id==='sheetBg'||e.target.closest('[data-close]')){closeSheet();return}
 const recentDel=e.target.closest('[data-remove-recent]');if(recentDel){recentRemove(recentDel.dataset.removeRecent);return}
 const unfav=e.target.closest('[data-unfavorite]');if(unfav){e.stopPropagation();removeFavorite(unfav.dataset.unfavorite);return}
 const action=e.target.closest('[data-action]')?.dataset.action;
 if(action==='clear-favorites'){favorites=[];saveFavorites();notify('Favorites cleared','Your saved profile list is empty.');return}
 if(action==='share-profile'){shareProfile();return}
 if(action==='export-profile'){exportProfile();return}
 if(action==='refresh-profile'){if(data.user){rlCache.clear();search(String(data.userId))}return}
 if(action==='copy-display-name'){if(data.user)copyText(data.user.displayName||data.user.name);return}
 if(action==='export-items-csv'){exportItemsCsv();return}
 if(e.target.closest('#favoriteToggle')){toggleCurrentFavorite();return}
 const layout=e.target.closest('[data-layout-toggle]');if(layout){const key=layout.dataset.layoutToggle;collectionPrefs[key].layout=collectionPrefs[key].layout==='grid'?'list':'grid';applyCollectionTools(key);return}
 const sw=e.target.closest('.sw');if(sw){setAcc(sw.dataset.c);return}
 const wall=e.target.closest('.wallpick');if(wall){setBackground(wall.dataset.wall);return}
 const cp=e.target.closest('[data-copy]');if(cp){copyText(cp.dataset.copy);return}
 const oc=e.target.closest('[data-outfit]');if(oc){openOutfit(oc.dataset.outfit);return}
 const q=e.target.closest('[data-q]');if(q){showWin();search(q.dataset.q);return}
 const tb=e.target.closest('[data-tab]');if(tb){showWin();showTab(tb.dataset.tab);return}
});
document.addEventListener('input',e=>{const f=e.target.closest('[data-collection-filter]');if(!f)return;const key=f.dataset.collectionFilter;collectionPrefs[key].query=f.value;applyCollectionTools(key)});
document.addEventListener('change',e=>{const s=e.target.closest('[data-collection-sort]');if(!s)return;const key=s.dataset.collectionSort;collectionPrefs[key].sort=s.value;applyCollectionTools(key)});
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
const cursorAura=$('#cursorAura');
if(cursorAura&&matchMedia('(pointer:fine)').matches){
 let x=innerWidth/2,y=innerHeight/2,tx=x,ty=y,frame=0;
 const animateCursor=()=>{
  x+=(tx-x)*.22;y+=(ty-y)*.22;
  cursorAura.style.transform=`translate3d(${x}px,${y}px,0) translate(-50%,-50%)`;
  if(Math.abs(tx-x)+Math.abs(ty-y)>.4)frame=requestAnimationFrame(animateCursor);else frame=0;
 };
 addEventListener('pointermove',e=>{
  if(e.pointerType==='touch')return;
  tx=e.clientX;ty=e.clientY;cursorAura.classList.add('visible');
  if(!frame)frame=requestAnimationFrame(animateCursor);
 });
 document.addEventListener('pointerdown',e=>{
  if(e.pointerType==='touch')return;
  const ripple=document.createElement('span');ripple.className='cursor-ripple';
  ripple.style.left=e.clientX+'px';ripple.style.top=e.clientY+'px';
  document.body.appendChild(ripple);setTimeout(()=>ripple.remove(),700);
 });
 document.documentElement.addEventListener('mouseleave',()=>cursorAura.classList.remove('visible'));
}

/* ---------- Start ---------- */
function clock(){$('#clock').textContent=new Date().toLocaleString('de-DE',{weekday:'short',day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'})}
if(innerWidth>760)setSidebarWidth(localStorage.getItem('af-sidebar-width')||178,false);
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
