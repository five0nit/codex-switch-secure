#!/usr/bin/env python3
"""Local-only web UI for Mike's codex-switch-secure fork.

Binds to 127.0.0.1 only. It never displays OAuth tokens. Auth onboarding uses
OpenAI's device-code flow through the reviewed local codex-switch-secure binary.
"""
from __future__ import annotations

import html
import json
import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HOST = "127.0.0.1"
PORT = int(os.environ.get("CODEX_SWITCH_WEB_PORT", "8787"))
BIN = os.environ.get("CODEX_SWITCH_BIN", str(Path.home() / ".local/bin/codex-switch-secure"))
DEFAULT_ACCOUNTS = [
    {"alias": "pro-1", "label": "Pro account 1", "expected_plan": "pro"},
    {"alias": "pro-2", "label": "Pro account 2", "expected_plan": "pro"},
    {"alias": "business-seat", "label": "Business seat", "expected_plan": "business"},
]
DEVICE_URL_FALLBACK = "https://auth.openai.com/codex/device"

sessions: dict[str, "LoginSession"] = {}
sessions_lock = threading.Lock()


def run_cmd(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("CS_ALLOW_INSECURE_ENDPOINT_OVERRIDES", None)
    return subprocess.run(args, text=True, capture_output=True, timeout=timeout, env=env)


def json_from_mixed_stdout(stdout: str):
    for i, ch in enumerate(stdout):
        if ch in "[{":
            try:
                return json.loads(stdout[i:])
            except json.JSONDecodeError:
                continue
    return None


def get_profiles() -> dict:
    try:
        proc = run_cmd([BIN, "--json", "list"], timeout=45)
        parsed = json_from_mixed_stdout(proc.stdout)
        if parsed is None:
            return {"ok": False, "error": (proc.stderr or proc.stdout or "No JSON output")[-2000:]}
        return {"ok": True, **parsed}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


class LoginSession:
    def __init__(self, alias: str):
        self.alias = alias
        self.started_at = time.time()
        self.proc: subprocess.Popen[str] | None = None
        self.lines: list[str] = []
        self.verification_uri: str | None = None
        self.user_code: str | None = None
        self.done = False
        self.returncode: int | None = None
        self.error: str | None = None
        self._lock = threading.Lock()
        self._start()

    def _start(self):
        env = os.environ.copy()
        env.pop("CS_ALLOW_INSECURE_ENDPOINT_OVERRIDES", None)
        self.proc = subprocess.Popen(
            [BIN, "login", "--device", self.alias],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        threading.Thread(target=self._reader, name=f"login-reader-{self.alias}", daemon=True).start()

    def _reader(self):
        assert self.proc and self.proc.stdout
        try:
            for raw in self.proc.stdout:
                line = raw.rstrip("\n")
                with self._lock:
                    self.lines.append(line)
                    self.lines = self.lines[-250:]
                    if "To sign in, visit:" in line:
                        self.verification_uri = line.split("To sign in, visit:", 1)[1].strip()
                    if "Enter code:" in line:
                        self.user_code = line.split("Enter code:", 1)[1].strip()
                    # Defensive generic URL/code parsing if output text changes slightly.
                    if not self.verification_uri:
                        m = re.search(r"https://auth\.openai\.com/\S+", line)
                        if m:
                            self.verification_uri = m.group(0)
                    if not self.user_code:
                        m = re.search(r"\b[A-Z0-9]{4,}(?:-[A-Z0-9]{4,})+\b", line)
                        if m:
                            self.user_code = m.group(0)
            rc = self.proc.wait(timeout=5)
            with self._lock:
                self.done = True
                self.returncode = rc
                if rc != 0:
                    self.error = f"login exited with code {rc}"
        except Exception as exc:
            with self._lock:
                self.done = True
                self.error = str(exc)

    def status(self) -> dict:
        with self._lock:
            return {
                "alias": self.alias,
                "started_at": self.started_at,
                "age_seconds": int(time.time() - self.started_at),
                "verification_uri": self.verification_uri or DEVICE_URL_FALLBACK,
                "user_code": self.user_code,
                "ready": bool(self.user_code),
                "done": self.done,
                "returncode": self.returncode,
                "error": self.error,
                "lines": self.lines[-80:],
            }

    def cancel(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def start_login(alias: str) -> LoginSession:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", alias):
        raise ValueError("invalid alias")
    with sessions_lock:
        existing = sessions.get(alias)
        if existing and not existing.done:
            return existing
        sess = LoginSession(alias)
        sessions[alias] = sess
        return sess


def get_session(alias: str) -> LoginSession | None:
    with sessions_lock:
        return sessions.get(alias)


def cancel_session(alias: str) -> bool:
    with sessions_lock:
        sess = sessions.pop(alias, None)
    if sess:
        sess.cancel()
        return True
    return False


INDEX_HTML = r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Codex Account Usage</title>
  <style>
    :root { color-scheme: dark; --bg:#0b0f14; --card:#111923; --muted:#8da2b5; --text:#e9f0f6; --line:#223142; --green:#41d17d; --yellow:#ffd166; --red:#ff5d5d; --blue:#62a8ff; }
    * { box-sizing:border-box; }
    body { margin:0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif; background:linear-gradient(135deg,#07111f,#101018); color:var(--text); }
    header { padding:28px 28px 16px; border-bottom:1px solid var(--line); background:rgba(0,0,0,.24); position:sticky; top:0; backdrop-filter: blur(10px); z-index:2; }
    h1 { margin:0 0 8px; font-size:28px; }
    .sub { color:var(--muted); }
    main { padding:22px; max-width:1180px; margin:0 auto; }
    .grid { display:grid; grid-template-columns: repeat(auto-fit,minmax(290px,1fr)); gap:16px; }
    .card { background:rgba(17,25,35,.94); border:1px solid var(--line); border-radius:18px; padding:18px; box-shadow:0 8px 30px rgba(0,0,0,.25); }
    .row { display:flex; justify-content:space-between; gap:12px; align-items:center; }
    .alias { font-size:20px; font-weight:800; }
    .pill { padding:4px 9px; border-radius:999px; background:#1c2a38; color:var(--muted); font-size:12px; white-space:nowrap; }
    .ok { color:var(--green); } .warn { color:var(--yellow); } .bad { color:var(--red); }
    .bar { height:12px; background:#0b1118; border-radius:999px; overflow:hidden; border:1px solid #243447; margin:8px 0 3px; }
    .fill { height:100%; width:0%; background:var(--green); transition:width .25s; }
    .fill.warn { background:var(--yellow); } .fill.bad { background:var(--red); }
    button, a.btn { display:inline-flex; align-items:center; justify-content:center; gap:8px; border:1px solid #2f4761; background:#16263a; color:var(--text); padding:10px 12px; border-radius:12px; cursor:pointer; text-decoration:none; font-weight:700; }
    button:hover, a.btn:hover { background:#1d3450; }
    .btn.primary { background:#14539a; border-color:#2e7ccc; }
    .muted { color:var(--muted); font-size:13px; }
    .code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; background:#05080c; border:1px solid var(--line); border-radius:10px; padding:9px; overflow:auto; }
    pre { white-space:pre-wrap; max-height:220px; overflow:auto; }
    .actions { display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; }
    .top-actions { display:flex; flex-wrap:wrap; gap:10px; margin-top:12px; }
  </style>
</head>
<body>
<header>
  <h1>Codex Account Usage</h1>
  <div class="sub">Local-only dashboard. Auth uses device-code flow; OAuth tokens are never shown here.</div>
  <div class="top-actions">
    <button onclick="refreshAll()">Refresh usage</button>
    <a class="btn" href="/api/accounts" target="_blank">Raw JSON</a>
  </div>
</header>
<main>
  <section class="grid" id="cards"></section>
  <section class="card" style="margin-top:18px">
    <div class="alias">Auth setup links</div>
    <p class="muted">Open each local link below, then click the OpenAI device page and enter the displayed code. Use different browser profiles/incognito sessions if you need to keep accounts separate.</p>
    <div class="actions" id="authLinks"></div>
  </section>
</main>
<script>
const expected = __ACCOUNTS__;
function pct(n){ return Math.max(0, Math.min(100, Number(n||0))); }
function bar(label, win){
  if(!win) return `<div class="muted">${label}: no data</div>`;
  const p=pct(win.used_percent), cls=p>=90?'bad':p>=70?'warn':'ok';
  return `<div><div class="row"><span>${label}</span><span class="${cls}">${p.toFixed(1)}% used · ${(100-p).toFixed(1)}% left</span></div><div class="bar"><div class="fill ${cls}" style="width:${p}%"></div></div><div class="muted">resets in ${Math.max(0, Math.round((win.resets_in_seconds||0)/3600))}h</div></div>`;
}
async function api(path, opts){ const r=await fetch(path, opts); return await r.json(); }
async function refreshAll(){
  const data = await api('/api/accounts');
  const profiles = data.profiles || [];
  const byAlias = Object.fromEntries(profiles.map(p=>[p.alias,p]));
  const merged = [...expected.map(e=>({expected:e, profile:byAlias[e.alias]})), ...profiles.filter(p=>!expected.find(e=>e.alias===p.alias)).map(p=>({expected:{alias:p.alias,label:p.alias,expected_plan:''}, profile:p}))];
  document.getElementById('cards').innerHTML = merged.map(({expected:e, profile:p})=>{
    const usage=p?.usage||{}; const acct=p?.account||{}; const status=p?'Connected':'Not connected';
    return `<article class="card"><div class="row"><div><div class="alias">${e.label}</div><div class="muted">alias: ${e.alias}</div></div><span class="pill ${p?'ok':'warn'}">${status}</span></div>
      <p class="muted">Plan: ${acct.plan||e.expected_plan||'unknown'} ${p?.is_current?'· current':''}</p>
      ${bar('5h', usage.primary)}${bar('7d / weekly', usage.secondary)}
      <div class="actions"><a class="btn primary" href="/auth/${encodeURIComponent(e.alias)}">Add / re-auth</a><button onclick="refreshOne('${e.alias}')">Refresh</button></div></article>`;
  }).join('');
}
async function refreshOne(alias){ await refreshAll(); }
function renderAuthLinks(){
  document.getElementById('authLinks').innerHTML = expected.map(e=>`<a class="btn primary" href="/auth/${encodeURIComponent(e.alias)}">Authorize ${e.label}</a>`).join('');
}
renderAuthLinks(); refreshAll(); setInterval(refreshAll, 15000);
</script>
</body>
</html>
"""

AUTH_HTML = r"""
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Authorize __ALIAS__</title>
<style>body{font-family:system-ui;background:#0b0f14;color:#e9f0f6;margin:0;padding:28px}a,button{background:#14539a;color:white;border:1px solid #2e7ccc;border-radius:12px;padding:12px 14px;text-decoration:none;font-weight:800;display:inline-block;margin:6px 6px 6px 0}.card{background:#111923;border:1px solid #223142;border-radius:18px;padding:20px;max-width:820px}.code{font:34px ui-monospace,monospace;letter-spacing:2px;background:#05080c;border:1px solid #223142;border-radius:12px;padding:16px;display:inline-block}.muted{color:#8da2b5}pre{background:#05080c;border:1px solid #223142;border-radius:12px;padding:12px;white-space:pre-wrap;max-height:300px;overflow:auto}</style></head>
<body><div class="card"><h1>Authorize __ALIAS__</h1><p class="muted">Waiting for OpenAI device code. Keep this page open until complete.</p><div id="content">Starting…</div><p><a href="/">Back to dashboard</a></p></div>
<script>
const alias="__ALIAS__";
async function poll(){
 const r=await fetch(`/api/login/status?alias=${encodeURIComponent(alias)}`); const s=await r.json();
 let html='';
 if(s.ready){ html+=`<p>1. Open OpenAI device page:</p><p><a target="_blank" href="${s.verification_uri}">Open auth page</a></p><p>2. Enter this code:</p><div class="code">${s.user_code}</div>`; }
 else { html+=`<p>Requesting code…</p>`; }
 if(s.done && s.returncode===0){ html+=`<h2 style="color:#41d17d">Done — account saved.</h2><p><a href="/">Return to dashboard</a></p>`; }
 if(s.error){ html+=`<h2 style="color:#ff5d5d">Error</h2><p>${s.error}</p>`; }
 html+=`<h3>Log</h3><pre>${(s.lines||[]).map(x=>x.replace(/[<>&]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]))).join('\n')}</pre>`;
 document.getElementById('content').innerHTML=html;
 if(!s.done) setTimeout(poll,2000);
}
fetch(`/api/login/start?alias=${encodeURIComponent(alias)}`, {method:'POST'}).then(poll);
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "CodexSwitchLocalWeb/0.1"

    def log_message(self, format, *args):
        # Avoid noisy request logs; no tokens should be present, but keep logs quiet.
        return

    def _send(self, status: int, body: bytes, content_type: str):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, obj, status=200):
        self._send(status, json.dumps(obj, indent=2).encode(), "application/json; charset=utf-8")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            body = INDEX_HTML.replace("__ACCOUNTS__", json.dumps(DEFAULT_ACCOUNTS)).encode()
            return self._send(200, body, "text/html; charset=utf-8")
        if path == "/api/accounts":
            return self.send_json(get_profiles())
        if path == "/api/login/status":
            alias = parse_qs(parsed.query).get("alias", [""])[0]
            sess = get_session(alias)
            return self.send_json(sess.status() if sess else {"alias": alias, "ready": False, "done": False, "lines": []})
        if path.startswith("/auth/"):
            alias = path.rsplit("/", 1)[-1]
            alias = re.sub(r"[^A-Za-z0-9._-]", "", alias)[:64]
            body = AUTH_HTML.replace("__ALIAS__", html.escape(alias)).encode()
            return self._send(200, body, "text/html; charset=utf-8")
        return self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/login/start":
            alias = parse_qs(parsed.query).get("alias", [""])[0]
            try:
                sess = start_login(alias)
                return self.send_json(sess.status())
            except Exception as exc:
                return self.send_json({"error": str(exc)}, 400)
        if parsed.path == "/api/login/cancel":
            alias = parse_qs(parsed.query).get("alias", [""])[0]
            return self.send_json({"cancelled": cancel_session(alias)})
        return self.send_json({"error": "not found"}, 404)


def main():
    if not Path(BIN).exists():
        raise SystemExit(f"codex-switch binary not found: {BIN}")
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Codex Switch local web UI: http://{HOST}:{PORT}", flush=True)
    print(f"Auth links:", flush=True)
    for acct in DEFAULT_ACCOUNTS:
        print(f"  {acct['label']}: http://{HOST}:{PORT}/auth/{acct['alias']}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
