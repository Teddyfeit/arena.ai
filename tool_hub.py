#!/usr/bin/env python3
"""Discord Name Ideas — offline username ideas with manual checks and optional webhook."""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORT = 8765
STATE_FILE = ROOT / "username_ideas.json"  # Local-only: also contains the private webhook URL.
STATE_LOCK = threading.RLock()
MAX_BODY = 64 * 1024
ALLOWED_WEBHOOK_HOSTS = {"discord.com", "discordapp.com"}
CONSONANTS = "bcdfghjklmnpqrstvwxyz"
VOWELS = "aeiou"


def default_state():
    return {"ideas": [], "webhook_url": ""}


def load_state():
    with STATE_LOCK:
        try:
            value = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                return default_state()
            ideas = value.get("ideas", [])
            if not isinstance(ideas, list):
                ideas = []
            clean = []
            for item in ideas[-200:]:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", "")).lower()
                if not valid_name(name):
                    continue
                status = item.get("status") if item.get("status") in {"new", "taken", "available"} else "new"
                clean.append({"id": str(item.get("id", secrets.token_hex(5)))[:32],
                              "name": name, "status": status, "notified": bool(item.get("notified", False)),
                              "createdAt": str(item.get("createdAt", ""))[:40]})
            webhook = value.get("webhook_url", "")
            if not isinstance(webhook, str) or not valid_webhook(webhook):
                webhook = ""
            return {"ideas": clean, "webhook_url": webhook}
        except (OSError, ValueError, TypeError):
            return default_state()


def save_state(state):
    with STATE_LOCK:
        temp = STATE_FILE.with_suffix(".tmp")
        temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(temp, 0o600)
        except OSError:
            pass
        temp.replace(STATE_FILE)


def valid_name(name):
    return bool(re.fullmatch(r"[a-z0-9._]{2,32}", str(name)) and
                not name.startswith(".") and not name.endswith(".") and ".." not in name)


def valid_webhook(value):
    try:
        parsed = urllib.parse.urlparse(value)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_WEBHOOK_HOSTS:
            return False
        if parsed.username or parsed.password or parsed.port not in (None, 443):
            return False
        return bool(re.fullmatch(r"/api/webhooks/[0-9]+/[A-Za-z0-9._-]+/?", parsed.path))
    except (ValueError, TypeError):
        return False


def generate_name(length, numbers=False):
    """Create a pronounceable, exact-length idea locally; this never checks Discord."""
    chars = []
    want_vowel = bool(secrets.randbelow(2))
    for _ in range(length):
        pool = VOWELS if want_vowel else CONSONANTS
        chars.append(secrets.choice(pool))
        want_vowel = not want_vowel
    if numbers and length >= 3 and secrets.randbelow(2):
        digit_count = 1 if length < 8 or secrets.randbelow(2) else 2
        start = max(1, length - digit_count)
        chars[start:] = [secrets.choice("0123456789") for _ in range(length - start)]
    return "".join(chars)


def send_webhook(url, name):
    if not valid_webhook(url):
        raise ValueError("Webhook-Adresse fehlt oder ist keine gültige Discord-Webhook-URL.")
    payload = json.dumps({"content": f"Manuell als verfügbar markierte Discord-Namensidee: `{name}`"}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST", headers={
        "Content-Type": "application/json", "User-Agent": "NameIdeas/1.0"
    })
    # Do not follow redirects: the private webhook token must not be forwarded elsewhere.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(req, timeout=8) as response:
            if response.status not in (200, 204):
                raise ValueError(f"Discord hat die Nachricht abgelehnt (HTTP {response.status}).")
    except urllib.error.HTTPError as exc:
        raise ValueError(f"Discord-Webhook meldete HTTP {exc.code}.") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ValueError("Webhook konnte nicht erreicht werden. Prüfe die Adresse und Verbindung.") from None


HTML = r'''<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#f5f8ff"><title>Discord Name Ideas</title>
<style>
:root{color-scheme:light;--ink:#1c2940;--muted:#5b6980;--blue:#5368e8;--blue2:#3f54d6;--line:#dfe6f1;--paper:#fff;--wash:#f5f8ff;--green:#14804a;--red:#bb3c4a;--shadow:0 18px 48px rgba(53,72,116,.09)*{box-sizing:border-box}body{margin:0;background:linear-gradient(145deg,#eef4ff,#f8faff 56%,#edf7f5);color:var(--ink);font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;min-height:100vh}button,input,select{font:inherit}button{cursor:pointer}.shell{width:min(1080px,calc(100% - 36px));margin:0 auto;padding:20px 0 52px}.top{display:flex;align-items:center;gap:12px;padding:3px 0 18px;border-bottom:1px solid var(--line)}.mark{width:42px;height:42px;display:grid;place-items:center;border-radius:14px;background:linear-gradient(135deg,#6679f4,#8898ff);color:white;font-weight:800;font-size:20px;box-shadow:0 7px 17px #6477ea38}.brand{font-weight:780;font-size:17px;letter-spacing:-.02em}.brand small{display:block;color:var(--muted);font-weight:500;font-size:12px;letter-spacing:.04em}.saved{margin-left:auto;color:var(--green);font-size:13px}.hero{padding:30px 0 18px}.eyebrow{color:#5264bd;font-size:12px;font-weight:750;letter-spacing:.14em;text-transform:uppercase}.hero h1{margin:5px 0 8px;font-size:clamp(30px,5vw,44px);line-height:1.12;letter-spacing:-.045em}.hero p{max-width:760px;margin:0;color:var(--muted);font-size:17px}.steps{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:20px 0}.step,.card,.result,.notice{background:var(--paper);border:1px solid var(--line);border-radius:16px;box-shadow:0 5px 20px rgba(53,72,116,.045)}.step{padding:14px 16px}.step b{display:flex;align-items:center;gap:8px;margin-bottom:3px;font-size:14px}.step i{width:25px;height:25px;display:grid;place-items:center;border-radius:50%;background:#edf0ff;color:#4b5ed3;font-style:normal}.step span{color:var(--muted);font-size:13px}.card{padding:20px;margin:15px 0}.card h2{margin:0 0 5px;font-size:21px;letter-spacing:-.02em}.sub{margin:0 0 16px;color:var(--muted);font-size:14px}.controls{display:grid;grid-template-columns:1fr 1fr 1.25fr auto;align-items:end;gap:13px}.field label{display:block;margin-bottom:6px;font-weight:680;font-size:13px}.field small{display:block;margin-top:5px;color:var(--muted);font-size:12px}.field input,.field select{width:100%;height:46px;padding:0 12px;border:1px solid #cbd6e6;border-radius:10px;background:white;color:var(--ink)}.field input:focus,.field select:focus{outline:3px solid #5368e822;border-color:#6478ef}.btn{min-height:44px;padding:0 16px;border:1px solid #cbd6e6;border-radius:10px;background:white;color:var(--ink);font-weight:680;transition:.15s}.btn:hover{transform:translateY(-1px);box-shadow:0 5px 15px #3c52821c}.btn.primary{background:var(--blue);border-color:var(--blue);color:white}.btn.primary:hover{background:var(--blue2)}.btn.good{background:#ebf8f0;color:#116b3e;border-color:#b9e2c8}.btn.bad{background:#fff3f3;color:#a3313b;border-color:#f0c7cb}.results-head{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:12px}.results-head h2{margin:0;font-size:20px}.counts{color:var(--muted);font-size:13px}.list{display:grid;gap:9px}.result{display:flex;align-items:center;gap:12px;padding:12px 14px;box-shadow:none}.name{font:700 19px ui-monospace,SFMono-Regular,Consolas,monospace;letter-spacing:.035em;overflow-wrap:anywhere}.name-wrap{min-width:0;flex:1}.badge{display:inline-flex;margin-top:4px;padding:2px 9px;border-radius:99px;background:#eef2f8;color:#54637a;font-size:11px;font-weight:700}.badge.available{background:#e7f7ed;color:#167344}.badge.taken{background:#fff0f0;color:#a93641}.actions{display:flex;flex-wrap:wrap;gap:7px}.actions .btn{min-height:36px;padding:0 11px;font-size:13px}.empty{padding:32px 15px;text-align:center;color:var(--muted)}.empty b{display:block;margin-bottom:4px;color:var(--ink);font-size:17px}.notice{padding:14px 16px;color:#45536a;font-size:13px;line-height:1.55}.notice strong{color:#263650}.settings{display:grid;grid-template-columns:1fr auto;align-items:center;gap:12px}.settings p{margin:0;color:var(--muted);font-size:13px}.webhook{display:flex;gap:8px;min-width:min(490px,100%)}.webhook input{min-width:0;flex:1;height:42px;padding:0 11px;border:1px solid #cbd6e6;border-radius:9px;background:white}.webhook-state{grid-column:1/-1;color:var(--muted);font-size:12px}.toast{position:fixed;right:18px;bottom:18px;z-index:4;max-width:min(420px,calc(100vw - 36px));padding:13px 16px;background:#1d2940;color:white;border-radius:12px;box-shadow:0 10px 35px #1d294044}.toast.error{background:#9f3040}.footer{padding-top:14px;color:#758197;font-size:12px;text-align:center}.hidden{display:none!important}
@media(max-width:760px){.steps{grid-template-columns:1fr}.controls{grid-template-columns:1fr 1fr}.controls .field:nth-child(3){grid-column:1/-1}.controls>.btn{grid-column:1/-1}.settings{grid-template-columns:1fr}.webhook{min-width:0}.result{align-items:flex-start;flex-direction:column}.actions{width:100%}.actions .btn{flex:1}.saved{font-size:11px}}
@media(prefers-reduced-motion:reduce){*,*:before,*:after{scroll-behavior:auto!important;transition:none!important}}
</style></head><body><main class="shell">
<header class="top"><div class="mark">@</div><div class="brand">NAME IDEAS<small>DISCORD · OFFLINE-GENERATOR</small></div><span class="saved" id="saveState" aria-live="polite">Lokal gespeichert</span></header>
<section class="hero"><div class="eyebrow">EINFACH STARTEN</div><h1>Finde deine nächste Namensidee.</h1><p>Wähle eine Länge und erzeuge Vorschläge. FIELDKIT prüft Discord-Namen nicht automatisch. Wenn du selbst geprüft hast, kannst du einen verfügbaren Namen an deinen Discord-Webhook senden.</p></section>
<section class="steps" aria-label="So funktioniert es"><article class="step"><b><i>1</i>Ideen erzeugen</b><span>Lege fest, wie viele Zeichen dein Name haben soll.</span></article><article class="step"><b><i>2</i>Selbst in Discord prüfen</b><span>Öffne Discord und kontrolliere dort, ob der Name frei ist.</span></article><article class="step"><b><i>3</i>Als verfügbar melden</b><span>Bestätige es hier. Erst dann sendet die App den Namen an deinen Webhook.</span></article></section>
<section class="card"><h2>Welche Namen soll ich vorschlagen?</h2><p class="sub">Die Vorschläge werden nur auf deinem Gerät erzeugt. Es gibt keine automatische Discord-Abfrage.</p><div class="controls"><div class="field"><label for="length">Länge in Zeichen</label><input id="length" type="number" min="2" max="32" value="6"><small>Discord erlaubt 2 bis 32 Zeichen.</small></div><div class="field"><label for="count">Anzahl Vorschläge</label><input id="count" type="number" min="1" max="30" value="12"><small>Pro Runde höchstens 30 Ideen.</small></div><div class="field"><label for="style">Zeichen</label><select id="style"><option value="letters">Nur Kleinbuchstaben</option><option value="mixed">Buchstaben und Zahlen</option></select><small>Jeder Vorschlag hält die gewählte Länge ein.</small></div><button class="btn primary" id="generate">Neue Ideen erzeugen</button></div></section>
<section class="card"><div class="results-head"><h2>Deine Vorschläge</h2><span class="counts" id="countLabel">0 Ideen gespeichert</span></div><div class="list" id="list"><div class="empty"><b>Noch keine Ideen</b>Wähle oben eine Länge und klicke auf „Neue Ideen erzeugen“.</div></div></section>
<section class="notice"><strong>Nur manuelle Verfügbarkeitsprüfung:</strong> Discord stellt dieser App keinen offiziellen öffentlichen Prüfdienst zur Verfügung. Der Generator erfragt keine Discord-Daten, loggt sich nicht ein und beansprucht keine Namen. Der Webhook wird nur gesendet, wenn du nach deiner eigenen Prüfung ausdrücklich „Ich habe geprüft – verfügbar“ auswählst.</section>
<section class="card"><h2>Discord-Webhook verbinden</h2><p class="sub">Optional. Die Webhook-Adresse bleibt lokal in einer Datei auf diesem Computer gespeichert. Gib hier nur deinen Discord-Webhook ein – niemals dein Passwort oder einen Bot-Token.</p><div class="settings"><p id="hookHelp">Ohne Webhook kannst du Ideen erzeugen und manuell verwalten.</p><div class="webhook"><input id="webhook" type="password" autocomplete="new-password" placeholder="https://discord.com/api/webhooks/…" aria-label="Discord-Webhook-Adresse"><button class="btn primary" id="saveWebhook">Speichern</button><button class="btn" id="clearWebhook">Entfernen</button></div><span class="webhook-state" id="hookState" aria-live="polite">Webhook wird nicht im Browser gespeichert.</span></div></section>
<div class="footer">FIELDKIT · Vorschläge sind zufällig und ihre Verfügbarkeit ist nicht garantiert.</div>
</main><div id="toast" class="toast hidden" role="status" aria-live="polite"></div>
<script>
'use strict';
const $=s=>document.querySelector(s);let ideas=[];let webhookConfigured=false;let toastTimer;
const escapeHTML=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(url,options={}){const res=await fetch(url,{headers:{'Content-Type':'application/json',...(options.headers||{})},...options});let data={};try{data=await res.json()}catch{}if(!res.ok)throw new Error(data.error||`Fehler (${res.status})`);return data}
function toast(message,error=false){const t=$('#toast');t.textContent=message;t.classList.toggle('error',error);t.classList.remove('hidden');clearTimeout(toastTimer);toastTimer=setTimeout(()=>t.classList.add('hidden'),4200)}
function render(){const list=$('#list');$('#countLabel').textContent=`${ideas.length} ${ideas.length===1?'Idee':'Ideen'} gespeichert`;if(!ideas.length){list.innerHTML='<div class="empty"><b>Noch keine Ideen</b>Wähle oben eine Länge und klicke auf „Neue Ideen erzeugen“.</div>';return}list.innerHTML=ideas.map(x=>{const badge=x.status==='available'?(x.notified?'Verfügbar · an Webhook gesendet':'Verfügbar · noch nicht gesendet'):x.status==='taken'?'Manuell als vergeben markiert':'Noch nicht geprüft';const action=x.status==='available'?'An Webhook senden':'Ich habe geprüft – verfügbar';return `<article class="result"><div class="name-wrap"><div class="name">${escapeHTML(x.name)}</div><span class="badge ${x.status}">${badge}</span></div><div class="actions"><button class="btn" data-copy="${escapeHTML(x.id)}">Kopieren</button><a class="btn" href="https://discord.com/settings" target="_blank" rel="noopener">Discord öffnen</a><button class="btn good" data-available="${escapeHTML(x.id)}" ${x.notified?'disabled':''}>${action}</button><button class="btn bad" data-taken="${escapeHTML(x.id)}" ${x.status==='taken'?'disabled':''}>Vergeben</button><button class="btn" data-remove="${escapeHTML(x.id)}" aria-label="Idee ${escapeHTML(x.name)} löschen">Löschen</button></div></article>`}).join('')}
async function refresh(){const state=await api('/api/state');ideas=state.ideas||[];webhookConfigured=!!state.webhookConfigured;$('#hookHelp').textContent=webhookConfigured?'Webhook gespeichert. Die Adresse selbst wird nicht erneut angezeigt.':'Ohne Webhook kannst du Ideen erzeugen und manuell verwalten.';$('#hookState').textContent=webhookConfigured?'Webhook verbunden (lokal gespeichert).':'Webhook wird nicht im Browser gespeichert.';render()}
$('#generate').addEventListener('click',async()=>{const length=Number($('#length').value),count=Number($('#count').value);try{const d=await api('/api/generate',{method:'POST',body:JSON.stringify({length,count,style:$('#style').value})});ideas=d.ideas;render();toast(`${d.added} neue Namensideen mit genau ${length} Zeichen erzeugt.`);document.querySelector('.results-head').scrollIntoView({behavior:'smooth',block:'start'})}catch(e){toast(e.message,true)}});
$('#saveWebhook').addEventListener('click',async()=>{const url=$('#webhook').value.trim();if(!url){toast('Füge zuerst deine Discord-Webhook-Adresse ein.',true);return}try{const d=await api('/api/webhook',{method:'PUT',body:JSON.stringify({url})});webhookConfigured=d.configured;$('#webhook').value='';$('#hookState').textContent='Webhook verbunden (lokal gespeichert).';$('#hookHelp').textContent='Webhook gespeichert. Die Adresse selbst wird nicht erneut angezeigt.';toast('Webhook sicher lokal gespeichert.')}catch(e){toast(e.message,true)}});
$('#clearWebhook').addEventListener('click',async()=>{try{await api('/api/webhook',{method:'DELETE'});webhookConfigured=false;$('#hookState').textContent='Webhook entfernt.';$('#hookHelp').textContent='Ohne Webhook kannst du Ideen erzeugen und manuell verwalten.';toast('Webhook-Adresse entfernt.')}catch(e){toast(e.message,true)}});
document.addEventListener('click',async e=>{const copy=e.target.closest('[data-copy]');if(copy){const item=ideas.find(x=>x.id===copy.dataset.copy);if(!item)return;try{await navigator.clipboard.writeText(item.name);toast(`„${item.name}“ kopiert.`)}catch{toast('Kopieren wurde vom Browser blockiert.',true)}return}const remove=e.target.closest('[data-remove]');if(remove){if(!confirm('Diese Namensidee wirklich löschen?'))return;try{const d=await api('/api/idea/'+encodeURIComponent(remove.dataset.remove),{method:'DELETE'});ideas=d.ideas;render();toast('Idee gelöscht.')}catch(err){toast(err.message,true)}return}const taken=e.target.closest('[data-taken]');if(taken){try{const d=await api('/api/status',{method:'POST',body:JSON.stringify({id:taken.dataset.taken,status:'taken'})});ideas=d.ideas;render();toast('Als vergeben markiert – keine Webhook-Nachricht gesendet.')}catch(err){toast(err.message,true)}return}const available=e.target.closest('[data-available]');if(available){const item=ideas.find(x=>x.id===available.dataset.available);if(!item)return;if(!confirm(`Hast du „${item.name}“ selbst in Discord geprüft und als verfügbar bestätigt? Erst danach wird der Name an deinen Webhook gesendet.`))return;try{const d=await api('/api/status',{method:'POST',body:JSON.stringify({id:item.id,status:'available'})});ideas=d.ideas;render();toast(d.sent?'Manuell bestätigter Name wurde an deinen Webhook gesendet.':'Als verfügbar notiert. Richte einen Webhook ein, wenn du ihn senden möchtest.')}catch(err){toast(err.message,true)}}});
refresh().catch(e=>toast(`Lokale Daten konnten nicht geladen werden: ${e.message}`,true));
</script></body></html>'''


class AppHandler(BaseHTTPRequestHandler):
    server_version = "NameIdeas/1.0"

    def log_message(self, fmt, *args):
        # Avoid logging webhook URLs or request bodies.
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))

    def send_json(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def send_html(self):
        body = HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'self'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Ungültige Anfragegröße.")
        if size < 2 or size > MAX_BODY:
            raise ValueError("Die Anfrage ist leer oder zu groß.")
        try:
            return json.loads(self.rfile.read(size).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("Die Anfrage enthält kein gültiges JSON.")

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self.send_html()
        if path == "/health":
            return self.send_json({"ok": True, "app": "Discord Name Ideas"})
        if path == "/api/state":
            state = load_state()
            return self.send_json({"ideas": state["ideas"], "webhookConfigured": bool(state["webhook_url"])})
        return self.send_json({"error": "Seite nicht gefunden."}, 404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            data = self.read_json()
            if not isinstance(data, dict):
                raise ValueError("Ungültige Anfrage.")
            if path == "/api/generate":
                length = data.get("length")
                count = data.get("count")
                if isinstance(length, bool) or not isinstance(length, int) or not 2 <= length <= 32:
                    raise ValueError("Wähle eine Länge zwischen 2 und 32 Zeichen.")
                if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 30:
                    raise ValueError("Wähle zwischen 1 und 30 Vorschlägen.")
                if data.get("style") not in ("letters", "mixed"):
                    raise ValueError("Bitte wähle eine gültige Zeichenart.")
                with STATE_LOCK:
                    state = load_state()
                    existing = {x["name"] for x in state["ideas"]}
                    batch = []
                    attempts = 0
                    while len(batch) < count and attempts < count * 30:
                        attempts += 1
                        name = generate_name(length, data["style"] == "mixed")
                        if name in existing:
                            continue
                        existing.add(name)
                        batch.append({"id": secrets.token_hex(6), "name": name, "status": "new", "notified": False})
                    state["ideas"] = (batch + state["ideas"])[0:200]
                    save_state(state)
                return self.send_json({"ideas": state["ideas"], "added": len(batch)})
            if path == "/api/status":
                idea_id, status = data.get("id"), data.get("status")
                if not isinstance(idea_id, str) or status not in ("available", "taken"):
                    raise ValueError("Ungültiger Status oder Name.")
                with STATE_LOCK:
                    state = load_state()
                    idea = next((x for x in state["ideas"] if x["id"] == idea_id), None)
                    if not idea:
                        return self.send_json({"error": "Namensidee nicht gefunden."}, 404)
                    idea["status"] = status
                    sent = False
                    if status == "available" and not idea.get("notified") and state["webhook_url"]:
                        send_webhook(state["webhook_url"], idea["name"])
                        idea["notified"] = True
                        sent = True
                    save_state(state)
                return self.send_json({"ideas": state["ideas"], "sent": sent})
            return self.send_json({"error": "Aktion nicht gefunden."}, 404)
        except ValueError as exc:
            return self.send_json({"error": str(exc)}, 400)
        except OSError:
            return self.send_json({"error": "Die lokalen Daten konnten nicht gespeichert werden."}, 500)

    def do_PUT(self):
        if urllib.parse.urlparse(self.path).path != "/api/webhook":
            return self.send_json({"error": "Aktion nicht gefunden."}, 404)
        try:
            data = self.read_json()
            url = data.get("url", "") if isinstance(data, dict) else ""
            if not isinstance(url, str) or not valid_webhook(url):
                raise ValueError("Bitte eine gültige Discord-Webhook-URL (https://discord.com/api/webhooks/…) eingeben.")
            with STATE_LOCK:
                state = load_state()
                state["webhook_url"] = url
                save_state(state)
            return self.send_json({"configured": True})
        except ValueError as exc:
            return self.send_json({"error": str(exc)}, 400)
        except OSError:
            return self.send_json({"error": "Die lokale Einstellung konnte nicht gespeichert werden."}, 500)

    def do_DELETE(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/webhook":
            with STATE_LOCK:
                state = load_state()
                state["webhook_url"] = ""
                save_state(state)
            return self.send_json({"configured": False})
        if path.startswith("/api/idea/"):
            idea_id = urllib.parse.unquote(path.removeprefix("/api/idea/"))
            with STATE_LOCK:
                state = load_state()
                state["ideas"] = [x for x in state["ideas"] if x["id"] != idea_id]
                save_state(state)
            return self.send_json({"ideas": state["ideas"]})
        return self.send_json({"error": "Aktion nicht gefunden."}, 404)


def main():
    parser = argparse.ArgumentParser(description="Offline Discord username idea generator with manual review.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: localhost)")
    parser.add_argument("--port", type=int, default=PORT, help="Port (default: 8765)")
    parser.add_argument("--no-browser", action="store_true", help="Do not open a browser automatically")
    args = parser.parse_args()
    try:
        server = ThreadingHTTPServer((args.host, args.port), AppHandler)
    except OSError as exc:
        print(f"Port {args.port} konnte nicht geöffnet werden: {exc}")
        return
    server.daemon_threads = True
    display_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{display_host}:{args.port}/"
    print(f"Discord Name Ideas läuft unter {url}")
    print("Ideen und Webhook werden nur lokal in username_ideas.json gespeichert.")
    if not args.no_browser:
        import threading as _threading
        import webbrowser
        _threading.Timer(0.6, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
