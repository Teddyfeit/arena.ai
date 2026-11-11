#!/usr/bin/env python3
"""Fieldkit — one local tool hub for the Roblox Avatar Finder and Discord Name Radar.

Run: python tool_hub.py
Preview: python tool_hub.py --host 0.0.0.0 --port 8002 --no-browser
Everything uses the Python standard library. No Discord token, login, or automation is used.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import test as avatar_app

ROOT = Path(__file__).resolve().parent
WATCH_FILE = ROOT / "discord_watchlist.json"
WATCH_LOCK = threading.Lock()
MAX_BODY = 2 * 1024 * 1024
NAME_RE = re.compile(r"^[a-z0-9._]{2,32}$")
STATUSES = {"watching", "available", "taken", "parked"}
PRIORITIES = {"low", "normal", "high"}
INTERVALS = {5, 10, 15, 30, 60}


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_watchlist() -> dict:
    return {
        "version": 1,
        "names": [],
        "settings": {"checkInterval": 15, "desktopNotifications": False, "sound": False, "notifyOnAvailable": True, "remindOnlyWatching": True, "compact": False, "confirmDelete": True, "copyFormat": "plain", "quietStart": "22:00", "quietEnd": "08:00", "sort": "priority"},
        "events": [],
        "savedAt": stamp(),
    }


def valid_name(value: object) -> str:
    name = str(value or "").strip().lower()
    if not NAME_RE.fullmatch(name) or name.startswith(".") or name.endswith(".") or ".." in name:
        raise ValueError("Use 2–32 lowercase letters, numbers, underscores or dots; dots cannot repeat or be at either end.")
    return name


def clean_watchlist(value: object) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("names"), list):
        raise ValueError("Expected a watchlist with a names array.")
    if len(value["names"]) > 500:
        raise ValueError("The watchlist is limited to 500 candidates.")
    names, seen = [], set()
    for row in value["names"]:
        if not isinstance(row, dict):
            continue
        try:
            name = valid_name(row.get("name"))
        except ValueError:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        status = str(row.get("status", "watching"))
        priority = str(row.get("priority", "normal"))
        names.append({
            "id": str(row.get("id") or f"name-{len(names)+1}")[:48],
            "name": name,
            "status": status if status in STATUSES else "watching",
            "priority": priority if priority in PRIORITIES else "normal",
            "notes": str(row.get("notes", ""))[:500],
            "createdAt": str(row.get("createdAt") or stamp())[:40],
            "updatedAt": str(row.get("updatedAt") or stamp())[:40],
        })
    source_settings = value.get("settings") if isinstance(value.get("settings"), dict) else {}
    try:
        interval = int(source_settings.get("checkInterval", 15))
    except (TypeError, ValueError):
        interval = 15
    def clock_value(key: str, fallback: str) -> str:
        candidate = str(source_settings.get(key, fallback))
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", candidate):
            return fallback
        return candidate
    settings = {
        "checkInterval": interval if interval in INTERVALS else 15,
        "desktopNotifications": source_settings.get("desktopNotifications") is True,
        "sound": source_settings.get("sound") is True,
        "notifyOnAvailable": source_settings.get("notifyOnAvailable", True) is True,
        "remindOnlyWatching": source_settings.get("remindOnlyWatching", True) is True,
        "compact": source_settings.get("compact") is True,
        "confirmDelete": source_settings.get("confirmDelete", True) is True,
        "copyFormat": source_settings.get("copyFormat") if source_settings.get("copyFormat") in {"plain", "mention"} else "plain",
        "quietStart": clock_value("quietStart", "22:00"),
        "quietEnd": clock_value("quietEnd", "08:00"),
        "sort": source_settings.get("sort") if source_settings.get("sort") in {"priority", "name", "recent"} else "priority",
    }
    events = value.get("events", [])
    if not isinstance(events, list):
        events = []
    cleaned_events = [
        {"at": str(event.get("at") or stamp())[:40], "text": str(event.get("text", ""))[:200]}
        for event in events[-60:] if isinstance(event, dict)
    ]
    return {"version": 1, "names": names, "settings": settings, "events": cleaned_events, "savedAt": str(value.get("savedAt") or stamp())[:40]}


def load_watchlist() -> dict:
    with WATCH_LOCK:
        try:
            return clean_watchlist(json.loads(WATCH_FILE.read_text(encoding="utf-8")))
        except (OSError, ValueError, json.JSONDecodeError):
            return default_watchlist()


def save_watchlist(value: dict) -> dict:
    data = clean_watchlist(value)
    data["savedAt"] = stamp()
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    with WATCH_LOCK:
        fd, tmp = tempfile.mkstemp(prefix=".discord-watch-", suffix=".tmp", dir=ROOT)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, WATCH_FILE)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    return data


HUB_HTML = r"""<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#090806"><title>FIELDKIT — Tool desk</title>
<style>
:root{color-scheme:dark;--bg:#090806;--panel:#11100e;--line:rgba(229,178,101,.17);--gold:#efbb68;--text:#eee4d1;--muted:#96866d;--mono:ui-monospace,Consolas,monospace;--sans:Inter,system-ui,-apple-system,"Segoe UI",sans-serif}*{box-sizing:border-box}html,body{margin:0;min-height:100%;background:var(--bg);color:var(--text);font:14px var(--sans)}body{min-height:100vh;overflow-x:hidden;background:radial-gradient(ellipse at 50% 32%,rgba(119,69,23,.2),transparent 48%),linear-gradient(135deg,#0f0c09,#080807 70%)}button,a{font:inherit;color:inherit}a{text-decoration:none}.grain{position:fixed;inset:0;pointer-events:none;opacity:.12;background-image:radial-gradient(rgba(238,194,129,.27) .55px,transparent .8px);background-size:7px 7px;mask-image:linear-gradient(transparent,#000 20%,#000 85%,transparent)}.wrap{position:relative;z-index:1;width:min(1100px,calc(100% - 48px));margin:auto}.top{height:66px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--line)}.brand{display:flex;align-items:center;gap:10px}.logo{width:31px;height:31px;display:grid;place-items:center;border:1px solid #a5763b;border-radius:9px;color:#f7ca7c;background:rgba(159,101,38,.13);box-shadow:0 0 22px #bf7d301f}.logo svg{width:18px}.brand b{font:700 12px var(--mono);letter-spacing:.2em;color:#ecd6ae}.brand small{display:block;margin-top:2px;color:#78684f;font:8px var(--mono);letter-spacing:.13em}.local{padding:6px 9px;border:1px solid rgba(225,174,95,.18);border-radius:4px;color:#bb9d67;font:8px var(--mono);letter-spacing:.09em}.local i{display:inline-block;width:5px;height:5px;margin-right:6px;border-radius:50%;background:#c69855;box-shadow:0 0 7px #d9a75c}.hero{padding:75px 0 36px;text-align:center}.eyebrow{color:#bd9356;font:9px var(--mono);letter-spacing:.2em}.hero h1{margin:14px 0 10px;font-size:clamp(33px,6vw,60px);line-height:1.03;letter-spacing:-.055em;font-weight:600;color:#f0e2c9}.hero h1 span{color:#d9a558}.hero p{max-width:540px;margin:0 auto;color:#a3957b;font-size:13px;line-height:1.75}.quick{display:flex;align-items:center;justify-content:center;gap:7px;margin-top:22px}.quick span{color:#77664c;font:8px var(--mono)}.quick kbd{padding:4px 6px;border:1px solid #52422d;border-radius:4px;background:#15110d;color:#b99d6c;font:8px var(--mono)}.tools-head{display:flex;align-items:end;justify-content:space-between;margin:16px 2px 11px}.tools-head h2{margin:0;color:#e0cfaf;font:10px var(--mono);letter-spacing:.13em}.tools-head span{color:#76674f;font:8px var(--mono)}.cards{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:13px}.tool-card{position:relative;min-height:258px;overflow:hidden;padding:18px;border:1px solid rgba(220,170,98,.2);border-radius:9px;background:linear-gradient(140deg,rgba(28,23,16,.95),rgba(13,12,10,.96));box-shadow:inset 0 1px #ffffff08,0 15px 40px #0004;transition:transform .25s,border-color .25s,box-shadow .25s}.tool-card:hover{transform:translateY(-4px);border-color:rgba(234,183,105,.52);box-shadow:inset 0 1px #ffffff0e,0 19px 50px #0007,0 0 30px #b4792711}.card-top{display:flex;justify-content:space-between;align-items:start}.tool-icon{width:38px;height:38px;display:grid;place-items:center;border:1px solid #8f6736;border-radius:9px;color:#f2c878;background:#a2632017}.tool-icon svg{width:21px;height:21px}.tool-label{color:#8d7958;font:8px var(--mono);letter-spacing:.14em}.tool-card h3{margin:17px 0 6px;color:#eddfc7;font:500 17px var(--mono)}.tool-card p{max-width:440px;margin:0;color:#9b8d75;font-size:11px;line-height:1.7}.card-bottom{position:absolute;right:18px;bottom:17px;left:18px;display:flex;align-items:center;justify-content:space-between;padding-top:12px;border-top:1px solid rgba(225,174,95,.12)}.card-meta{color:#76664d;font:8px var(--mono)}.launch{display:flex;align-items:center;gap:7px;color:#e3b96f;font:9px var(--mono);letter-spacing:.07em}.launch svg{width:13px;transition:transform .2s}.tool-card:hover .launch svg{transform:translateX(4px)}.preview{position:absolute;right:14px;top:69px;width:39%;height:113px;opacity:.85;pointer-events:none}.preview svg{width:100%;height:100%;overflow:visible}.preview-line{stroke:#b77f36;stroke-opacity:.42;stroke-width:.65}.preview-dot{fill:#e9b761;filter:drop-shadow(0 0 4px #e6a547)}.preview-dot.cool{fill:#77c9c3;filter:drop-shadow(0 0 4px #77c9c3)}.preview-core{fill:#ffc66b;filter:drop-shadow(0 0 9px #efa53c)}.discord-card .tool-icon{color:#a8baff;border-color:#586496;background:#58649620}.discord-card .launch{color:#b9c4ff}.discord-card:hover{border-color:#858fc766}.discord-mark{font:700 20px var(--sans)}.avatar-card .tool-icon{color:#8fcaff;border-color:#4a7597;background:#4a759720}.avatar-card .launch{color:#91caff}.avatar-card:hover{border-color:#7cb9e766}.avatar-mark{font:700 15px var(--mono)}.divider{height:1px;margin:32px 0 14px;background:linear-gradient(90deg,transparent,var(--line),transparent)}.fineprint{display:flex;justify-content:space-between;gap:15px;padding-bottom:22px;color:#73634d;font:8px/1.7 var(--mono)}.fineprint a{color:#b28d58}.safe{display:inline-flex;align-items:center;gap:6px}.safe i{width:5px;height:5px;border-radius:50%;background:#8cb784}.tag{padding:4px 6px;border:1px solid rgba(211,169,103,.15);border-radius:3px;color:#a98d60;font:7px var(--mono)}@media(max-width:650px){.wrap{width:min(100% - 26px,480px)}.hero{padding:56px 0 27px}.cards{grid-template-columns:1fr}.tool-card{min-height:230px}.preview{width:35%;opacity:.55}.fineprint{flex-direction:column;gap:4px}.tools-head{margin-top:10px}}@media(prefers-reduced-motion:reduce){*,*:before,*:after{transition:none!important;animation:none!important;scroll-behavior:auto!important}}
</style></head><body><div class="grain"></div><div class="wrap"><header class="top"><a class="brand" href="/"><span class="logo"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M12 2 21 7v10l-9 5-9-5V7zM3 7l9 5 9-5M12 12v10"/></svg></span><span><b>FIELDKIT</b><small>PERSONAL TOOL DESK</small></span></a><span class="local"><i></i>LOCAL WORKSPACE</span></header><main><section class="hero"><div class="eyebrow">A QUIETER KIND OF POWER TOOL</div><h1>Your tools.<br><span>Your space.</span></h1><p>Choose a workspace to open. Your notes, watchlist, and settings stay on this machine. No sign-in tokens or third-party account credentials are stored.</p><div class="quick"><span>QUICK OPEN</span><kbd>CTRL</kbd><kbd>K</kbd></div></section><div class="tools-head"><h2>01 / SELECT A TOOL</h2><span>2 WORKSPACES</span></div><section class="cards">
<a class="tool-card avatar-card" href="/avatar"><div class="card-top"><span class="tool-icon"><span class="avatar-mark">AV</span></span><span class="tag">ROBLOX / NO LOGIN</span></div><div class="preview"><svg viewBox="0 0 140 100"><defs><linearGradient id="avglow" x1="0" x2="1"><stop stop-color="#84c9ff" stop-opacity=".25"/><stop offset="1" stop-color="#94a7ff" stop-opacity=".04"/></linearGradient></defs><rect x="18" y="9" width="95" height="80" rx="9" fill="url(#avglow)" stroke="#6ebcff" stroke-opacity=".4"/><circle cx="66" cy="39" r="17" fill="#a9d9ff" fill-opacity=".65"/><path d="M35 82c3-19 16-29 31-29s28 10 31 29" fill="#75baff" fill-opacity=".45"/><path d="M26 17h30M26 23h18M26 70h22M86 70h17M86 76h12" stroke="#91c9ff" stroke-opacity=".35" stroke-width="2" stroke-linecap="round"/></svg></div><h3>ROBLOX / AVATAR FINDER</h3><p>Explore public Roblox profiles, avatars, outfits, games, groups, and account stats from one searchable dashboard.</p><div class="card-bottom"><span class="card-meta">PROFILES · OUTFITS · ITEMS</span><span class="launch">OPEN AVATAR FINDER <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M5 12h14m-6-6 6 6-6 6"/></svg></span></div></a>
<a class="tool-card discord-card" href="/discord"><div class="card-top"><span class="tool-icon"><span class="discord-mark">@</span></span><span class="tag">MANUAL CHECKS ONLY</span></div><div class="preview"><svg viewBox="0 0 140 100"><g class="preview-line"><path d="M22 70 46 40M46 40 75 58M75 58 105 26M75 58 115 76M22 70 55 83M46 40 105 26M55 83 115 76"/></g><circle class="preview-dot cool" cx="22" cy="70" r="3"/><circle class="preview-dot" cx="46" cy="40" r="4"/><circle class="preview-dot cool" cx="75" cy="58" r="5"/><circle class="preview-dot" cx="105" cy="26" r="3"/><circle class="preview-dot" cx="115" cy="76" r="3"/><circle class="preview-dot cool" cx="55" cy="83" r="2"/></svg></div><h3>DISCORD / NAME RADAR</h3><p>Keep an organized shortlist of username ideas. Add notes, set priorities, run manual reminders, and record what you verified yourself.</p><div class="card-bottom"><span class="card-meta">WATCHLIST · SETTINGS · ALERTS</span><span class="launch">OPEN NAME RADAR <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M5 12h14m-6-6 6 6-6 6"/></svg></span></div></a>
</section><div class="divider"></div><footer class="fineprint"><span class="safe"><i></i> No Discord bot, token, auto-claim, or rate-limit evasion.</span><span>Need a quick reset? <a href="/health">Check local server status ↗</a></span><span>FIELDKIT / 2026</span></footer></main></div><script>addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){e.preventDefault();location.href='/avatar'}})</script></body></html>"""


DISCORD_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#0b0d13"><title>FIELDKIT — Discord Name Radar</title>
<style>
:root{color-scheme:dark;--bg:#0b0c11;--panel:#11131a;--panel2:#151822;--line:rgba(157,171,231,.16);--line2:rgba(157,171,231,.3);--text:#e8eaf4;--muted:#9095aa;--dim:#5f657b;--periwinkle:#aab8ff;--blue:#7a8ef0;--green:#81d5ad;--orange:#eab76a;--red:#e58d93;--mono:ui-monospace,Consolas,monospace;--sans:Inter,system-ui,-apple-system,"Segoe UI",sans-serif}*{box-sizing:border-box}html,body{margin:0;min-height:100%;background:radial-gradient(ellipse at 70% 3%,rgba(67,79,143,.16),transparent 43%),var(--bg);color:var(--text);font:13px var(--sans)}button,input,select,textarea{font:inherit;color:inherit}button{cursor:pointer}button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{outline:1px solid var(--periwinkle);outline-offset:2px}a{color:inherit;text-decoration:none}.page{width:min(1180px,calc(100% - 40px));margin:auto}.top{height:58px;display:flex;align-items:center;gap:14px;border-bottom:1px solid var(--line)}.back{display:flex;align-items:center;gap:7px;color:#a5abc0;font:9px var(--mono)}.back:hover{color:white}.back svg{width:14px}.brand{display:flex;align-items:center;gap:8px}.brand-icon{width:27px;height:27px;display:grid;place-items:center;border:1px solid rgba(140,155,235,.4);border-radius:8px;background:rgba(92,106,194,.18);color:#bdc8ff;font-weight:700}.brand b{font:600 10px var(--mono);letter-spacing:.12em}.brand small{display:block;color:#6c7187;font:7px var(--mono);letter-spacing:.1em}.top-right{margin-left:auto;display:flex;align-items:center;gap:9px;color:#8b91a7;font:8px var(--mono)}.top-right i{width:5px;height:5px;border-radius:50%;background:#7bd3ab;box-shadow:0 0 6px #79d2a6}.button{height:31px;display:inline-flex;align-items:center;justify-content:center;gap:6px;padding:0 10px;border:1px solid var(--line);border-radius:5px;background:#171a24;color:#c2c8dc;font:8px var(--mono);transition:.16s}.button:hover{border-color:var(--line2);background:#202434}.button.primary{border-color:rgba(166,181,255,.35);background:linear-gradient(120deg,#596bb9,#44518a);color:#fff}.button.danger{color:#e59a9f;border-color:rgba(229,141,147,.23)}.button svg{width:13px;height:13px}.hero{display:flex;align-items:end;justify-content:space-between;gap:20px;padding:37px 0 23px}.eyebrow{color:#929dcf;font:8px var(--mono);letter-spacing:.16em}.hero h1{margin:7px 0 5px;color:#f0f1f9;font-size:28px;letter-spacing:-.035em;font-weight:550}.hero p{max-width:650px;margin:0;color:#9297aa;font-size:10px;line-height:1.65}.hero-actions{display:flex;gap:7px;flex:none}.notice{display:flex;align-items:flex-start;gap:10px;margin:0 0 16px;padding:10px 12px;border:1px solid rgba(236,183,106,.22);border-radius:6px;background:rgba(174,125,55,.08);color:#d7c4a3;font-size:9px;line-height:1.55}.notice b{color:#f0c77f}.notice .shield{color:#edc177;font-size:13px}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:13px}.stat{padding:11px 12px;border:1px solid var(--line);border-radius:6px;background:linear-gradient(140deg,rgba(24,27,39,.92),rgba(15,17,24,.9))}.stat span{display:block;color:#7c8299;font:7px var(--mono);letter-spacing:.1em}.stat b{display:block;margin-top:6px;color:#e7eaf5;font:600 19px var(--mono)}.stat b.watch{color:var(--periwinkle)}.stat b.available{color:var(--green)}.stat b.taken{color:var(--red)}.toolbar{display:flex;align-items:center;gap:7px;padding:9px;border:1px solid var(--line);border-bottom:0;border-radius:7px 7px 0 0;background:#11131a}.toolbar-search{height:31px;display:flex;align-items:center;gap:7px;flex:1;min-width:100px;padding:0 9px;border:1px solid rgba(160,170,221,.14);border-radius:4px;background:#0c0e14;color:#8990aa}.toolbar-search svg{width:13px}.toolbar-search input{width:100%;border:0;outline:0;background:none;color:#e3e6f2;font:9px var(--mono)}.toolbar-search input::placeholder{color:#626982}.toolbar select{height:31px;max-width:150px;padding:0 8px;border:1px solid rgba(160,170,221,.15);border-radius:4px;background:#0c0e14;color:#b8bfd2;font:8px var(--mono)}.table{overflow:hidden;border:1px solid var(--line);border-radius:0 0 7px 7px;background:rgba(14,16,22,.89)}.compact .name-row{min-height:43px}.compact .name-glyph{width:22px;height:22px}.table-head,.name-row{display:grid;grid-template-columns:minmax(160px,1.3fr) 115px 86px minmax(130px,1fr) 156px;align-items:center;gap:10px;padding:0 12px}.table-head{height:32px;border-bottom:1px solid rgba(157,171,231,.1);color:#70778d;font:7px var(--mono);letter-spacing:.1em}.name-row{min-height:63px;border-bottom:1px solid rgba(157,171,231,.08);transition:background .15s}.name-row:last-child{border-bottom:0}.name-row:hover{background:rgba(93,109,187,.07)}.name-title{min-width:0;display:flex;align-items:center;gap:9px}.name-glyph{width:27px;height:27px;flex:none;display:grid;place-items:center;border:1px solid rgba(152,166,238,.18);border-radius:6px;background:#8797f014;color:#b8c3ff;font:12px var(--mono)}.name-copy{min-width:0}.name-copy b{display:block;overflow:hidden;color:#e1e5f2;text-overflow:ellipsis;white-space:nowrap;font:10px var(--mono)}.name-copy small{display:block;margin-top:3px;overflow:hidden;color:#6f768c;text-overflow:ellipsis;white-space:nowrap;font:7px var(--mono)}.status-select{height:26px;width:106px;padding:0 6px;border:1px solid rgba(160,170,221,.17);border-radius:4px;background:#12151e;color:#bfc5d8;font:7px var(--mono)}.status-select[data-status="available"]{border-color:rgba(129,213,173,.27);color:var(--green)}.status-select[data-status="taken"]{color:var(--red)}.status-select[data-status="watching"]{color:var(--periwinkle)}.priority{display:inline-flex;align-items:center;gap:5px;color:#9298ab;font:7px var(--mono)}.priority i{width:5px;height:5px;border-radius:50%;background:#778096}.priority.high i{background:#e68b87;box-shadow:0 0 5px #e68b87}.priority.normal i{background:#e6bb6d}.priority.low i{background:#7b8cbb}.notes{overflow:hidden;color:#81879a;text-overflow:ellipsis;white-space:nowrap;font-size:8px}.row-actions{display:flex;justify-content:flex-end;gap:5px}.mini-button{height:25px;display:inline-flex;align-items:center;gap:4px;padding:0 7px;border:1px solid rgba(157,171,231,.14);border-radius:4px;background:#171a24;color:#9fa8c4;font:7px var(--mono)}.mini-button:hover{border-color:rgba(157,171,231,.32);color:#e4e8f7}.mini-button svg{width:11px;height:11px}.remove-button{width:25px;justify-content:center;padding:0;color:#9c737a}.empty{padding:48px 20px;text-align:center}.empty b{display:block;color:#d5d9e9;font:11px var(--mono)}.empty p{margin:7px;color:#767e95;font-size:9px}.empty button{margin-top:8px}.bottom-grid{display:grid;grid-template-columns:1fr 320px;gap:9px;margin-top:13px}.panel{border:1px solid var(--line);border-radius:6px;background:linear-gradient(140deg,rgba(22,25,35,.95),rgba(13,15,21,.94))}.panel-head{height:32px;display:flex;align-items:center;justify-content:space-between;padding:0 10px;border-bottom:1px solid rgba(157,171,231,.1);color:#929abc;font:7px var(--mono);letter-spacing:.11em}.panel-head small{color:#636b82;font-size:7px}.events{padding:6px 10px}.event{display:grid;grid-template-columns:45px 1fr 50px;gap:8px;padding:6px 0;border-bottom:1px solid rgba(157,171,231,.06);color:#9198ac;font:7px var(--mono)}.event:last-child{border:0}.event time{color:#616a82}.event b{overflow:hidden;color:#c2c8da;text-overflow:ellipsis;white-space:nowrap;font-weight:400}.event span:last-child{text-align:right;color:#707991}.settings-body{padding:8px 11px}.setting{display:flex;align-items:center;justify-content:space-between;gap:10px;min-height:31px;border-bottom:1px solid rgba(157,171,231,.06);color:#aab0c0;font-size:8px}.setting:last-child{border:0}.setting label{font:8px var(--mono)}.setting select{height:23px;padding:0 6px;border:1px solid rgba(157,171,231,.16);border-radius:4px;background:#0c0e14;color:#bfc5d8;font:7px var(--mono)}.switch{position:relative;width:31px;height:17px;flex:none;padding:0;border:1px solid #3d4254;border-radius:20px;background:#252938}.switch:after{content:"";position:absolute;top:2px;left:2px;width:11px;height:11px;border-radius:50%;background:#838ba0;transition:.15s}.switch.on{border-color:#6776c3;background:#4c5aa0}.switch.on:after{left:16px;background:#e2e6ff}.help-line{padding:8px 11px;color:#737b91;font:7px/1.55 var(--mono)}.quiet-setting input{width:66px;height:22px;padding:0 3px;border:1px solid rgba(157,171,231,.16);border-radius:3px;background:#0c0e14;color:#bfc5d8;font:7px var(--mono)}.drawer{position:fixed;inset:0;z-index:10;display:none;place-items:center;padding:16px;background:#000a;backdrop-filter:blur(7px)}.drawer.open{display:grid}.form-card{width:min(460px,100%);padding:17px;border:1px solid rgba(158,173,239,.3);border-radius:8px;background:#141720;box-shadow:0 20px 65px #000a}.form-card h2{margin:0 0 4px;font:500 14px var(--mono);color:#edf0fc}.form-card>p{margin:0 0 14px;color:#8b92a7;font-size:9px;line-height:1.5}.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.form-field{display:flex;flex-direction:column;gap:5px;margin-bottom:9px}.form-field.full{grid-column:1/-1}.form-field label{color:#858da6;font:7px var(--mono);letter-spacing:.08em}.form-field input,.form-field textarea,.form-field select{width:100%;min-height:31px;padding:6px 8px;border:1px solid rgba(157,171,231,.16);border-radius:4px;background:#0c0e14;color:#e2e6f5;font:9px var(--mono)}.form-field textarea{height:70px;resize:vertical}.drawer-actions{display:flex;justify-content:flex-end;gap:7px;margin-top:12px}.toast-stack{position:fixed;right:15px;bottom:15px;z-index:20;display:flex;flex-direction:column;gap:6px;pointer-events:none}.toast{max-width:340px;padding:9px 11px;border:1px solid rgba(159,174,239,.25);border-radius:5px;background:#171a24;color:#e7eaf5;box-shadow:0 10px 25px #0008;font:8px var(--mono)}.foot{padding:13px 0 20px;color:#606980;text-align:center;font:7px var(--mono)}.foot a{color:#94a4ed}.hidden{display:none!important}
@media(max-width:900px){.table-head,.name-row{grid-template-columns:minmax(140px,1.2fr) 105px 75px minmax(100px,1fr) 125px;gap:6px;padding-inline:8px}.status-select{width:100px}.notes{font-size:7px}.row-actions .mini-button span{display:none}.mini-button{width:28px;justify-content:center;padding:0}.bottom-grid{grid-template-columns:1fr 280px}}
@media(max-width:680px){.page{width:calc(100% - 22px)}.hero{align-items:flex-start;flex-direction:column;padding:25px 0 17px}.hero h1{font-size:23px}.hero-actions{width:100%}.stats{grid-template-columns:repeat(2,1fr)}.toolbar{flex-wrap:wrap}.toolbar-search{flex-basis:100%}.toolbar select{flex:1;max-width:none}.table-head{display:none}.table{background:transparent;border:0;display:flex;flex-direction:column;gap:8px}.name-row{position:relative;grid-template-columns:minmax(0,1fr) 112px;gap:7px;padding:9px;border:1px solid var(--line);border-radius:6px;background:#11131a}.name-row>.priority,.name-row>.notes{display:none}.name-title{grid-column:1}.status-select{grid-column:2;grid-row:1;width:100%}.row-actions{grid-column:1/-1;justify-content:flex-start;padding-left:35px}.row-actions .mini-button span{display:inline}.row-actions .mini-button{width:auto;padding:0 7px}.bottom-grid{grid-template-columns:1fr}.settings-panel{order:-1}.top-right .server-state{display:none}.notice{font-size:8px}.foot{padding-bottom:12px}}
@media(prefers-reduced-motion:reduce){*,*:before,*:after{transition:none!important;animation:none!important}}
</style></head><body><div class="page"><header class="top"><a class="back" href="/" title="Back to tool desk"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="m15 18-6-6 6-6M9 12h12"/></svg>TOOL DESK</a><span class="brand"><span class="brand-icon">@</span><span><b>NAME RADAR</b><small>DISCORD USERNAME WATCHLIST</small></span></span><span class="top-right"><i></i><span class="server-state">LOCAL WATCHLIST</span><span id="saveStatus">SAVED</span></span></header>
<main><section class="hero"><div><div class="eyebrow">DISCORD / NAME RADAR</div><h1>Your shortlist, under control.</h1><p>Organize name ideas, keep notes and priorities, and set reminders for a manual check. This tool does not query Discord or claim usernames for you.</p></div><div class="hero-actions"><button class="button" id="settingsJump">⚙ SETTINGS</button><button class="button primary" id="addBtn">＋ ADD CANDIDATE</button></div></section>
<div class="notice"><span class="shield">◈</span><span><b>Manual-only by design.</b> Discord does not provide this app with an official public availability checker. “Available” and “Taken” are your own notes after checking in Discord. No token, bot, automated claim, account scraping, or rate-limit bypass is used.</span></div>
<section class="stats"><div class="stat"><span>TOTAL CANDIDATES</span><b id="statTotal">00</b></div><div class="stat"><span>WATCHING</span><b class="watch" id="statWatching">00</b></div><div class="stat"><span>MANUALLY MARKED AVAILABLE</span><b class="available" id="statAvailable">00</b></div><div class="stat"><span>HIGH PRIORITY</span><b class="taken" id="statPriority">00</b></div></section>
<div class="toolbar"><label class="toolbar-search"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/></svg><input id="filter" placeholder="Filter candidates or notes…" type="search"></label><select id="sortSelect" aria-label="Sort candidates"><option value="priority">Priority first</option><option value="name">Name A–Z</option><option value="recent">Recently updated</option></select><button class="button" id="reminderBtn">▶ START REMINDERS</button><button class="button" id="exportBtn">⇩ EXPORT</button><button class="button" id="importBtn">⇧ IMPORT</button><input id="importFile" class="hidden" type="file" accept="application/json,.json"></div>
<section class="table"><div class="table-head"><span>USERNAME IDEA</span><span>MANUAL STATUS</span><span>PRIORITY</span><span>NOTES</span><span style="text-align:right">ACTIONS</span></div><div id="candidateList"></div></section>
<div class="bottom-grid"><section class="panel"><div class="panel-head"><span>RECENT ACTIVITY</span><small id="eventCount">00 EVENTS</small></div><div class="events" id="events"></div><div class="help-line">Tip: “Copy + open Discord” copies the candidate and opens Discord settings in a new tab. You complete any check or username change yourself.</div></section><section class="panel settings-panel" id="settingsPanel"><div class="panel-head"><span>REMINDER SETTINGS</span><small>MANUAL PROMPTS ONLY</small></div><div class="settings-body"><div class="setting"><label for="interval">Reminder interval</label><select id="interval"><option value="5">Every 5 min</option><option value="10">Every 10 min</option><option value="15">Every 15 min</option><option value="30">Every 30 min</option><option value="60">Every hour</option></select></div><div class="setting"><label for="notifications">Desktop notifications</label><button class="switch" id="notifications" role="switch" aria-checked="false" aria-label="Toggle desktop notifications"></button></div><div class="setting"><label for="sound">Play a soft alert</label><button class="switch" id="sound" role="switch" aria-checked="false" aria-label="Toggle sound alert"></button></div><div class="setting"><label for="notifyAvailable">Alert when manually marked available</label><button class="switch" id="notifyAvailable" role="switch" aria-checked="true" aria-label="Alert on manual available status"></button></div><div class="setting"><label for="onlyWatching">Remind for watching names only</label><button class="switch" id="onlyWatching" role="switch" aria-checked="true" aria-label="Remind only for watching names"></button></div><div class="setting"><label for="compactMode">Compact candidate rows</label><button class="switch" id="compactMode" role="switch" aria-checked="false" aria-label="Toggle compact rows"></button></div><div class="setting"><label for="confirmDelete">Confirm before removing</label><button class="switch" id="confirmDelete" role="switch" aria-checked="true" aria-label="Confirm before removing names"></button></div><div class="setting"><label for="copyFormat">Copy format</label><select id="copyFormat"><option value="plain">name</option><option value="mention">@name</option></select></div><div class="setting quiet-setting"><label>Quiet hours</label><span><input type="time" id="quietStart" aria-label="Quiet hours start"> – <input type="time" id="quietEnd" aria-label="Quiet hours end"></span></div></div><div class="help-line">Reminders only ask you to check manually; they never call Discord or auto-claim a name.</div></section></div>
<div class="foot">FIELDKIT / NAME RADAR &nbsp;·&nbsp; <a href="/">BACK TO TOOL DESK</a> &nbsp;·&nbsp; YOUR WATCHLIST STAYS LOCAL TO THIS SERVER</div></main></div>
<div class="drawer" id="drawer"><form class="form-card" id="candidateForm"><h2 id="formTitle">Add a candidate</h2><p>Store an idea and your own notes. The format check is local only; it does not verify availability.</p><div class="form-grid"><div class="form-field full"><label for="candidateName">USERNAME IDEA</label><input id="candidateName" name="name" autocomplete="off" maxlength="32" placeholder="e.g. quiet.comet" required></div><div class="form-field"><label for="candidatePriority">PRIORITY</label><select id="candidatePriority" name="priority"><option value="normal">Normal</option><option value="high">High</option><option value="low">Low</option></select></div><div class="form-field"><label for="candidateStatus">YOUR STATUS</label><select id="candidateStatus" name="status"><option value="watching">Watching</option><option value="available">Manually marked available</option><option value="taken">Manually marked taken</option><option value="parked">Parked</option></select></div><div class="form-field full"><label for="candidateNotes">NOTES</label><textarea id="candidateNotes" name="notes" maxlength="500" placeholder="Why this name? Context, variants, reminders…"></textarea></div></div><div id="formError" style="min-height:15px;color:#e69b9d;font:8px var(--mono)"></div><div class="drawer-actions"><button class="button" type="button" id="cancelForm">CANCEL</button><button class="button primary" type="submit">SAVE CANDIDATE</button></div></form></div>
<div class="toast-stack" id="toastStack" aria-live="polite"></div>
<script>
'use strict';const $=s=>document.querySelector(s),$$=s=>[...document.querySelectorAll(s)];const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));const labels={watching:'Watching',available:'Available (manual)',taken:'Taken (manual)',parked:'Parked'};const model={names:[],events:[],settings:{checkInterval:15,desktopNotifications:false,sound:false,notifyOnAvailable:true,remindOnlyWatching:true,compact:false,confirmDelete:true,copyFormat:'plain',quietStart:'22:00',quietEnd:'08:00',sort:'priority'},savedAt:null,filter:'',editId:null,timer:null,seconds:0,audio:null};
function say(text){const el=document.createElement('div');el.className='toast';el.textContent=text;$('#toastStack').append(el);setTimeout(()=>el.remove(),3000)}function time(iso){try{return new Date(iso).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})}catch{return'--:--'}}function validateName(raw){const n=String(raw||'').trim().toLowerCase();if(!/^[a-z0-9._]{2,32}$/.test(n)||n.startsWith('.')||n.endsWith('.')||n.includes('..'))throw new Error('Use 2–32 lowercase letters, numbers, underscores or dots; dots cannot repeat or be at either end.');return n}async function api(path,opt={}){const r=await fetch(path,{headers:{'Content-Type':'application/json',...(opt.headers||{})},...opt});let d={};try{d=await r.json()}catch{}if(!r.ok)throw new Error(d.error||`Request failed (${r.status})`);return d}function scheduleSave(){clearTimeout(model.saveTimer);$('#saveStatus').textContent='SAVING…';model.saveTimer=setTimeout(save,350)}async function save(){try{const d=await api('/api/watchlist',{method:'PUT',body:JSON.stringify({names:model.names,events:model.events,settings:model.settings})});model.savedAt=d.savedAt;$('#saveStatus').textContent='SAVED';renderStats()}catch(e){$('#saveStatus').textContent='SAVE ERROR';say(e.message)}}function event(text){model.events.unshift({at:new Date().toISOString(),text});model.events=model.events.slice(0,60);renderEvents();scheduleSave()}function priorityRank(p){return p==='high'?0:p==='normal'?1:2}
function filtered(){return model.names.filter(n=>!model.filter||`${n.name} ${n.notes} ${n.status} ${n.priority}`.toLowerCase().includes(model.filter.toLowerCase()))}function renderStats(){$('#statTotal').textContent=String(model.names.length).padStart(2,'0');$('#statWatching').textContent=String(model.names.filter(n=>n.status==='watching').length).padStart(2,'0');$('#statAvailable').textContent=String(model.names.filter(n=>n.status==='available').length).padStart(2,'0');$('#statPriority').textContent=String(model.names.filter(n=>n.priority==='high').length).padStart(2,'0');$('#eventCount').textContent=`${String(model.events.length).padStart(2,'0')} EVENTS`;$('#sortSelect').value=model.settings.sort||'priority'}
function renderNames(){let rows=filtered().slice(),sort=model.settings.sort;if(sort==='name')rows.sort((a,b)=>a.name.localeCompare(b.name));else if(sort==='recent')rows.sort((a,b)=>b.updatedAt.localeCompare(a.updatedAt));else rows.sort((a,b)=>priorityRank(a.priority)-priorityRank(b.priority)||a.name.localeCompare(b.name));const box=$('#candidateList');if(!rows.length){box.innerHTML=`<div class="empty"><b>${model.names.length?'NO MATCHES':'YOUR RADAR IS CLEAR'}</b><p>${model.names.length?'Try a different search.':'Add a name idea to start your private watchlist.'}</p>${model.names.length?'':'<button class="button primary" id="emptyAdd">＋ ADD FIRST CANDIDATE</button>'}</div>`;return}box.innerHTML=rows.map(n=>`<article class="name-row" data-id="${esc(n.id)}"><div class="name-title"><span class="name-glyph">@</span><div class="name-copy"><b>${esc(n.name)}</b><small>${esc(n.id)} · UPDATED ${time(n.updatedAt)}</small></div></div><select class="status-select" data-status-select="${esc(n.id)}" data-status="${esc(n.status)}" aria-label="Manual status for ${esc(n.name)}">${Object.entries(labels).map(([v,l])=>`<option value="${v}"${n.status===v?' selected':''}>${l}</option>`).join('')}</select><span class="priority ${n.priority}"><i></i>${n.priority.toUpperCase()}</span><span class="notes" title="${esc(n.notes)}">${esc(n.notes||'— no notes —')}</span><span class="row-actions"><button class="mini-button" data-copy="${esc(n.id)}" title="Copy candidate"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="8" y="8" width="12" height="12" rx="1"/><path d="M16 8V5H5v11h3"/></svg><span>COPY</span></button><button class="mini-button" data-open-discord="${esc(n.id)}" title="Copy name and open Discord settings"><span>CHECK MANUALLY</span> ↗</button><button class="mini-button" data-edit="${esc(n.id)}" title="Edit candidate">EDIT</button><button class="mini-button remove-button" data-remove="${esc(n.id)}" title="Remove candidate" aria-label="Remove ${esc(n.name)}">×</button></span></article>`).join('')}
function renderEvents(){$('#events').innerHTML=model.events.slice(0,5).map(e=>`<div class="event"><time>${time(e.at)}</time><b>${esc(e.text)}</b><span>LOCAL</span></div>`).join('')||'<div class="help-line">No activity yet. Add a candidate or update a manual status.</div>'}function renderAll(){renderStats();renderNames();renderEvents();$('#interval').value=String(model.settings.checkInterval);$('#copyFormat').value=model.settings.copyFormat||'plain';$('#quietStart').value=model.settings.quietStart||'22:00';$('#quietEnd').value=model.settings.quietEnd||'08:00';$('#candidateList').classList.toggle('compact',!!model.settings.compact);setSwitch('notifications',model.settings.desktopNotifications);setSwitch('sound',model.settings.sound);setSwitch('notifyAvailable',model.settings.notifyOnAvailable);setSwitch('onlyWatching',model.settings.remindOnlyWatching);setSwitch('compactMode',model.settings.compact);setSwitch('confirmDelete',model.settings.confirmDelete)}function setSwitch(id,on){const b=$('#'+id);b.classList.toggle('on',!!on);b.setAttribute('aria-checked',String(!!on))}
function openForm(id=null){model.editId=id;const n=id&&model.names.find(x=>x.id===id);$('#formTitle').textContent=n?'Edit candidate':'Add a candidate';$('#candidateName').value=n?.name||'';$('#candidatePriority').value=n?.priority||'normal';$('#candidateStatus').value=n?.status||'watching';$('#candidateNotes').value=n?.notes||'';$('#formError').textContent='';$('#drawer').classList.add('open');setTimeout(()=>$('#candidateName').focus(),20)}function closeForm(){$('#drawer').classList.remove('open');model.editId=null}
function saveCandidate(form){const values=new FormData(form),name=validateName(values.get('name'));if(model.names.some(n=>n.name===name&&n.id!==model.editId))throw new Error('That candidate is already on your list.');const now=new Date().toISOString(),existing=model.editId&&model.names.find(n=>n.id===model.editId);if(existing){existing.name=name;existing.priority=values.get('priority');existing.status=values.get('status');existing.notes=String(values.get('notes')||'').slice(0,500);existing.updatedAt=now;event(`Updated @${name}.`)}else{const n={id:'c-'+Math.random().toString(36).slice(2,9),name,priority:values.get('priority'),status:values.get('status'),notes:String(values.get('notes')||'').slice(0,500),createdAt:now,updatedAt:now};model.names.unshift(n);event(`Added @${name} to the watchlist.`)}closeForm();renderAll()}
async function copyName(id){const n=model.names.find(x=>x.id===id);if(!n)return;const value=model.settings.copyFormat==='mention'?'@'+n.name:n.name;try{await navigator.clipboard.writeText(value);say(`Copied ${value}`)}catch{const input=document.createElement('textarea');input.value=value;document.body.append(input);input.select();document.execCommand('copy');input.remove();say(`Copied ${value}`)}}function openDiscord(id){const n=model.names.find(x=>x.id===id);if(!n)return;window.open('https://discord.com/settings','_blank','noopener,noreferrer');copyName(id);event(`Opened Discord settings for a manual check of @${n.name}.`)}function removeName(id){const n=model.names.find(x=>x.id===id);if(!n)return;if(model.settings.confirmDelete&&!confirm(`Remove @${n.name} from your watchlist?`))return;model.names=model.names.filter(x=>x.id!==id);event(`Removed @${n.name}.`);renderAll()}
function updateStatus(id,status){const n=model.names.find(x=>x.id===id);if(!n||!labels[status])return;const old=n.status;n.status=status;n.updatedAt=new Date().toISOString();renderAll();event(`Manually changed @${n.name}: ${labels[old]} → ${labels[status]}.`);if(status==='available'&&model.settings.notifyOnAvailable)notifyUser(`@${n.name} marked available`, 'This is your manual status; it was not checked by the app.')}
function quietNow(){const toMinutes=value=>{const [h,m]=String(value||'00:00').split(':').map(Number);return h*60+m};const start=toMinutes(model.settings.quietStart),end=toMinutes(model.settings.quietEnd);if(start===end)return false;const now=new Date(),current=now.getHours()*60+now.getMinutes();return start<end?current>=start&&current<end:current>=start||current<end}function notifyUser(title,body){if(quietNow()){say('Quiet hours are active; reminder suppressed.');return}if(model.settings.desktopNotifications&&'Notification'in window&&Notification.permission==='granted')new Notification(title,{body,tag:'fieldkit-name-radar'});if(model.settings.sound)beep();say(title)}function beep(){ try{const C=window.AudioContext||window.webkitAudioContext;if(!C)return;model.audio=model.audio||new C();const osc=model.audio.createOscillator(),gain=model.audio.createGain();osc.type='sine';osc.frequency.value=740;gain.gain.setValueAtTime(.0001,model.audio.currentTime);gain.gain.exponentialRampToValueAtTime(.045,model.audio.currentTime+.015);gain.gain.exponentialRampToValueAtTime(.0001,model.audio.currentTime+.22);osc.connect(gain);gain.connect(model.audio.destination);osc.start();osc.stop(model.audio.currentTime+.24)}catch{}}
function startReminder(){if(model.timer){clearInterval(model.timer);model.timer=null;$('#reminderBtn').textContent='▶ START REMINDERS';say('Manual reminders paused.');return}model.seconds=model.settings.checkInterval*60;$('#reminderBtn').textContent='Ⅱ PAUSE REMINDERS';model.timer=setInterval(()=>{model.seconds--;if(model.seconds<=0){model.seconds=model.settings.checkInterval*60;const targets=model.settings.remindOnlyWatching?model.names.filter(n=>n.status==='watching'):model.names.filter(n=>n.status!=='parked');if(targets.length)notifyUser(`Manual check time · ${targets.length} name${targets.length===1?'':'s'}`,`Review your ${targets.length} candidate${targets.length===1?'':'s'} manually in Discord settings.`);else say('No candidates match your reminder settings.')}else $('#reminderBtn').textContent=`Ⅱ ${String(Math.floor(model.seconds/60)).padStart(2,'0')}:${String(model.seconds%60).padStart(2,'0')} PAUSE`},1000);say(`Manual reminder set for every ${model.settings.checkInterval} minutes.`)}
function exportBackup(){const payload={version:1,exportedAt:new Date().toISOString(),names:model.names,events:model.events,settings:model.settings};const url=URL.createObjectURL(new Blob([JSON.stringify(payload,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download=`fieldkit-name-watchlist-${new Date().toISOString().slice(0,10)}.json`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);say('Watchlist backup downloaded.')}
function importBackup(file){const reader=new FileReader();reader.onload=async()=>{try{const x=JSON.parse(reader.result);if(!Array.isArray(x.names))throw new Error('This file does not contain a watchlist.');const d=await api('/api/watchlist',{method:'PUT',body:JSON.stringify(x)});model.names=d.names;model.events=d.events||[];model.settings=d.settings;renderAll();say('Watchlist imported.')}catch(e){say(`Import failed: ${e.message}`)}};reader.readAsText(file)}
function bind(){
 $('#addBtn').onclick=()=>openForm();$('#settingsJump').onclick=()=>$('#settingsPanel').scrollIntoView({behavior:'smooth',block:'center'});$('#cancelForm').onclick=closeForm;$('#candidateForm').addEventListener('submit',e=>{e.preventDefault();try{saveCandidate(e.target)}catch(err){$('#formError').textContent=err.message}});$('#drawer').addEventListener('click',e=>{if(e.target===$('#drawer'))closeForm()});
 $('#filter').addEventListener('input',e=>{model.filter=e.target.value;renderNames()});
 $('#sortSelect').addEventListener('change',e=>{model.settings.sort=e.target.value;renderNames();scheduleSave()});
 $('#interval').addEventListener('change',e=>{model.settings.checkInterval=Number(e.target.value);scheduleSave();if(model.timer){clearInterval(model.timer);model.timer=null;startReminder()}});
 $('#notifications').onclick=async()=>{if(!model.settings.desktopNotifications&&'Notification'in window&&Notification.permission==='default'){const p=await Notification.requestPermission();if(p!=='granted'){say('Notification permission was not granted.');return}}model.settings.desktopNotifications=!model.settings.desktopNotifications;setSwitch('notifications',model.settings.desktopNotifications);scheduleSave()};
 $('#sound').onclick=()=>{model.settings.sound=!model.settings.sound;setSwitch('sound',model.settings.sound);scheduleSave();if(model.settings.sound)beep()};
 const toggleSetting=(id,key)=>{model.settings[key]=!model.settings[key];setSwitch(id,model.settings[key]);if(key==='compact')$('#candidateList').classList.toggle('compact',model.settings.compact);scheduleSave()};
 $('#notifyAvailable').onclick=()=>toggleSetting('notifyAvailable','notifyOnAvailable');$('#onlyWatching').onclick=()=>toggleSetting('onlyWatching','remindOnlyWatching');$('#compactMode').onclick=()=>toggleSetting('compactMode','compact');$('#confirmDelete').onclick=()=>toggleSetting('confirmDelete','confirmDelete');
 $('#copyFormat').addEventListener('change',e=>{model.settings.copyFormat=e.target.value;scheduleSave()});
 ['quietStart','quietEnd'].forEach(id=>$('#'+id).addEventListener('change',e=>{model.settings[id]=e.target.value;scheduleSave()}));
 $('#reminderBtn').onclick=startReminder;$('#exportBtn').onclick=exportBackup;$('#importBtn').onclick=()=>$('#importFile').click();$('#importFile').onchange=e=>{if(e.target.files?.[0])importBackup(e.target.files[0]);e.target.value=''};
 document.addEventListener('click',e=>{const add=e.target.closest('#emptyAdd');if(add){openForm();return}const copy=e.target.closest('[data-copy]');if(copy){copyName(copy.dataset.copy);return}const open=e.target.closest('[data-open-discord]');if(open){openDiscord(open.dataset.openDiscord);return}const edit=e.target.closest('[data-edit]');if(edit){openForm(edit.dataset.edit);return}const remove=e.target.closest('[data-remove]');if(remove){removeName(remove.dataset.remove);return}});document.addEventListener('change',e=>{const s=e.target.closest('[data-status-select]');if(s)updateStatus(s.dataset.statusSelect,s.value)});addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){e.preventDefault();$('#filter').focus()}if(e.key==='Escape')closeForm();if(e.key.toLowerCase()==='n'&&!/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)){e.preventDefault();openForm()}});
}
async function boot(){try{const d=await api('/api/watchlist');model.names=d.names;model.events=d.events||[];model.settings=d.settings||model.settings;model.savedAt=d.savedAt;renderAll()}catch(e){say(`Could not load local watchlist: ${e.message}`)}bind()}boot();
</script></body></html>"""


class Handler(avatar_app.H):
    server_version = "Fieldkit/1.0"

    def log_message(self, fmt: str, *args) -> None:
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))

    def send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_json(self, status: int, payload: object) -> None:
        self.send_bytes(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def body_json(self) -> object:
        length = int(self.headers.get("Content-Length", "0"))
        if length < 2 or length > MAX_BODY:
            raise OverflowError("Request body is empty or larger than 2 MB.")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html", "/hub"):
            self.send_bytes(200, HUB_HTML.encode(), "text/html; charset=utf-8")
        elif path in ("/avatar", "/test.html"):
            self.send_bytes(200, avatar_app.PAGE.encode(), "text/html; charset=utf-8")
        elif path == "/memory":
            self.send_response(302)
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path == "/discord":
            self.send_bytes(200, DISCORD_HTML.encode(), "text/html; charset=utf-8")
        elif path == "/api/watchlist":
            self.send_json(200, load_watchlist())
        elif path == "/health":
            self.send_json(200, {"ok": True, "app": "FIELDKIT", "tools": ["avatar-finder", "discord-watchlist"]})
        else:
            super().do_GET()

    def do_PUT(self) -> None:
        path = urlparse(self.path).path
        try:
            raw = self.body_json()
            if path == "/api/watchlist":
                self.send_json(200, save_watchlist(raw))
            else:
                self.send_json(404, {"error": "Not found"})
        except OverflowError as exc:
            self.send_json(413, {"error": str(exc)})
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            self.send_json(400, {"error": str(exc)[:240]})
        except Exception as exc:
            self.send_json(500, {"error": "Could not save data: " + str(exc)[:160]})

    def do_POST(self) -> None:
        super().do_POST()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    parser = argparse.ArgumentParser(description="Launch the FIELDKIT local tool desk.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: localhost)")
    parser.add_argument("--port", type=int, default=8765, help="Port (default: 8765)")
    parser.add_argument("--no-browser", action="store_true", help="Do not open a browser automatically")
    args = parser.parse_args()
    server = Server((args.host, args.port), Handler)
    host = "127.0.0.1" if args.host == "0.0.0.0" else args.host
    url = f"http://{host}:{args.port}/"
    print(f"FIELDKIT is running at {url}")
    print(f"Discord watchlist: {WATCH_FILE}")
    if not args.no_browser:
        threading.Timer(.7, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nClosing FIELDKIT…")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
