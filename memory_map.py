#!/usr/bin/env python3
"""NEXUS Memory Map — a self-contained, local-first knowledge graph dashboard.

Run with: python memory_map.py
For a proxied preview: python memory_map.py --host 0.0.0.0 --port 8001 --no-browser
"""
from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "memory_map_data.json"
MAX_BODY = 2 * 1024 * 1024
STATE_LOCK = threading.Lock()
TYPES = {"core", "note", "project", "decision", "person", "task", "reference"}
COLORS = {"amber", "gold", "cyan", "rose", "violet", "mint"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def seed_state() -> dict:
    """A compact but rich starter graph so the dashboard is useful on first launch."""
    rows = [
        ("brain", "brain.md", "core", "The index of this second brain. Start here: purpose, principles, and how the pieces connect.", ["index", "start"], .50, .48, "amber", ["projects", "decisions", "weekly", "inbox", "systems"]),
        ("projects", "projects/", "project", "A home for active work. Every project gets an outcome, a next action, and a review date.", ["active", "work"], .70, .30, "gold", ["weekly", "ideas", "people"]),
        ("decisions", "decisions.md", "decision", "Record the context behind important choices so future-you can understand the why.", ["principles", "review"], .31, .30, "rose", ["brain", "systems"]),
        ("people", "people/", "person", "Notes about collaborators, mentors, friends, and the ideas they share.", ["relationships", "people"], .78, .51, "cyan", ["projects", "weekly"]),
        ("inbox", "inbox.md", "task", "A temporary landing zone for loose thoughts. Capture fast; sort during the weekly review.", ["capture", "today"], .43, .74, "amber", ["weekly", "ideas"]),
        ("weekly", "weekly-review.md", "note", "A small ritual: celebrate progress, close open loops, and choose the next few priorities.", ["reflection", "ritual"], .56, .19, "violet", ["brain", "projects", "inbox"]),
        ("systems", "systems/", "reference", "Reusable workflows, checklists, and tools that make good work easier to repeat.", ["workflow", "tools"], .20, .52, "mint", ["decisions", "brain"]),
        ("ideas", "ideas/", "note", "Interesting sparks worth keeping. Not every idea needs to become a project.", ["curiosity", "someday"], .57, .82, "gold", ["inbox", "projects"]),
        ("goals", "goals.md", "note", "A short compass for what matters this season. Prefer a few meaningful directions over a long list.", ["direction", "quarter"], .87, .35, "rose", ["brain", "weekly"]),
        ("daily", "daily-log.md", "task", "A lightweight daily log for wins, blockers, and the one thing to carry into tomorrow.", ["journal", "daily"], .34, .83, "cyan", ["inbox", "weekly"]),
        ("reading", "reading-list/", "reference", "Books, articles, talks, and useful passages. Keep the source next to the takeaway.", ["learning", "sources"], .11, .39, "violet", ["ideas", "systems"]),
        ("archive", "archive/", "reference", "Finished projects and old notes. Searchable history, not a pile of things to maintain.", ["done", "history"], .89, .65, "mint", ["projects", "decisions"]),
    ]
    stamp = now_iso()
    nodes = [
        {"id": ident, "title": title, "type": typ, "content": body, "tags": tags,
         "x": x, "y": y, "color": color, "links": links,
         "createdAt": stamp, "updatedAt": stamp}
        for ident, title, typ, body, tags, x, y, color, links in rows
    ]
    return {"version": 1, "nodes": nodes, "logs": [
        {"at": stamp, "text": "NEXUS initialized with a starter knowledge map."},
        {"at": stamp, "text": "Local-first storage is ready. Your edits are saved automatically."},
    ], "savedAt": stamp}


def clean_state(raw: object) -> dict:
    if not isinstance(raw, dict) or not isinstance(raw.get("nodes"), list):
        raise ValueError("Expected an object with a nodes array.")
    if len(raw["nodes"]) > 250:
        raise ValueError("A map can contain at most 250 nodes.")
    nodes, seen = [], set()
    for item in raw["nodes"]:
        if not isinstance(item, dict):
            continue
        ident = str(item.get("id", ""))[:48]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", ident) or ident in seen:
            continue
        seen.add(ident)
        title = str(item.get("title", "Untitled note")).strip()[:120] or "Untitled note"
        typ = str(item.get("type", "note"))
        if typ not in TYPES:
            typ = "note"
        tags = item.get("tags", [])
        if isinstance(tags, str):
            tags = [part.strip() for part in tags.split(",")]
        if not isinstance(tags, list):
            tags = []
        links = item.get("links", [])
        if not isinstance(links, list):
            links = []
        color = str(item.get("color", "amber"))
        if color not in COLORS:
            color = "amber"
        try:
            x = min(.98, max(.02, float(item.get("x", .5))))
            y = min(.98, max(.02, float(item.get("y", .5))))
        except (TypeError, ValueError):
            x, y = .5, .5
        nodes.append({
            "id": ident, "title": title, "type": typ,
            "content": str(item.get("content", ""))[:8000],
            "tags": [str(tag).strip()[:32] for tag in tags[:16] if str(tag).strip()],
            "x": x, "y": y, "color": color,
            "links": [str(link)[:48] for link in links[:80] if str(link)[:48] != ident],
            "createdAt": str(item.get("createdAt", now_iso()))[:40],
            "updatedAt": str(item.get("updatedAt", now_iso()))[:40],
        })
    if not nodes:
        raise ValueError("A map needs at least one node.")
    valid = {node["id"] for node in nodes}
    for node in nodes:
        node["links"] = list(dict.fromkeys(link for link in node["links"] if link in valid))
    logs = raw.get("logs", [])
    if not isinstance(logs, list):
        logs = []
    clean_logs = []
    for log in logs[-80:]:
        if isinstance(log, dict):
            clean_logs.append({"at": str(log.get("at", now_iso()))[:40], "text": str(log.get("text", ""))[:240]})
    saved_at = str(raw.get("savedAt") or now_iso())[:40]
    return {"version": 1, "nodes": nodes, "logs": clean_logs, "savedAt": saved_at}


def load_state() -> dict:
    with STATE_LOCK:
        try:
            return clean_state(json.loads(STATE_FILE.read_text(encoding="utf-8")))
        except (OSError, ValueError, json.JSONDecodeError):
            return seed_state()


def save_state(state: dict) -> dict:
    cleaned = clean_state(state)
    cleaned["savedAt"] = now_iso()
    payload = json.dumps(cleaned, ensure_ascii=False, indent=2)
    with STATE_LOCK:
        fd, temp_name = tempfile.mkstemp(prefix=".nexus-", suffix=".tmp", dir=ROOT)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, STATE_FILE)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
    return cleaned


HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#090806"><meta name="description" content="NEXUS is a local-first, interactive second-brain knowledge graph.">
<title>NEXUS — Memory Map</title>
<style>
:root{color-scheme:dark;--bg:#090806;--panel:#100e0b;--panel2:#15110c;--line:rgba(235,184,112,.16);--line2:rgba(235,184,112,.29);--text:#eee5d5;--muted:#8f8068;--dim:#554a3b;--gold:#efb95f;--gold2:#ffdc9a;--amber:#d88335;--cyan:#76c9c5;--rose:#df8f81;--violet:#b69bf4;--mint:#9bc786;--sans:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;--mono:"SFMono-Regular",Consolas,"Liberation Mono",monospace}
*{box-sizing:border-box}html,body{width:100%;height:100%;margin:0;overflow:hidden;background:var(--bg);color:var(--text);font:13px/1.45 var(--sans)}button,input,textarea,select{font:inherit;color:inherit}button{cursor:pointer}button:focus-visible,input:focus-visible,textarea:focus-visible,select:focus-visible{outline:1px solid var(--gold);outline-offset:2px}::selection{background:#976324;color:white}
body:before{content:"";position:fixed;inset:0;z-index:0;pointer-events:none;opacity:.2;background:radial-gradient(ellipse at 48% 41%,rgba(111,66,24,.22),transparent 52%),linear-gradient(120deg,#0d0b08,#080706 68%)}
#app{position:relative;z-index:1;height:100%;display:grid;grid-template-rows:58px 27px minmax(0,1fr) 24px;padding:0 14px;max-width:1900px;margin:auto}
.topbar{display:flex;align-items:center;gap:18px;border-bottom:1px solid var(--line)}.brand{display:flex;align-items:center;gap:10px;min-width:245px}.brand-mark{width:30px;height:30px;position:relative;display:grid;place-items:center;border:1px solid #9b703b;border-radius:9px;color:var(--gold2);background:radial-gradient(circle at 50% 50%,rgba(237,174,83,.22),rgba(39,24,11,.5));box-shadow:0 0 21px rgba(220,147,57,.13)}.brand-mark svg{width:19px;height:19px}.brand-name{font:700 13px var(--mono);letter-spacing:.18em;color:#f1d7ac}.brand-tag{margin-left:7px;color:#7d6b52;font:8px var(--mono);letter-spacing:.13em}.top-search{height:34px;flex:1;max-width:580px;margin:auto;display:flex;align-items:center;gap:9px;padding:0 11px;border:1px solid rgba(224,178,112,.23);border-radius:18px;background:rgba(27,22,15,.69);color:#b4a183;box-shadow:inset 0 1px rgba(255,255,255,.025)}.top-search svg{width:15px;flex:none}.top-search input{width:100%;border:0;outline:0;background:transparent;color:var(--text);font:11px var(--mono)}.top-search input::placeholder{color:#71634d}.kbd{padding:2px 5px;border:1px solid #55452e;border-radius:4px;color:#927b57;font:8px var(--mono);white-space:nowrap}.top-actions{display:flex;align-items:center;gap:6px;margin-left:auto}.icon-btn,.top-btn,.tiny-btn{height:30px;display:inline-flex;align-items:center;justify-content:center;gap:6px;padding:0 9px;border:1px solid var(--line);border-radius:6px;background:rgba(34,26,16,.68);color:#c9b590;font:9px var(--mono);transition:.18s}.icon-btn{width:31px;padding:0}.icon-btn svg,.top-btn svg,.tiny-btn svg{width:14px;height:14px}.icon-btn:hover,.top-btn:hover,.tiny-btn:hover{border-color:var(--line2);background:rgba(125,78,27,.2);color:#ffe0a1}.top-btn.primary{border-color:rgba(239,185,95,.42);background:linear-gradient(120deg,rgba(193,118,38,.24),rgba(75,48,20,.4));color:#ffdc9a}.statusline{display:flex;align-items:center;gap:16px;overflow:hidden;border-bottom:1px solid rgba(235,184,112,.09);color:#76664f;font:8px var(--mono);letter-spacing:.06em;white-space:nowrap}.statusline b{color:#b49a70;font-weight:500}.status-spacer{margin-left:auto}.live-dot{width:5px;height:5px;display:inline-block;margin-right:5px;border-radius:50%;background:#c9944e;box-shadow:0 0 8px #d99c4d;animation:pulse 2s infinite}.status-active{color:#d6b779}
.workspace{min-height:0;display:grid;grid-template-columns:218px minmax(320px,1fr) 276px;gap:9px;padding:9px 0}.panel{min-width:0;min-height:0;overflow:hidden;border:1px solid var(--line);border-radius:8px;background:linear-gradient(150deg,rgba(25,20,14,.95),rgba(13,11,9,.97));box-shadow:inset 0 1px rgba(255,255,255,.025),0 10px 30px rgba(0,0,0,.2)}.panel-head{height:36px;display:flex;align-items:center;justify-content:space-between;padding:0 10px;border-bottom:1px solid rgba(235,184,112,.11);color:#b39a70;font:8px var(--mono);letter-spacing:.105em}.panel-head .head-right{display:flex;align-items:center;gap:7px;color:#746249;font-size:8px;letter-spacing:.04em}.mini-icon{width:13px;height:13px;color:#c08d49}.left-column{display:grid;grid-template-rows:minmax(0,1fr) 180px;gap:9px;min-height:0}.files-panel{display:flex;flex-direction:column}.tree-root{display:flex;align-items:center;gap:6px;padding:13px 11px 5px;color:#d6c197;font:9px var(--mono)}.tree-root .caret{color:var(--gold)}.tree-path{padding:2px 11px 8px;color:#75664f;font:9px var(--mono)}.file-list{flex:1;min-height:0;overflow:auto;padding:0 6px 8px}.file-row{width:100%;min-height:29px;display:flex;align-items:center;gap:7px;padding:4px 6px;border:1px solid transparent;border-radius:5px;background:transparent;color:#9d8b70;text-align:left;font:9px var(--mono);transition:.15s}.file-row:hover,.file-row.selected{border-color:rgba(217,160,82,.18);background:rgba(180,111,40,.12);color:#f0d49c}.file-glyph{width:13px;color:#cb9751;text-align:center}.file-label{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.file-count{margin-left:auto;color:#79694f;font-size:8px}.file-empty{padding:15px 8px;color:#72634f;font:9px/1.6 var(--mono)}.left-bottom{padding-bottom:8px}.quick-actions{display:grid;grid-template-columns:1fr 1fr;gap:6px;padding:10px}.quick-actions button{height:32px;border:1px solid rgba(232,180,99,.15);border-radius:5px;background:rgba(130,81,29,.1);color:#c9ad7a;text-align:left;padding:0 8px;font:8px var(--mono)}.quick-actions button:hover{background:rgba(184,115,43,.22);color:#ffda96}.storage-meter{padding:0 10px;color:#75654e;font:8px var(--mono)}.meter-track{height:3px;margin:6px 0;background:#2b2116;border-radius:4px}.meter-track i{display:block;width:24%;height:100%;border-radius:4px;background:linear-gradient(90deg,#94622b,#f0bb62);box-shadow:0 0 8px rgba(237,173,80,.4)}
.center-column{min-width:0;min-height:0;display:grid;grid-template-rows:minmax(0,1fr) 148px;gap:9px}.graph-panel{position:relative;display:grid;grid-template-rows:46px minmax(0,1fr) 28px;min-height:0;overflow:hidden;border:1px solid rgba(228,173,98,.2);border-radius:8px;background:#090806}.graph-head{z-index:2;display:flex;align-items:center;justify-content:space-between;gap:8px;padding:0 12px;border-bottom:1px solid rgba(235,184,112,.11);background:linear-gradient(90deg,rgba(79,49,20,.24),rgba(20,15,9,.58))}.graph-title-group{min-width:0}.eyebrow{color:#9f8159;font:8px var(--mono);letter-spacing:.15em}.graph-title{max-width:40vw;margin:3px 0 0;overflow:hidden;color:#eadabf;text-overflow:ellipsis;white-space:nowrap;font:500 13px var(--mono)}.graph-controls{display:flex;align-items:center;gap:5px;color:#a48a60;font:8px var(--mono)}.graph-controls button{width:26px;height:24px;display:grid;place-items:center;border:1px solid rgba(226,172,96,.18);border-radius:4px;background:rgba(166,105,39,.08);color:#d0ac71}.graph-controls button:hover{border-color:rgba(242,188,108,.45);background:rgba(185,116,39,.2);color:#ffe1a3}.zoom-text{min-width:31px;text-align:center}.graph-body{position:relative;min-height:0;overflow:hidden}.graph-body:after{content:"";position:absolute;inset:0;pointer-events:none;background:radial-gradient(ellipse at center,transparent 34%,rgba(0,0,0,.22) 100%);box-shadow:inset 0 0 50px rgba(0,0,0,.3)}#graph{width:100%;height:100%;display:block;cursor:grab;touch-action:none}#graph.dragging{cursor:grabbing}.graph-hud{position:absolute;z-index:1;pointer-events:none;color:#927b5b;font:7px var(--mono);letter-spacing:.1em}.graph-hud.top-left{top:12px;left:12px}.graph-hud.bottom-left{bottom:11px;left:12px}.graph-hud.bottom-right{right:12px;bottom:11px}.graph-hud .signal{display:inline-block;width:5px;height:5px;margin-right:4px;border-radius:50%;background:#e4a952;box-shadow:0 0 6px #df9940}.graph-legend{display:flex;align-items:center;gap:12px;overflow:hidden;padding:0 10px;border-top:1px solid rgba(235,184,112,.1);color:#7b6a51;font:7px var(--mono);white-space:nowrap}.legend-entry{display:flex;align-items:center;gap:5px}.legend-swatch{width:5px;height:5px;border-radius:50%;background:#f0b85c;box-shadow:0 0 5px currentColor}.legend-swatch.cyan{background:#74c7c2}.legend-swatch.rose{background:#df8a7c}.legend-end{margin-left:auto;color:#ae8c56}.lower-row{display:grid;grid-template-columns:1.1fr .9fr;gap:9px;min-height:0}.console-panel,.queue-panel{min-height:0}.console-body{height:calc(100% - 36px);display:flex;flex-direction:column;padding:6px 9px}.console-lines{flex:1;min-height:0;overflow:auto;color:#9b8a6d;font:8px/1.65 var(--mono)}.console-line{white-space:normal}.console-time{color:#66563e}.console-kind{color:#d2a65d}.console-form{height:29px;flex:none;display:flex;align-items:center;gap:7px;padding:0 7px;border:1px solid rgba(225,170,89,.17);border-radius:4px;background:rgba(0,0,0,.3);color:#d99c4d}.console-form input{width:100%;min-width:0;border:0;outline:0;background:transparent;color:#ead9bb;font:8px var(--mono)}.console-form input::placeholder{color:#716049}.console-form button{border:0;background:transparent;color:#d6a859;font:8px var(--mono)}.queue-list{height:calc(100% - 36px);overflow:auto;padding:5px 8px}.queue-item{height:18px;position:relative;display:flex;align-items:center;gap:6px;overflow:hidden;color:#968266;font:7px var(--mono)}.queue-item>span:first-child{color:#68573f}.queue-item b{min-width:0;overflow:hidden;text-overflow:ellipsis;font-weight:400}.queue-item i{margin-left:auto;color:#c09c63;font-style:normal}.queue-bar{position:absolute;bottom:1px;left:20px;width:var(--w);height:1px;background:rgba(211,143,59,.5)}
.inspector{display:flex;flex-direction:column}.inspector-body{flex:1;min-height:0;overflow:auto;padding:12px}.inspector-empty{display:flex;height:100%;min-height:180px;flex-direction:column;align-items:center;justify-content:center;text-align:center;color:#8d7a5d}.empty-orbit{width:52px;height:52px;display:grid;place-items:center;margin-bottom:12px;border:1px solid rgba(233,176,91,.27);border-radius:50%;color:#dfa954;box-shadow:0 0 25px rgba(208,136,44,.12),inset 0 0 15px rgba(219,149,57,.08)}.empty-orbit svg{width:23px;height:23px}.inspector-empty b{color:#d8c39b;font:10px var(--mono)}.inspector-empty p{max-width:180px;color:#73644d;font:8px/1.6 var(--mono)}.inspector-kicker{color:#bf904d;font:8px var(--mono);letter-spacing:.1em}.inspector-title{width:100%;margin:10px 0 3px;padding:0;border:0;background:transparent;color:#f0dfbf;font:500 15px var(--mono)}.inspector-id{color:#6e5c43;font:8px var(--mono)}.field-label{display:block;margin:15px 0 5px;color:#8f7b5b;font:8px var(--mono);letter-spacing:.08em}.field,.field-select,.field-textarea{width:100%;border:1px solid rgba(226,173,96,.16);border-radius:5px;background:rgba(5,5,4,.42);color:#d9c8a7;font:9px/1.55 var(--mono)}.field{height:29px;padding:0 8px}.field-select{height:29px;padding:0 7px}.field-textarea{min-height:102px;padding:8px;resize:vertical}.field::placeholder,.field-textarea::placeholder{color:#5d503e}.tag-list{display:flex;flex-wrap:wrap;gap:5px;margin-top:6px}.tag-pill{display:inline-flex;align-items:center;gap:5px;padding:3px 6px;border:1px solid rgba(226,173,96,.18);border-radius:10px;background:rgba(181,116,45,.1);color:#c9a96e;font:7px var(--mono)}.tag-pill button{padding:0;border:0;background:none;color:#846f4f}.tag-add{display:flex;gap:5px;margin-top:7px}.tag-add input{height:25px}.tag-add button{width:28px;flex:none;border:1px solid rgba(226,173,96,.18);border-radius:4px;background:rgba(181,116,45,.12);color:#d7aa64}.connect-row{display:flex;align-items:center;gap:7px;margin:14px 0 6px}.connect-row b{color:#ad9161;font:8px var(--mono);letter-spacing:.06em}.connect-row button{margin-left:auto;border:1px solid rgba(226,173,96,.2);border-radius:4px;background:rgba(181,116,45,.1);color:#d6ad70;font:7px var(--mono);padding:5px}.connect-row button.connecting{border-color:#edbb68;background:rgba(220,149,53,.25);color:#ffe1a5}.link-list{display:flex;flex-direction:column;gap:3px}.link-chip{width:100%;display:flex;align-items:center;gap:6px;padding:5px 6px;border:1px solid transparent;border-radius:4px;background:transparent;color:#a99571;text-align:left;font:8px var(--mono)}.link-chip:hover{border-color:var(--line);background:rgba(181,116,45,.11)}.link-chip .link-dot{width:5px;height:5px;border-radius:50%;background:#d9a655}.link-chip span:last-child{margin-left:auto;color:#69583f}.inspector-actions{display:flex;gap:6px;padding:9px 11px;border-top:1px solid rgba(235,184,112,.12)}.inspector-actions button{height:29px;flex:1;display:flex;align-items:center;justify-content:center;gap:5px;border:1px solid rgba(226,173,96,.22);border-radius:5px;background:rgba(181,116,45,.1);color:#d6b77e;font:8px var(--mono)}.inspector-actions button:hover{background:rgba(181,116,45,.23)}.inspector-actions .delete-btn{flex:0 0 31px;color:#c57d70}.inspector-actions .save-btn{border-color:rgba(232,184,99,.4);color:#f3cf8b}
.toast-stack{position:fixed;right:18px;bottom:34px;z-index:20;display:flex;flex-direction:column;gap:7px;pointer-events:none}.toast{max-width:320px;padding:9px 12px;border:1px solid rgba(229,178,98,.28);border-radius:6px;background:rgba(20,16,11,.96);color:#e9d7b4;box-shadow:0 10px 30px #0009;font:9px var(--mono);animation:toast-in .2s ease-out}.modal-backdrop{position:fixed;inset:0;z-index:30;display:none;place-items:center;padding:18px;background:rgba(0,0,0,.67);backdrop-filter:blur(8px)}.modal-backdrop.open{display:grid}.modal{width:min(480px,100%);padding:18px;border:1px solid rgba(236,183,94,.32);border-radius:10px;background:#15110c;box-shadow:0 25px 90px #000b}.modal h2{margin:0 0 5px;color:#f1dab0;font:500 14px var(--mono)}.modal p{margin:0 0 15px;color:#8d7a5b;font:9px/1.6 var(--mono)}.modal-actions{display:flex;justify-content:flex-end;gap:7px;margin-top:15px}.modal-actions button{height:30px;padding:0 12px;border:1px solid var(--line);border-radius:5px;background:#21190f;color:#c6ad7b;font:9px var(--mono)}.modal-actions button.primary{border-color:#a9773c;background:#6a451f;color:#ffe3a8}.toast-stack,.modal-backdrop{font-family:var(--mono)}.footer{display:flex;align-items:center;justify-content:space-between;color:#675840;font:7px var(--mono);letter-spacing:.08em}.footer b{color:#b68c51;font-weight:400}.hidden-file{display:none}
@keyframes pulse{50%{opacity:.42;box-shadow:0 0 2px #d99c4d}}@keyframes toast-in{from{transform:translateY(6px);opacity:0}to{transform:none;opacity:1}}@keyframes glow{50%{filter:brightness(1.4)}}@media(max-width:1120px){#app{padding:0 8px}.workspace{grid-template-columns:185px minmax(260px,1fr) 235px;gap:6px}.left-column{grid-template-rows:minmax(0,1fr) 160px}.center-column{grid-template-rows:minmax(0,1fr) 140px}.topbar{gap:9px}.brand{min-width:185px}.brand-tag{display:none}.top-actions .top-btn span{display:none}.top-actions .top-btn{width:32px;padding:0}.lower-row{grid-template-columns:1fr}.queue-panel{display:none}}@media(max-width:820px){.workspace{grid-template-columns:1fr 235px}.left-column{display:none}.center-column{grid-column:1}.topbar{gap:7px}.brand{min-width:34px}.brand-name{display:none}.statusline{gap:9px}.statusline .optional{display:none}.top-search{max-width:none}.top-actions .top-btn{width:30px}.graph-title{max-width:34vw}}@media(max-width:560px){#app{grid-template-rows:52px 23px minmax(0,1fr) 20px;padding:0 5px}.workspace{grid-template-columns:1fr;padding:5px 0}.inspector{position:fixed;z-index:10;top:80px;right:7px;bottom:28px;width:min(340px,calc(100vw - 20px));display:flex;transform:translateX(calc(100% + 12px));pointer-events:none;transition:transform .22s ease,visibility .22s;visibility:hidden;box-shadow:0 18px 55px #000c}.inspector.mobile-open{transform:translateX(0);pointer-events:auto;visibility:visible}.close-inspector{display:inline-grid;width:21px;height:21px;place-items:center;border:1px solid var(--line);border-radius:4px;background:#21190f;color:#d7b477;font:14px var(--mono)}.center-column{grid-template-rows:minmax(0,1fr) 132px;gap:5px}.graph-panel{grid-template-rows:41px minmax(0,1fr) 25px}.graph-title{font-size:11px}.graph-hud.bottom-left{display:none}.graph-legend{gap:7px;padding:0 6px;font-size:6px}.graph-controls button{width:23px;height:22px}.brand-mark{width:27px;height:27px}.top-search{height:29px;padding:0 7px}.top-search input{font-size:9px}.top-search .kbd{display:none}.top-actions{gap:3px}.top-actions .top-btn{height:27px;width:27px}.top-actions .icon-btn{height:27px;width:27px}.top-actions svg{width:12px}.statusline{font-size:6px}.lower-row{gap:5px}.footer{font-size:6px}.toast-stack{right:8px;bottom:28px}}
@media(prefers-reduced-motion:reduce){*,*:before,*:after{scroll-behavior:auto!important;animation-duration:.01ms!important;animation-iteration-count:1!important;transition-duration:.01ms!important}}
</style>
</head>
<body>
<div id="app">
 <header class="topbar">
  <div class="brand"><div class="brand-mark" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.35"><circle cx="12" cy="12" r="2.2"/><ellipse cx="12" cy="12" rx="9" ry="4.7" transform="rotate(-35 12 12)"/><ellipse cx="12" cy="12" rx="9" ry="4.7" transform="rotate(35 12 12)"/><circle cx="18.8" cy="6.1" r="1" fill="currentColor"/></svg></div><div><span class="brand-name">NEXUS</span><span class="brand-tag">MEMORY OS</span></div></div>
  <label class="top-search"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="10.8" cy="10.8" r="6.8"/><path d="m16 16 5 5"/></svg><input id="search" type="search" placeholder="Find a note, person, project…" autocomplete="off"><span class="kbd">⌘ K</span></label>
  <div class="top-actions"><button class="top-btn" id="newNodeBtn" title="Create a new note"><span>＋ NEW NOTE</span><b>＋</b></button><button class="top-btn" id="exportBtn" title="Export a JSON backup"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 3v12m-5-5 5 5 5-5M5 20h14"/></svg><span>EXPORT</span></button><button class="top-btn" id="importBtn" title="Import a JSON backup"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 16V4m-5 5 5-5 5 5M5 20h14"/></svg><span>IMPORT</span></button><input id="importFile" class="hidden-file" type="file" accept="application/json,.json"><button class="icon-btn" id="helpBtn" title="Keyboard shortcuts" aria-label="Help">?</button></div>
 </header>
 <div class="statusline"><span><i class="live-dot"></i><b id="saveStatus">LOCAL STORAGE READY</b></span><span>MAP <b>NEXUS-01</b></span><span><b id="nodeCount">00</b> NODES</span><span><b id="linkCount">00</b> CONNECTIONS</span><span class="status-spacer optional">LAST SYNC <b id="lastSync">—</b></span><span class="status-active">● PRIVATE BY DEFAULT</span></div>
 <main class="workspace">
  <div class="left-column">
   <aside class="panel files-panel"><div class="panel-head"><span>01 / FILE SYSTEM</span><span class="head-right">LOCAL <svg class="mini-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="m5 12 4 4L19 6"/></svg></span></div><div class="tree-root"><span class="caret">⌄</span><span>~/second-brain</span></div><div class="tree-path" id="filePath">memory/</div><div class="file-list" id="fileList"></div></aside>
   <section class="panel left-bottom"><div class="panel-head"><span>02 / WORKSPACE</span><span class="head-right" id="storageLabel">LOCAL</span></div><div class="quick-actions"><button id="focusBtn">⌕ &nbsp; Search map</button><button id="randomBtn">✦ &nbsp; Surprise me</button><button id="resetBtn">↺ &nbsp; Restore demo</button><button id="exportSmallBtn">⇩ &nbsp; Save backup</button></div><div class="storage-meter"><span id="storageText">0 KB / 2 MB SAVED</span><div class="meter-track"><i id="meterBar"></i></div>EDITS SYNC AUTOMATICALLY</div></section>
  </div>
  <div class="center-column">
   <section class="graph-panel"><div class="graph-head"><div class="graph-title-group"><div class="eyebrow">LIVE KNOWLEDGE GRAPH / <span id="graphMode">OVERVIEW</span></div><div class="graph-title" id="graphTitle">Map of the mind. Under 200 lines.</div></div><div class="graph-controls"><button id="zoomOut" title="Zoom out" aria-label="Zoom out">−</button><span class="zoom-text" id="zoomText">100%</span><button id="zoomIn" title="Zoom in" aria-label="Zoom in">＋</button><button id="centerBtn" title="Center graph" aria-label="Center graph">◎</button></div></div><div class="graph-body" id="graphBody"><canvas id="graph" aria-label="Interactive memory map"></canvas><div class="graph-hud top-left"><i class="signal"></i>GRAPH ENGINE <span id="engineFps">/ LIVE</span></div><div class="graph-hud bottom-left">DRAG TO ORBIT <span>·</span> SCROLL TO ZOOM <span>·</span> CLICK TO INSPECT</div><div class="graph-hud bottom-right" id="coords">X 000 / Y 000</div></div><div class="graph-legend"><span class="legend-entry"><i class="legend-swatch"></i>CORE NOTE</span><span class="legend-entry"><i class="legend-swatch cyan"></i>REFERENCE</span><span class="legend-entry"><i class="legend-swatch rose"></i>DECISION</span><span class="legend-end" id="matchCount">12 / 12 VISIBLE</span></div></section>
   <div class="lower-row"><section class="panel console-panel"><div class="panel-head"><span>03 / SYSTEM CHAT</span><span class="head-right">NEXUS SHELL</span></div><div class="console-body"><div class="console-lines" id="consoleLines"></div><form class="console-form" id="commandForm"><span>›</span><input name="command" id="commandInput" autocomplete="off" placeholder="help · new · search · connect"><button>RUN</button></form></div></section><section class="panel queue-panel"><div class="panel-head"><span>04 / RECENT ACTIVITY</span><span class="head-right" id="activityCount">— EVENTS</span></div><div class="queue-list" id="activityList"></div></section></div>
  </div>
  <aside class="panel inspector" id="inspectorPanel"><div class="panel-head"><span>05 / NOTE INSPECTOR</span><span class="head-right"><span id="inspectorState">NO SELECTION</span><button class="close-inspector" id="closeInspector" aria-label="Close note inspector">×</button></span></div><div class="inspector-body" id="inspectorBody"><div class="inspector-empty"><div class="empty-orbit"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4"><circle cx="12" cy="12" r="2"/><ellipse cx="12" cy="12" rx="9" ry="4.5" transform="rotate(-30 12 12)"/><ellipse cx="12" cy="12" rx="9" ry="4.5" transform="rotate(30 12 12)"/></svg></div><b>SELECT A MEMORY</b><p>Click a node or file to inspect, edit, and connect it.</p></div></div><div class="inspector-actions"><button class="save-btn" id="saveNowBtn">✓ &nbsp; SAVED</button><button class="delete-btn" id="deleteBtn" title="Delete selected note" aria-label="Delete selected note">⌫</button></div></aside>
 </main>
 <footer class="footer"><span>NEXUS / LOCAL-FIRST KNOWLEDGE GRAPH</span><span>ALL NOTES STAY ON THIS MACHINE <b>●</b></span></footer>
</div>
<div class="toast-stack" id="toastStack" aria-live="polite"></div><div class="modal-backdrop" id="modal"><section class="modal" id="modalBody"></section></div>
<script>
'use strict';
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const TYPES=['core','note','project','decision','person','task','reference'];
const COLORS={amber:'#efb95f',gold:'#e0a542',cyan:'#76c9c5',rose:'#df8f81',violet:'#b69bf4',mint:'#9bc786'};
const model={nodes:[],logs:[],savedAt:null,selected:null,filter:'',zoom:1,rotation:0,panX:0,panY:0,positions:[],dust:null,raf:0,drag:null,connectMode:false,dirty:false,saveTimer:0,searchIndex:0,revision:0};
let reducedMotion=matchMedia('(prefers-reduced-motion: reduce)').matches;
const typeIcon=t=>({core:'◆',note:'◈',project:'▣',decision:'◇',person:'◉',task:'☷',reference:'⊙'}[t]||'◈');
const nodeById=id=>model.nodes.find(n=>n.id===id);
const filteredNodes=()=>model.nodes.filter(n=>!model.filter||`${n.title} ${n.content} ${n.tags.join(' ')} ${n.type}`.toLowerCase().includes(model.filter.toLowerCase()));
const linkedCount=()=>model.nodes.reduce((sum,n)=>sum+n.links.length,0)/2;
const timeLabel=iso=>{try{return new Date(iso).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})}catch{return'--:--'}};
function toast(message){const el=document.createElement('div');el.className='toast';el.textContent=message;$('#toastStack').appendChild(el);setTimeout(()=>el.remove(),2800)}
function logLine(text,kind='nexus'){
 model.logs.unshift({at:new Date().toISOString(),text:String(text).slice(0,180),kind});model.logs=model.logs.slice(0,60);renderActivity();renderConsole();debounceSave();
}
async function api(path,options={}){const res=await fetch(path,{headers:{'Content-Type':'application/json',...(options.headers||{})},...options});let body={};try{body=await res.json()}catch{}if(!res.ok)throw new Error(body.error||`Request failed (${res.status})`);return body}
function debounceSave(){model.dirty=true;model.revision++;$('#saveStatus').textContent='SAVING…';$('#saveStatus').style.color='#e8b764';clearTimeout(model.saveTimer);model.saveTimer=setTimeout(saveState,420)}
async function saveState(){
 if(!model.dirty)return;const revision=model.revision;
 try{const state=await api('/api/state',{method:'PUT',body:JSON.stringify({version:1,nodes:model.nodes,logs:model.logs})});model.savedAt=state.savedAt;$('#lastSync').textContent=timeLabel(state.savedAt);if(model.revision===revision){model.dirty=false;$('#saveStatus').textContent='ALL CHANGES SAVED';$('#saveStatus').style.color='';$('#saveNowBtn').textContent='✓  SAVED';const node=nodeById(model.selected);if(node)$('#inspectorState').textContent='NODE / '+node.type.toUpperCase()}else{model.dirty=true;model.saveTimer=setTimeout(saveState,90)}updateCounts();}
 catch(err){$('#saveStatus').textContent='SAVE FAILED';$('#saveStatus').style.color='#e08b78';toast(err.message)}
}
function updateCounts(){
 $('#nodeCount').textContent=String(model.nodes.length).padStart(2,'0');$('#linkCount').textContent=String(Math.round(linkedCount())).padStart(2,'0');
 const visible=filteredNodes().length;$('#matchCount').textContent=`${String(visible).padStart(2,'0')} / ${String(model.nodes.length).padStart(2,'0')} VISIBLE`;
 $('#storageText').textContent=`${Math.max(1,Math.round(new Blob([JSON.stringify({nodes:model.nodes,logs:model.logs})]).size/1024))} KB / 2 MB SAVED`;
 $('#meterBar').style.width=Math.min(100,Math.max(5,JSON.stringify(model.nodes).length/(2*1024*1024)*100))+'%';
}
function renderFiles(){
 const rows=filteredNodes().slice().sort((a,b)=>a.title.localeCompare(b.title));
 $('#fileList').innerHTML=rows.length?rows.map(n=>`<button class="file-row${n.id===model.selected?' selected':''}" data-select="${esc(n.id)}"><span class="file-glyph">${typeIcon(n.type)}</span><span class="file-label">${esc(n.title)}</span><span class="file-count">${n.links.length}</span></button>`).join(''):`<div class="file-empty">No matches. Try another search or create a note.</div>`;
 $('#filePath').textContent=model.filter?`search / ${model.filter}`:'memory/';
}
function renderActivity(){
 const logs=model.logs.slice(0,8);$('#activityCount').textContent=`${String(model.logs.length).padStart(2,'0')} EVENTS`;
 $('#activityList').innerHTML=logs.map((l,i)=>`<div class="queue-item"><span>${timeLabel(l.at)}</span><b>${esc(l.text)}</b><i>${i===0?'NOW':'LOG'}</i><em class="queue-bar" style="--w:${Math.max(12,92-i*9)}%"></em></div>`).join('')||'<div class="file-empty">No activity yet.</div>';
}
function renderConsole(){
 $('#consoleLines').innerHTML=model.logs.slice(0,4).reverse().map(l=>`<div class="console-line"><span class="console-time">[${timeLabel(l.at)}]</span> <b class="console-kind">${esc(l.kind||'nexus')}</b> &nbsp;${esc(l.text)}</div>`).join('');
 const box=$('#consoleLines');box.scrollTop=box.scrollHeight;
}
function renderInspector(){
 const node=nodeById(model.selected),body=$('#inspectorBody');
 if(!node){$('#inspectorState').textContent='NO SELECTION';body.innerHTML='<div class="inspector-empty"><div class="empty-orbit">◉</div><b>SELECT A MEMORY</b><p>Click a node or file to inspect, edit, and connect it.</p></div>';$('#saveNowBtn').textContent='✓  SAVED';$('#deleteBtn').disabled=true;return}
 $('#inspectorState').textContent=model.dirty?'EDITING':'NODE / '+node.type.toUpperCase();$('#deleteBtn').disabled=false;
 body.innerHTML=`<div class="inspector-kicker">${typeIcon(node.type)} &nbsp; ${esc(node.type.toUpperCase())} / MEMORY NODE</div><input class="inspector-title" id="editTitle" maxlength="120" value="${esc(node.title)}" aria-label="Note title"><div class="inspector-id">ID / ${esc(node.id)} · UPDATED ${timeLabel(node.updatedAt)}</div>
 <label class="field-label" for="editType">NODE TYPE</label><select id="editType" class="field-select">${TYPES.map(t=>`<option value="${t}"${node.type===t?' selected':''}>${t.toUpperCase()}</option>`).join('')}</select>
 <label class="field-label" for="editContent">NOTE CONTENT</label><textarea id="editContent" class="field-textarea" maxlength="8000" placeholder="Write the useful detail here…">${esc(node.content)}</textarea>
 <label class="field-label">TAGS</label><div class="tag-list">${node.tags.map((tag,i)=>`<span class="tag-pill">${esc(tag)}<button data-remove-tag="${i}" aria-label="Remove tag">×</button></span>`).join('')||'<span style="color:#64543e;font:8px var(--mono)">No tags yet</span>'}</div><form class="tag-add" id="tagForm"><input class="field" name="tag" maxlength="32" placeholder="Add a tag…"><button aria-label="Add tag">+</button></form>
 <div class="connect-row"><b>CONNECTIONS / ${node.links.length}</b><button id="connectBtn" class="${model.connectMode?'connecting':''}">${model.connectMode?'PICK ANOTHER NODE':'＋ CONNECT'}</button></div><div class="link-list">${node.links.map(id=>nodeById(id)).filter(Boolean).map(n=>`<button class="link-chip" data-select="${esc(n.id)}"><i class="link-dot"></i><span>${esc(n.title)}</span><span>↗</span></button>`).join('')||'<span style="color:#64543e;font:8px var(--mono)">No linked notes</span>'}</div>`;
 $('#saveNowBtn').textContent=model.dirty?'●  SAVE NOW':'✓  SAVED';
}
function renderAll(){renderFiles();renderInspector();renderActivity();renderConsole();updateCounts();drawGraph()}
function selectNode(id){if(!nodeById(id))return;model.selected=id;model.connectMode=false;renderFiles();renderInspector();drawGraph();if(innerWidth<=560)$('#inspectorPanel').classList.add('mobile-open')}
function nodeColor(n){return COLORS[n.color]||COLORS.amber}
function graphPositions(w,h){
 const pad=38,positions=[];
 for(const n of model.nodes){positions.push({id:n.id,x:pad+n.x*(w-pad*2),y:pad+n.y*(h-pad*2),r:n.id===model.selected?9:6.5})}
 return positions;
}
function drawGraph(time=performance.now()){
 const canvas=$('#graph');if(!canvas)return;const rect=canvas.getBoundingClientRect();if(rect.width<2||rect.height<2)return;
 const dpr=Math.min(devicePixelRatio||1,2),w=rect.width,h=rect.height;
 if(canvas.width!==Math.round(w*dpr)||canvas.height!==Math.round(h*dpr)){canvas.width=Math.round(w*dpr);canvas.height=Math.round(h*dpr)}
 const ctx=canvas.getContext('2d');ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,w,h);
 const bg=ctx.createRadialGradient(w*.51,h*.49,0,w*.51,h*.49,Math.max(w,h)*.76);bg.addColorStop(0,'#21160c');bg.addColorStop(.5,'#100c08');bg.addColorStop(1,'#080706');ctx.fillStyle=bg;ctx.fillRect(0,0,w,h);
 if(!model.dust){let seed=7019;const rand=()=>{seed=(seed*48271)%2147483647;return(seed-1)/2147483646};model.dust=Array.from({length:190},()=>({x:rand(),y:rand(),r:.25+rand()*1.3,a:.05+rand()*.32,phase:rand()*7}))}
 for(const p of model.dust){const tw=reducedMotion?1:.65+.35*Math.sin(time*.0007+p.phase);ctx.fillStyle=`rgba(255,193,117,${p.a*tw})`;ctx.beginPath();ctx.arc(p.x*w,p.y*h,p.r,0,Math.PI*2);ctx.fill()}
 ctx.save();ctx.translate(w/2+model.panX,h/2+model.panY);ctx.rotate(model.rotation);ctx.scale(model.zoom,model.zoom);ctx.translate(-w/2,-h/2);
 ctx.strokeStyle='rgba(242,189,119,.035)';ctx.lineWidth=1;for(let x=(w/2)%44;x<w;x+=44){ctx.beginPath();ctx.moveTo(x,0);ctx.lineTo(x,h);ctx.stroke()}for(let y=(h/2)%44;y<h;y+=44){ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(w,y);ctx.stroke()}
 const pos=graphPositions(w,h),byId=new Map(pos.map(p=>[p.id,p])),visible=new Set(filteredNodes().map(n=>n.id));model.positions=pos;
 const links=new Set();for(const node of model.nodes){if(!visible.has(node.id))continue;for(const otherId of node.links){if(!visible.has(otherId))continue;const key=[node.id,otherId].sort().join('|');if(links.has(key))continue;links.add(key);const a=byId.get(node.id),b=byId.get(otherId);if(!a||!b)continue;const related=node.id===model.selected||otherId===model.selected;ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.quadraticCurveTo((a.x+b.x)/2+(h/2-(a.y+b.y)/2)*.09,(a.y+b.y)/2+(b.x-a.x)*.09,b.x,b.y);ctx.strokeStyle=related?'rgba(250,191,103,.49)':'rgba(228,171,98,.14)';ctx.lineWidth=related?1.2:.7;ctx.shadowColor=related?'#c88337':'transparent';ctx.shadowBlur=related?9:0;ctx.stroke();ctx.shadowBlur=0;
  if(!reducedMotion&&related){const t=(time*.00012+(node.id.length*.13))%1;const px=a.x+(b.x-a.x)*t,py=a.y+(b.y-a.y)*t;ctx.beginPath();ctx.fillStyle='rgba(255,221,161,.9)';ctx.arc(px,py,1.6,0,Math.PI*2);ctx.fill()}}}
 // Ambient satellite strands create depth without changing the actual editable graph.
 if(!reducedMotion)pos.forEach((p,i)=>{if(i%2)return;for(let j=0;j<2;j++){const ang=time*.00011+i*1.7+j*2.8,rr=16+j*12,sx=p.x+Math.cos(ang)*rr,sy=p.y+Math.sin(ang)*rr*.7;ctx.beginPath();ctx.moveTo(p.x,p.y);ctx.lineTo(sx,sy);ctx.strokeStyle='rgba(224,157,72,.12)';ctx.lineWidth=.65;ctx.stroke();ctx.beginPath();ctx.fillStyle='rgba(255,202,127,.48)';ctx.arc(sx,sy,1,0,Math.PI*2);ctx.fill()}});
 for(const n of model.nodes){const p=byId.get(n.id);if(!p)continue;const match=visible.has(n.id),selected=n.id===model.selected;const base=selected?11:8,r=base+(selected&&!reducedMotion?Math.sin(time*.004)*.8:0),color=nodeColor(n);ctx.save();ctx.globalAlpha=match?1:.13;const glow=ctx.createRadialGradient(p.x,p.y,0,p.x,p.y,r*4);glow.addColorStop(0,color+'aa');glow.addColorStop(.22,color+'55');glow.addColorStop(1,color+'00');ctx.fillStyle=glow;ctx.beginPath();ctx.arc(p.x,p.y,r*4,0,Math.PI*2);ctx.fill();ctx.beginPath();ctx.arc(p.x,p.y,r,0,Math.PI*2);ctx.fillStyle=color;ctx.shadowColor=color;ctx.shadowBlur=selected?18:8;ctx.fill();ctx.beginPath();ctx.arc(p.x-2,p.y-2,Math.max(1,r*.3),0,Math.PI*2);ctx.fillStyle='#fff2d5';ctx.fill();ctx.restore();
  const right=p.x>w*.73,anchor=right?'right':'left',tx=right?p.x-15:p.x+15;ctx.save();ctx.globalAlpha=match?1:.2;ctx.textAlign=anchor;ctx.shadowColor='#000';ctx.shadowBlur=6;ctx.font=`${selected?'600 10px':'9px'} ui-monospace, monospace`;ctx.fillStyle=selected?'#ffe2a5':'#d6c4a2';ctx.fillText(n.title,tx,p.y-2);ctx.font='7px ui-monospace, monospace';ctx.fillStyle='#967e5c';ctx.fillText(`${n.type.toUpperCase()} / ${n.tags.slice(0,2).join(' · ')||'NO TAGS'}`,tx,p.y+10);ctx.restore();
 }
 const core=byId.get('brain')||pos[0];if(core){ctx.beginPath();ctx.arc(core.x,core.y,21+(reducedMotion?0:Math.sin(time*.003)*2),0,Math.PI*2);ctx.strokeStyle='rgba(238,183,98,.4)';ctx.lineWidth=.8;ctx.setLineDash([2,5]);ctx.stroke();ctx.setLineDash([])}
 ctx.restore();
}
function animate(t){const panel=$('#graphBody');if(!panel||document.hidden){model.raf=setTimeout(()=>{model.raf=requestAnimationFrame(animate)},180);return}drawGraph(t);if(reducedMotion){model.raf=0;return}model.raf=requestAnimationFrame(animate)}
function startAnimation(){if(!model.raf)model.raf=requestAnimationFrame(animate)}
function saveSelection(){const node=nodeById(model.selected);if(!node)return;node.updatedAt=new Date().toISOString();$('#inspectorState').textContent='EDITING';$('#saveNowBtn').textContent='●  SAVE NOW';debounceSave()}
function addNode(title='Untitled note',type='note',content=''){
 if(model.nodes.length>=250){toast('This map has reached its 250-note limit.');return null}
 const id='n-'+Math.random().toString(36).slice(2,9),angle=Math.random()*Math.PI*2;
 const node={id,title,type,content,tags:[],x:.5+Math.cos(angle)*(.12+Math.random()*.18),y:.5+Math.sin(angle)*(.12+Math.random()*.18),color:type==='decision'?'rose':type==='reference'?'cyan':'amber',links:model.selected&&model.selected!==id?[model.selected]:[],createdAt:new Date().toISOString(),updatedAt:new Date().toISOString()};
 model.nodes.push(node);if(model.selected){const prev=nodeById(model.selected);if(prev&&!prev.links.includes(id))prev.links.push(id)}model.selected=id;renderAll();if(innerWidth<=560)$('#inspectorPanel').classList.add('mobile-open');debounceSave();logLine(`Created ${title}.`,'create');setTimeout(()=>$('#editTitle')?.focus(),20);return node;
}
function removeNode(){const node=nodeById(model.selected);if(!node)return;if(model.nodes.length<=1){toast('Keep at least one note in the map.');return}confirmModal(`Delete “${node.title}”?`,`This removes the note and its ${node.links.length} connections from your local map.`,()=>{model.nodes=model.nodes.filter(n=>n.id!==node.id);for(const n of model.nodes)n.links=n.links.filter(id=>id!==node.id);model.selected=null;model.connectMode=false;renderAll();debounceSave();logLine(`Deleted ${node.title}.`,'delete');toast('Note removed')})}
function toggleLink(aId,bId){if(!aId||!bId||aId===bId)return;const a=nodeById(aId),b=nodeById(bId);if(!a||!b)return;const exists=a.links.includes(bId);a.links=exists?a.links.filter(x=>x!==bId):[...a.links,bId];b.links=exists?b.links.filter(x=>x!==aId):[...b.links,aId];model.connectMode=false;renderFiles();renderInspector();drawGraph();debounceSave();logLine(exists?`Disconnected ${a.title} ↔ ${b.title}.`:`Connected ${a.title} ↔ ${b.title}.`,'graph')}
function connectSelected(target){if(!model.connectMode||!model.selected||target===model.selected)return;toggleLink(model.selected,target)}
function renderAllAndSave(){renderAll();debounceSave()}
function exportJson(){const data={version:1,exportedAt:new Date().toISOString(),nodes:model.nodes,logs:model.logs};const blob=new Blob([JSON.stringify(data,null,2)],{type:'application/json'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=`nexus-memory-${new Date().toISOString().slice(0,10)}.json`;a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000);logLine('Created a JSON backup.','export');toast('Backup downloaded')}
function importJson(file){const reader=new FileReader();reader.onload=async()=>{try{const data=JSON.parse(reader.result);if(!Array.isArray(data.nodes)||!data.nodes.length)throw new Error('This file has no valid nodes.');const clean=await api('/api/state',{method:'PUT',body:JSON.stringify(data)});model.nodes=clean.nodes;model.logs=clean.logs||[];model.selected=model.nodes[0]?.id||null;renderAll();toast('Map imported successfully');logLine(`Imported ${model.nodes.length} notes.`,'import')}catch(e){toast(`Import failed: ${e.message}`)}};reader.readAsText(file)}
function toastModal(title,message,actions){$('#modalBody').innerHTML=`<h2>${esc(title)}</h2><p>${esc(message)}</p><div class="modal-actions">${actions.map((a,i)=>`<button data-modal-action="${i}" class="${a.primary?'primary':''}">${esc(a.label)}</button>`).join('')}</div>`;$('#modal').classList.add('open');$('#modal')._actions=actions}
function confirmModal(title,message,onConfirm){toastModal(title,message,[{label:'CANCEL',run:()=>{}},{label:'DELETE NOTE',primary:true,run:onConfirm}])}
function searchTerm(term){model.filter=String(term||'').trim();renderFiles();updateCounts();drawGraph();$('#graphMode').textContent=model.filter?'FILTERED VIEW':'OVERVIEW';const node=filteredNodes()[0];if(model.filter&&node){model.selected=node.id;renderInspector();if(innerWidth<=560)$('#inspectorPanel').classList.add('mobile-open')}if(!model.filter)$('#graphMode').textContent='OVERVIEW'}
function command(text){const raw=text.trim();if(!raw)return;logLine(`$ ${raw}`,'you');const parts=raw.split(/\s+/),verb=parts.shift().toLowerCase(),rest=parts.join(' ');
 if(verb==='help'){logLine('Commands: help · new <title> · search <term> · connect <id> <id> · export','help');return}
 if(verb==='new'){if(!rest){toast('Try: new a useful note');return}addNode(rest,'note','');return}
 if(verb==='search'){searchTerm(rest);$('#search').value=rest;return}
 if(verb==='export'){exportJson();return}
 if(verb==='connect'){const [a,b]=rest.split(/\s+/);if(!nodeById(a)||!nodeById(b)){toast('Use: connect <node-id> <node-id>');return}toggleLink(a,b);return}
 if(verb==='clear'){searchTerm('');$('#search').value='';return}
 searchTerm(raw);$('#search').value=raw;
}
function openHelp(){toastModal('NEXUS keyboard guide','Create notes, connect ideas, and move around your map without leaving the keyboard.',[{label:'⌘ / CTRL + K  Search',run:()=>$('#search').focus()},{label:'N  New note',run:()=>addNode()},{label:'ESC  Close',run:()=>{}}])}
function centerGraph(){model.zoom=1;model.rotation=0;model.panX=0;model.panY=0;$('#zoomText').textContent='100%';drawGraph()}
function bindCanvas(){const c=$('#graph');if(!c)return;
 c.addEventListener('pointerdown',e=>{model.drag={x:e.clientX,y:e.clientY,rot:model.rotation,panX:model.panX,panY:model.panY,moved:false};c.setPointerCapture(e.pointerId);c.classList.add('dragging')});
 c.addEventListener('pointermove',e=>{const r=c.getBoundingClientRect(),x=e.clientX-r.left,y=e.clientY-r.top;$('#coords').textContent=`X ${String(Math.round(x)).padStart(3,'0')} / Y ${String(Math.round(y)).padStart(3,'0')}`;if(model.drag){const dx=e.clientX-model.drag.x,dy=e.clientY-model.drag.y;if(Math.abs(dx)+Math.abs(dy)>4)model.drag.moved=true;if(model.drag.moved){model.rotation=model.drag.rot+dx*.004;model.panX=model.drag.panX+dx*.35;model.panY=model.drag.panY+dy*.35;drawGraph()}}});
 const release=e=>{if(!model.drag)return;const drag=model.drag;model.drag=null;c.classList.remove('dragging');if(!drag.moved){const r=c.getBoundingClientRect(),x=e.clientX-r.left,y=e.clientY-r.top,inv=(xx,yy)=>{const px=xx-c.clientWidth/2-model.panX,py=yy-c.clientHeight/2-model.panY,co=Math.cos(-model.rotation),si=Math.sin(-model.rotation);return{x:(px*co-py*si)/model.zoom+c.clientWidth/2,y:(px*si+py*co)/model.zoom+c.clientHeight/2}};const p=inv(x,y),hits=model.positions.map(q=>({q,d:Math.hypot(p.x-q.x,p.y-q.y)})).sort((a,b)=>a.d-b.d);if(hits[0]?.d<24){if(model.connectMode)connectSelected(hits[0].q.id);else selectNode(hits[0].q.id)}else if(model.connectMode){model.connectMode=false;renderInspector()}}};
 c.addEventListener('pointerup',release);c.addEventListener('pointercancel',release);c.addEventListener('wheel',e=>{e.preventDefault();model.zoom=Math.max(.45,Math.min(2.6,model.zoom*(e.deltaY<0?1.08:.92)));$('#zoomText').textContent=Math.round(model.zoom*100)+'%';drawGraph()},{passive:false});
 c.addEventListener('dblclick',e=>{const r=c.getBoundingClientRect(),x=e.clientX-r.left,y=e.clientY-r.top,best=model.positions.map(p=>({p,d:Math.hypot(x-p.x,y-p.y)})).sort((a,b)=>a.d-b.d)[0];if(best&&best.d<28){selectNode(best.p.id);$('#editTitle')?.focus()}})
}
function bindEvents(){
 document.addEventListener('click',e=>{
  const select=e.target.closest('[data-select]');if(select){if(model.connectMode)connectSelected(select.dataset.select);else selectNode(select.dataset.select);return}
  const removeTag=e.target.closest('[data-remove-tag]');if(removeTag){const n=nodeById(model.selected);n?.tags.splice(Number(removeTag.dataset.removeTag),1);renderInspector();debounceSave();return}
  if(e.target.closest('#newNodeBtn')){addNode();return}
  if(e.target.closest('#deleteBtn')){removeNode();return}
  if(e.target.closest('#connectBtn')){model.connectMode=!model.connectMode;renderInspector();toast(model.connectMode?'Pick another node in the graph to connect it.':'Connection mode cancelled');return}
  if(e.target.closest('#zoomIn')){model.zoom=Math.min(2.6,model.zoom*1.12);$('#zoomText').textContent=Math.round(model.zoom*100)+'%';drawGraph();return}
  if(e.target.closest('#zoomOut')){model.zoom=Math.max(.45,model.zoom/1.12);$('#zoomText').textContent=Math.round(model.zoom*100)+'%';drawGraph();return}
  if(e.target.closest('#centerBtn')){centerGraph();return}
  if(e.target.closest('#exportBtn')||e.target.closest('#exportSmallBtn')){exportJson();return}
  if(e.target.closest('#importBtn')){$('#importFile').click();return}
  if(e.target.closest('#resetBtn')){confirmModal('Restore the starter map?','This replaces the current local map with the original NEXUS example. Export a backup first if you want to keep your notes.',async()=>{try{const data=await api('/api/reset',{method:'POST'});model.nodes=data.nodes;model.logs=data.logs;model.selected=model.nodes[0]?.id||null;renderAll();toast('Starter map restored')}catch(err){toast(err.message)}});return}
  if(e.target.closest('#focusBtn')){$('#search').focus();$('#search').select();return}
  if(e.target.closest('#randomBtn')){const list=filteredNodes();if(list.length)selectNode(list[Math.floor(Math.random()*list.length)].id);return}
  if(e.target.closest('#helpBtn')){openHelp();return}
  if(e.target.closest('#closeInspector')){$('#inspectorPanel').classList.remove('mobile-open');return}
  const modalAction=e.target.closest('[data-modal-action]');if(modalAction){const fn=$('#modal')._actions?.[Number(modalAction.dataset.modalAction)]?.run;$('#modal').classList.remove('open');fn?.();return}
  if(e.target.id==='modal')$('#modal').classList.remove('open');
 });
 document.addEventListener('input',e=>{
  if(e.target.id==='search'){searchTerm(e.target.value);return}
  const n=nodeById(model.selected);if(!n)return;
  if(e.target.id==='editTitle'){n.title=e.target.value||'Untitled note';$('#graphTitle').textContent=n.title;renderFiles();drawGraph();saveSelection();return}
  if(e.target.id==='editContent'){n.content=e.target.value;saveSelection();return}
  if(e.target.id==='editType'){n.type=e.target.value;saveSelection();renderFiles();drawGraph();return}
 });
 document.addEventListener('change',e=>{const n=nodeById(model.selected);if(e.target.id==='editType'&&n){n.type=e.target.value;n.updatedAt=new Date().toISOString();renderFiles();renderInspector();drawGraph();debounceSave()}});
 document.addEventListener('submit',e=>{
  if(e.target.id==='commandForm'){e.preventDefault();const input=$('#commandInput');command(input.value);input.value='';return}
  if(e.target.id==='tagForm'){e.preventDefault();const n=nodeById(model.selected),value=new FormData(e.target).get('tag')?.toString().trim();if(n&&value&&!n.tags.includes(value)){n.tags.push(value.slice(0,32));renderInspector();debounceSave()}return}
 });
 $('#importFile').addEventListener('change',e=>{const f=e.target.files?.[0];if(f)importJson(f);e.target.value=''});
 $('#saveNowBtn').addEventListener('click',saveState);
 $('#search').addEventListener('keydown',e=>{if(e.key==='ArrowDown'&&filteredNodes().length){e.preventDefault();model.searchIndex=(model.searchIndex+1)%filteredNodes().length;selectNode(filteredNodes()[model.searchIndex].id)}if(e.key==='Enter'&&filteredNodes().length)selectNode(filteredNodes()[0].id)});
 addEventListener('keydown',e=>{const typing=/INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName||'');if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='k'){e.preventDefault();$('#search').focus();$('#search').select();return}if(e.key==='Escape'){$('#modal').classList.remove('open');$('#search').blur();model.connectMode=false;renderInspector();return}if(!typing&&e.key.toLowerCase()==='n'){e.preventDefault();addNode();return}if(!typing&&e.key==='/'){e.preventDefault();$('#search').focus();return}if(e.key==='Delete'&&!typing)removeNode()});
 addEventListener('resize',()=>drawGraph());document.addEventListener('visibilitychange',()=>{if(!document.hidden)drawGraph()});
}
async function boot(){
 try{const state=await api('/api/state');model.nodes=state.nodes;model.logs=state.logs||[];model.savedAt=state.savedAt;model.selected=model.nodes[0]?.id||null;$('#lastSync').textContent=timeLabel(state.savedAt);$('#saveStatus').textContent='LOCAL STORAGE READY'}
 catch(e){model.nodes=[];model.logs=[];toast(`Could not load local data: ${e.message}`)}
 renderAll();bindCanvas();bindEvents();startAnimation();$('#graphTitle').textContent='Map of the mind. Every thought, connected.';$('#commandInput').focus();
}
boot();
</script>
</body></html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "NexusMemory/1.0"

    def log_message(self, fmt: str, *args) -> None:
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))

    def send_bytes(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, code: int, value: object) -> None:
        self.send_bytes(code, json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self.send_bytes(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/state":
            self.send_json(200, load_state())
        elif path == "/health":
            self.send_json(200, {"ok": True, "app": "NEXUS Memory Map"})
        else:
            self.send_json(404, {"error": "Not found"})

    def do_PUT(self) -> None:
        if urlparse(self.path).path != "/api/state":
            self.send_json(404, {"error": "Not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 2 or length > MAX_BODY:
                self.send_json(413, {"error": "The map is empty or exceeds the 2 MB save limit."})
                return
            raw = json.loads(self.rfile.read(length).decode("utf-8"))
            saved = save_state(raw)
            self.send_json(200, saved)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            self.send_json(400, {"error": str(exc)[:240]})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            self.send_json(500, {"error": "Could not save map: " + str(exc)[:180]})

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/reset":
            self.send_json(404, {"error": "Not found"})
            return
        self.send_json(200, save_state(seed_state()))


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the NEXUS local-first memory graph website.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: localhost)")
    parser.add_argument("--port", type=int, default=8765, help="Port (default: 8765)")
    parser.add_argument("--no-browser", action="store_true", help="Do not open a browser automatically")
    args = parser.parse_args()
    server = Server((args.host, args.port), DashboardHandler)
    url = f"http://{args.host if args.host != '0.0.0.0' else '127.0.0.1'}:{args.port}/"
    print(f"NEXUS Memory Map is running at {url}")
    print(f"Local data file: {STATE_FILE}")
    if not args.no_browser:
        threading.Timer(.7, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nClosing NEXUS Memory Map…")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
