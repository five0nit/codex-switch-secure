#!/usr/bin/env python3
"""Local-only web UI for Mike's codex-switch-secure fork.

Binds to 127.0.0.1 only. It never displays OAuth tokens. Auth onboarding uses
OpenAI's device-code flow through the reviewed local codex-switch-secure binary.
Includes editable display names and a Melbourne-business-hours force refresher.
"""
from __future__ import annotations

import base64
import html
import json
import os
import re
import secrets
import shlex
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from zoneinfo import ZoneInfo

HOST = "127.0.0.1"
PORT = int(os.environ.get("CODEX_SWITCH_WEB_PORT", "8787"))
BIN = os.environ.get("CODEX_SWITCH_BIN", str(Path.home() / ".local/bin/codex-switch-secure"))
CONFIG_PATH = Path(os.environ.get("CODEX_SWITCH_WEB_CONFIG", str(Path.home() / ".codex-switch/local-web-config.json")))
PROFILE_ROOT = Path(os.environ.get("CODEX_SWITCH_PROFILE_ROOT", str(Path.home() / ".codex-switch/profiles")))
SHARE_DIR = Path(os.environ.get("CODEX_SWITCH_SHARE_DIR", str(Path.home() / ".codex-switch/share")))
MELBOURNE_TZ = ZoneInfo("Australia/Melbourne")
REFRESH_INTERVAL_SECONDS = 30 * 60
DEVICE_URL_FALLBACK = "https://auth.openai.com/codex/device"
CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
DEFAULT_ACCOUNTS = [
    {"alias": "pro-1", "label": "Pro account 1", "expected_plan": "pro"},
    {"alias": "pro-2", "label": "Pro account 2", "expected_plan": "pro"},
    {"alias": "business-seat", "label": "Business seat", "expected_plan": "business"},
]

sessions: dict[str, "LoginSession"] = {}
sessions_lock = threading.Lock()
config_lock = threading.Lock()
refresh_lock = threading.Lock()
refresh_state: dict = {
    "enabled": True,
    "timezone": "Australia/Melbourne",
    "window": "Mon-Fri 07:00-18:00",
    "interval_seconds": REFRESH_INTERVAL_SECONDS,
    "last_run_at": None,
    "last_success_at": None,
    "last_error": None,
    "last_profile_count": None,
    "next_run_at": None,
    "running": False,
}


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


def _default_config() -> dict:
    return {"accounts": DEFAULT_ACCOUNTS}


def load_config() -> dict:
    with config_lock:
        if not CONFIG_PATH.exists():
            cfg = _default_config()
            save_config_unlocked(cfg)
            return cfg
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
        except Exception:
            cfg = _default_config()
        accounts = cfg.get("accounts") if isinstance(cfg, dict) else None
        if not isinstance(accounts, list):
            cfg = _default_config()
        by_alias = {a.get("alias"): a for a in cfg.get("accounts", []) if isinstance(a, dict)}
        changed = False
        for acct in DEFAULT_ACCOUNTS:
            if acct["alias"] not in by_alias:
                cfg.setdefault("accounts", []).append(acct.copy())
                changed = True
        if changed:
            save_config_unlocked(cfg)
        return cfg


def save_config_unlocked(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(CONFIG_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(CONFIG_PATH)


def save_config(cfg: dict) -> None:
    with config_lock:
        save_config_unlocked(cfg)


def get_expected_accounts() -> list[dict]:
    cfg = load_config()
    accounts = []
    for acct in cfg.get("accounts", []):
        if not isinstance(acct, dict):
            continue
        alias = str(acct.get("alias", ""))
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", alias):
            continue
        accounts.append({
            "alias": alias,
            "label": str(acct.get("label") or alias)[:120],
            "expected_plan": str(acct.get("expected_plan") or "")[:80],
        })
    return accounts or DEFAULT_ACCOUNTS.copy()


def set_account_label(alias: str, label: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", alias):
        raise ValueError("invalid alias")
    label = label.strip()[:120]
    if not label:
        raise ValueError("display name cannot be empty")
    with config_lock:
        cfg = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else _default_config()
        accounts = cfg.setdefault("accounts", [])
        for acct in accounts:
            if acct.get("alias") == alias:
                acct["label"] = label
                save_config_unlocked(cfg)
                return acct
        acct = {"alias": alias, "label": label, "expected_plan": ""}
        accounts.append(acct)
        save_config_unlocked(cfg)
        return acct


def reorder_accounts(aliases: list[str]) -> dict:
    clean_aliases = []
    seen = set()
    for alias in aliases:
        alias = str(alias or "")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", alias):
            continue
        if alias in seen:
            continue
        seen.add(alias)
        clean_aliases.append(alias)
    if not clean_aliases:
        raise ValueError("no valid aliases supplied")

    with config_lock:
        cfg = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else _default_config()
        accounts = [a for a in cfg.get("accounts", []) if isinstance(a, dict)]
        by_alias = {str(a.get("alias")): a for a in accounts if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", str(a.get("alias", "")))}
        ordered = []
        for alias in clean_aliases:
            ordered.append(by_alias.pop(alias, {"alias": alias, "label": alias, "expected_plan": ""}))
        ordered.extend(by_alias.values())
        cfg["accounts"] = ordered
        save_config_unlocked(cfg)
    return {"ok": True, "accounts": get_expected_accounts()}


def build_share_auth(alias: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", alias):
        raise ValueError("invalid alias")
    auth_path = PROFILE_ROOT / alias / "auth.json"
    if not auth_path.exists():
        raise FileNotFoundError(f"saved auth profile not found for {alias}")
    raw = auth_path.read_bytes()
    # Validate shape but never return parsed token values.
    parsed = json.loads(raw.decode())
    if not isinstance(parsed, dict):
        raise ValueError("auth file is not a JSON object")

    SHARE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(SHARE_DIR, 0o700)
    bundle_path = SHARE_DIR / f"{alias}-{secrets.token_urlsafe(8)}.auth.json"
    bundle_path.write_bytes(raw)
    os.chmod(bundle_path, 0o600)

    b64 = base64.b64encode(raw).decode()
    q_alias = shlex.quote(alias)
    q_bundle = shlex.quote(str(bundle_path))
    q_bin = shlex.quote(BIN)
    payload_cmd = (
        "umask 077; mkdir -p ~/.codex; "
        "base64 -d > ~/.codex/auth.json <<'AUTH_PAYLOAD'\n"
        f"{b64}\n"
        "AUTH_PAYLOAD\n"
        "chmod 600 ~/.codex/auth.json"
    )
    return {
        "ok": True,
        "alias": alias,
        "bundle_path": str(bundle_path),
        "warning": "Sensitive OAuth auth payload. Only paste/share with your own trusted agent machine. Rotate/re-auth if exposed.",
        "same_machine_switch_command": f"{q_bin} use {q_alias}",
        "same_machine_install_command": f"umask 077; mkdir -p ~/.codex; install -m 600 {q_bundle} ~/.codex/auth.json",
        "target_machine_payload_install_command": payload_cmd,
        "target_machine_import_command_note": f"If you copy {bundle_path.name} to another machine, import it there with: {q_bin} import /path/to/{shlex.quote(bundle_path.name)} {q_alias}",
    }


def parse_profiles_from_output(stdout: str) -> list[dict]:
    parsed = json_from_mixed_stdout(stdout)
    if isinstance(parsed, dict) and isinstance(parsed.get("profiles"), list):
        return parsed["profiles"]
    return []


def remove_account(alias: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", alias):
        raise ValueError("invalid alias")

    removed_config = False
    with config_lock:
        cfg = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else _default_config()
        before = len(cfg.get("accounts", []))
        cfg["accounts"] = [a for a in cfg.get("accounts", []) if not (isinstance(a, dict) and a.get("alias") == alias)]
        removed_config = len(cfg.get("accounts", [])) != before
        save_config_unlocked(cfg)

    deleted_profile = False
    profile_error = None
    listed = run_cmd([BIN, "--json", "list"], timeout=45)
    profiles = parse_profiles_from_output(listed.stdout)
    aliases = [p.get("alias") for p in profiles]
    if alias in aliases:
        current = next((p.get("alias") for p in profiles if p.get("alias") == alias and p.get("is_current")), None)
        if current:
            replacement = next((a for a in aliases if a and a != alias), None)
            if replacement:
                run_cmd([BIN, "--json", "use", replacement], timeout=30)
        proc = run_cmd([BIN, "--json", "delete", alias], timeout=45)
        if proc.returncode == 0:
            deleted_profile = True
        else:
            profile_error = (proc.stderr or proc.stdout or f"delete failed with code {proc.returncode}")[-1000:]

    return {
        "ok": profile_error is None,
        "alias": alias,
        "removed_config": removed_config,
        "deleted_profile": deleted_profile,
        "profile_error": profile_error,
        "accounts": get_expected_accounts(),
    }


def get_profiles(force: bool = False) -> dict:
    try:
        args = [BIN, "--json", "list"]
        if force:
            args.append("--force")
        proc = run_cmd(args, timeout=75 if force else 45)
        parsed = json_from_mixed_stdout(proc.stdout)
        if parsed is None:
            return {"ok": False, "error": (proc.stderr or proc.stdout or "No JSON output")[-2000:]}
        parsed["accounts_config"] = get_expected_accounts()
        parsed["refresh_state"] = get_refresh_state()
        return {"ok": True, **parsed}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "accounts_config": get_expected_accounts(), "refresh_state": get_refresh_state()}


def parse_filter_ts(value: str | None, default: datetime) -> int:
    if not value:
        dt = default
    else:
        cleaned = value.strip()
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=MELBOURNE_TZ)
    return int(dt.timestamp())


def get_local_token_usage(start_s: str | None, end_s: str | None) -> dict:
    now = datetime.now(MELBOURNE_TZ)
    start = parse_filter_ts(start_s, now - timedelta(days=1))
    end = parse_filter_ts(end_s, now)
    if end < start:
        start, end = end, start

    rows = []
    total = 0
    db_files = sorted(CODEX_HOME.glob("state_*.sqlite"))
    for db_path in db_files:
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
            con.row_factory = sqlite3.Row
            exists = con.execute("select 1 from sqlite_master where type='table' and name='threads'").fetchone()
            if not exists:
                con.close()
                continue
            for row in con.execute(
                """
                select id, title, created_at, updated_at, tokens_used, model
                from threads
                where coalesce(updated_at, created_at, 0) between ? and ?
                  and coalesce(tokens_used, 0) > 0
                order by coalesce(updated_at, created_at, 0) desc
                limit 500
                """,
                (start, end),
            ):
                tokens = int(row["tokens_used"] or 0)
                total += tokens
                updated = int(row["updated_at"] or row["created_at"] or 0)
                rows.append({
                    "thread_id": row["id"],
                    "title": row["title"] or "Untitled",
                    "model": row["model"],
                    "tokens_used": tokens,
                    "updated_at": updated,
                    "updated_at_iso": datetime.fromtimestamp(updated, MELBOURNE_TZ).isoformat() if updated else None,
                    "source_db": db_path.name,
                })
            con.close()
        except Exception as exc:
            rows.append({"source_db": db_path.name, "error": str(exc), "tokens_used": 0})

    return {
        "ok": True,
        "source": "local_codex_state_sqlite",
        "scope": "local machine / all Codex CLI threads; OpenAI does not expose official per-seat token totals through the current Codex usage endpoint",
        "per_account_attribution": False,
        "start": start,
        "end": end,
        "start_iso": datetime.fromtimestamp(start, MELBOURNE_TZ).isoformat(),
        "end_iso": datetime.fromtimestamp(end, MELBOURNE_TZ).isoformat(),
        "total_tokens": total,
        "thread_count": len([r for r in rows if not r.get("error")]),
        "rows": rows[:500],
    }


def is_business_slot(dt: datetime) -> bool:
    # Monday=0, Friday=4. Run on half-hour slots 07:00 through 18:00 inclusive.
    return dt.weekday() < 5 and ((dt.hour > 7 or (dt.hour == 7 and dt.minute >= 0)) and (dt.hour < 18 or (dt.hour == 18 and dt.minute == 0)))


def ceil_to_half_hour(dt: datetime) -> datetime:
    base = dt.replace(second=0, microsecond=0)
    if dt.second or dt.microsecond:
        base += timedelta(minutes=1)
    minute = 0 if base.minute <= 0 else 30 if base.minute <= 30 else 60
    if minute == 60:
        return (base + timedelta(hours=1)).replace(minute=0)
    return base.replace(minute=minute)


def next_refresh_slot(now: datetime | None = None) -> datetime:
    now = now or datetime.now(MELBOURNE_TZ)
    candidate = ceil_to_half_hour(now)
    if candidate <= now:
        candidate += timedelta(minutes=30)
    for _ in range(16 * 24 * 2):
        if is_business_slot(candidate):
            return candidate
        # Jump efficiently outside the business window.
        if candidate.weekday() >= 5 or candidate.hour > 18 or (candidate.hour == 18 and candidate.minute > 0):
            candidate = (candidate + timedelta(days=1)).replace(hour=7, minute=0, second=0, microsecond=0)
        elif candidate.hour < 7:
            candidate = candidate.replace(hour=7, minute=0, second=0, microsecond=0)
        else:
            candidate += timedelta(minutes=30)
    return candidate


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def get_refresh_state() -> dict:
    with refresh_lock:
        state = dict(refresh_state)
    return state


def force_refresh(reason: str = "manual") -> dict:
    with refresh_lock:
        if refresh_state.get("running"):
            return {"ok": False, "error": "refresh already running", **dict(refresh_state)}
        refresh_state["running"] = True
        refresh_state["last_run_at"] = iso(datetime.now(MELBOURNE_TZ))
        refresh_state["last_error"] = None
    try:
        data = get_profiles(force=True)
        if not data.get("ok"):
            raise RuntimeError(data.get("error") or "refresh failed")
        with refresh_lock:
            refresh_state["running"] = False
            refresh_state["last_success_at"] = iso(datetime.now(MELBOURNE_TZ))
            refresh_state["last_profile_count"] = len(data.get("profiles", []))
            refresh_state["last_reason"] = reason
        return {"ok": True, "profile_count": len(data.get("profiles", [])), "reason": reason, "refresh_state": get_refresh_state()}
    except Exception as exc:
        with refresh_lock:
            refresh_state["running"] = False
            refresh_state["last_error"] = str(exc)
        return {"ok": False, "error": str(exc), "refresh_state": get_refresh_state()}


def scheduler_loop() -> None:
    while True:
        nxt = next_refresh_slot()
        with refresh_lock:
            refresh_state["next_run_at"] = iso(nxt)
        while True:
            diff = (nxt - datetime.now(MELBOURNE_TZ)).total_seconds()
            if diff <= 0:
                break
            time.sleep(min(60, max(1, diff)))
        force_refresh("scheduled")


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
    if not sess:
        return False
    sess.cancel()
    with sess._lock:
        sess.done = True
        sess.returncode = -1
        sess.error = "cancelled"
    return True


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
    main { padding:18px; max-width:none; width:100%; margin:0; }
    .grid { display:flex; flex-wrap:nowrap; gap:12px; overflow-x:auto; overflow-y:hidden; padding:4px 4px 18px; scroll-snap-type:x proximity; }
    .grid .card { flex:0 0 265px; min-width:265px; scroll-snap-align:start; }
    .grid .card.dragging { opacity:.45; outline:2px dashed var(--blue); }
    .drag-handle { cursor:grab; user-select:none; color:var(--muted); font-size:12px; margin-bottom:8px; display:inline-flex; gap:5px; align-items:center; }
    .drag-handle:active { cursor:grabbing; }
    .card { background:rgba(17,25,35,.94); border:1px solid var(--line); border-radius:16px; padding:14px; box-shadow:0 8px 30px rgba(0,0,0,.25); }
    .row { display:flex; justify-content:space-between; gap:8px; align-items:center; }
    .alias { font-size:16px; font-weight:800; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:180px; }
    .pill { padding:3px 7px; border-radius:999px; background:#1c2a38; color:var(--muted); font-size:11px; white-space:nowrap; }
    .ok { color:var(--green); } .warn { color:var(--yellow); } .bad { color:var(--red); }
    .bar { height:9px; background:#0b1118; border-radius:999px; overflow:hidden; border:1px solid #243447; margin:6px 0 2px; }
    .fill { height:100%; width:0%; background:var(--green); transition:width .25s; }
    .fill.warn { background:var(--yellow); } .fill.bad { background:var(--red); }
    button, a.btn { display:inline-flex; align-items:center; justify-content:center; gap:6px; border:1px solid #2f4761; background:#16263a; color:var(--text); padding:8px 10px; border-radius:10px; cursor:pointer; text-decoration:none; font-weight:700; font-size:13px; }
    button:hover, a.btn:hover { background:#1d3450; }
    .btn.primary { background:#14539a; border-color:#2e7ccc; }
    .muted { color:var(--muted); font-size:12px; }
    .code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; background:#05080c; border:1px solid var(--line); border-radius:10px; padding:9px; overflow:auto; }
    input { width:100%; background:#05080c; color:var(--text); border:1px solid #2d4158; border-radius:9px; padding:8px; margin-top:6px; font-size:13px; }
    pre { white-space:pre-wrap; max-height:220px; overflow:auto; }
    .actions { display:flex; flex-wrap:wrap; gap:6px; margin-top:8px; }
    .top-actions { display:flex; flex-wrap:wrap; gap:10px; margin-top:12px; }
    .token-table { width:100%; border-collapse:collapse; margin-top:10px; font-size:12px; }
    .token-table th, .token-table td { border-bottom:1px solid var(--line); padding:7px 6px; text-align:left; }
    .token-table th { color:var(--muted); font-weight:700; }
    .summary-number { font-size:24px; font-weight:900; color:var(--green); }
    .modal { position:fixed; inset:0; background:rgba(0,0,0,.72); display:none; align-items:center; justify-content:center; z-index:10; padding:18px; }
    .modal.open { display:flex; }
    .modal-card { width:min(980px, 96vw); max-height:90vh; overflow:auto; background:#111923; border:1px solid var(--line); border-radius:18px; padding:18px; box-shadow:0 20px 80px rgba(0,0,0,.55); }
    textarea { width:100%; min-height:170px; background:#05080c; color:var(--text); border:1px solid #2d4158; border-radius:10px; padding:10px; font:12px ui-monospace,monospace; }
    .danger-note { color:#ffd166; border:1px solid #5d4a16; background:#241b08; border-radius:12px; padding:10px; margin:10px 0; }
  </style>
</head>
<body>
<header>
  <h1>Codex Account Usage</h1>
  <div class="sub">Local-only dashboard. Auth uses device-code flow; OAuth tokens are never shown here.</div>
  <div class="top-actions">
    <button onclick="refreshAll()">Refresh usage</button>
    <button onclick="forceRefresh()">Force refresh now</button>
    <a class="btn" href="/api/accounts" target="_blank">Raw JSON</a>
  </div>
</header>
<main>
  <section class="card" style="margin-bottom:18px">
    <div class="alias">Auto refresher</div>
    <div class="muted" id="scheduler">Loading…</div>
  </section>
  <section class="card" style="margin-bottom:18px">
    <div class="alias">Add account</div>
    <p class="muted">Create a new dashboard slot and immediately start device-code auth. Alias is internal; display name is what you see on cards.</p>
    <div class="row" style="align-items:flex-end; flex-wrap:wrap">
      <label style="flex:1; min-width:220px"><span class="muted">Display name</span><input id="newLabel" placeholder="e.g. Spare Pro account" maxlength="120" /></label>
      <label style="flex:1; min-width:180px"><span class="muted">Alias</span><input id="newAlias" placeholder="e.g. spare-pro" maxlength="64" /></label>
      <button class="btn primary" onclick="addAccount()">Add account</button>
    </div>
    <div class="muted" id="addAccountStatus"></div>
  </section>
  <section class="grid" id="cards" aria-label="Account cards. Drag cards left or right to reorder."></section>
  <section class="card" style="margin-top:18px">
    <div class="alias">Token usage explorer</div>
    <p class="muted">OpenAI's Codex usage endpoint shows allowance % and reset times, not official token totals. This section reads local Codex thread token counters when available, with date/time filters.</p>
    <div class="row" style="align-items:flex-end; flex-wrap:wrap">
      <label style="flex:1; min-width:220px"><span class="muted">Start</span><input id="tokenStart" type="datetime-local" /></label>
      <label style="flex:1; min-width:220px"><span class="muted">End</span><input id="tokenEnd" type="datetime-local" /></label>
      <button onclick="loadTokenUsage()">Load token usage</button>
    </div>
    <div id="tokenSummary" style="margin-top:10px"></div>
    <div id="tokenRows"></div>
  </section>
  <section class="card" style="margin-top:18px">
    <div class="alias">Auth setup links</div>
    <p class="muted">Open each local link below, then click the OpenAI device page and enter the displayed code. Use different browser profiles/incognito sessions if you need to keep accounts separate.</p>
    <div class="actions" id="authLinks"></div>
  </section>
</main>
<div class="modal" id="shareModal">
  <div class="modal-card">
    <div class="row"><div class="alias" id="shareTitle">Share auth</div><button onclick="closeShareModal()">Close</button></div>
    <div class="danger-note">Sensitive OAuth auth payload. Only paste/share with your own trusted agent machine. Anyone with this payload can use that Codex auth until revoked/re-authenticated.</div>
    <p class="muted">Fast local repoint on this same machine:</p>
    <textarea id="shareLocal" readonly></textarea>
    <div class="actions"><button onclick="copyText('shareLocal')">Copy local command</button></div>
    <p class="muted">Copy/paste installer for another trusted machine. This contains the auth payload; do not paste into chats/logs.</p>
    <textarea id="sharePayload" readonly></textarea>
    <div class="actions"><button onclick="copyText('sharePayload')">Copy payload installer</button></div>
    <p class="muted" id="shareBundle"></p>
  </div>
</div>
<script>
let expected = [];
function escapeHtml(s){ return String(s||'').replace(/[<>&"]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c])); }
function pct(n){ return Math.max(0, Math.min(100, Number(n||0))); }
function fmtDate(ts){
  if(!ts) return 'unknown';
  return new Date(Number(ts)*1000).toLocaleString(undefined, {weekday:'short', month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'});
}
function fmtFullDate(ts){
  if(!ts) return 'unknown';
  return new Date(Number(ts)*1000).toLocaleString();
}
function resetLine(label, win){
  if(!win || !win.resets_at) return `<div class="muted">${label} reset: unknown</div>`;
  const hrs = Math.max(0, (win.resets_in_seconds||0)/3600);
  const rel = hrs < 1 ? `${Math.round(hrs*60)}m` : `${hrs.toFixed(1)}h`;
  return `<div class="muted">${label} reset: <b>${fmtDate(win.resets_at)}</b> · in ${rel}</div>`;
}
function bar(label, win){
  if(!win) return `<div class="muted">${label}: no data</div>`;
  const p=pct(win.used_percent), cls=p>=90?'bad':p>=70?'warn':'ok';
  return `<div><div class="row"><span>${label}</span><span class="${cls}">${p.toFixed(1)}% used · ${(100-p).toFixed(1)}% left</span></div><div class="bar"><div class="fill ${cls}" style="width:${p}%"></div></div>${resetLine(label, win)}</div>`;
}
async function api(path, opts){ const r=await fetch(path, opts); return await r.json(); }
function renderScheduler(state){
  state = state || {};
  const bits = [`Every 30 min`, `Mon-Fri`, `07:00-18:00`, `Australia/Melbourne`];
  if(state.running) bits.push('running now');
  if(state.next_run_at) bits.push(`next: ${new Date(state.next_run_at).toLocaleString()}`);
  if(state.last_success_at) bits.push(`last success: ${new Date(state.last_success_at).toLocaleString()}`);
  if(state.last_error) bits.push(`last error: ${state.last_error}`);
  document.getElementById('scheduler').innerText = bits.join(' · ');
}
async function saveName(alias){
  const label = document.getElementById(`name-${alias}`).value;
  const res = await api('/api/accounts/name', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({alias,label})});
  if(!res.ok) alert(res.error || 'Save failed');
  await refreshAll();
}
function slugifyAlias(label){
  return String(label||'').toLowerCase().replace(/[^a-z0-9._-]+/g,'-').replace(/^-+|-+$/g,'').slice(0,64);
}
async function addAccount(){
  const labelEl = document.getElementById('newLabel');
  const aliasEl = document.getElementById('newAlias');
  const statusEl = document.getElementById('addAccountStatus');
  const label = labelEl.value.trim();
  let alias = aliasEl.value.trim() || slugifyAlias(label);
  if(!label){ alert('Enter a display name first.'); return; }
  if(!alias){ alert('Enter an alias.'); return; }
  if(!/^[A-Za-z0-9._-]{1,64}$/.test(alias)){ alert('Alias can only use letters, numbers, dot, underscore, and dash.'); return; }
  statusEl.innerText = `Adding ${label}…`;
  const res = await api('/api/accounts/name', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({alias,label})});
  if(!res.ok){ alert(res.error || 'Add failed'); statusEl.innerText = ''; return; }
  labelEl.value = ''; aliasEl.value = '';
  statusEl.innerText = `Added ${label}. Opening auth page…`;
  await refreshAll();
  window.location.href = `/auth/${encodeURIComponent(alias)}`;
}
async function removeAccount(alias, label){
  if(!confirm(`Remove ${label || alias}? This removes the dashboard slot and deletes the saved local profile if it exists.`)) return;
  const res = await api('/api/accounts/remove', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({alias})});
  if(!res.ok) alert(res.profile_error || res.error || 'Remove failed');
  await refreshAll();
}
function closeShareModal(){ document.getElementById('shareModal').classList.remove('open'); }
async function copyText(id){
  const el = document.getElementById(id);
  el.focus(); el.select();
  await navigator.clipboard.writeText(el.value);
}
async function shareAuth(alias, label){
  const res = await api('/api/accounts/share-auth', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({alias})});
  if(!res.ok){ alert(res.error || 'Share auth failed'); return; }
  document.getElementById('shareTitle').innerText = `Share auth: ${label || alias}`;
  document.getElementById('shareLocal').value = `${res.same_machine_switch_command}\n# If the target local agent only reads ~/.codex/auth.json, use:\n${res.same_machine_install_command}`;
  document.getElementById('sharePayload').value = res.target_machine_payload_install_command;
  document.getElementById('shareBundle').innerText = `Local bundle written: ${res.bundle_path}\n${res.target_machine_import_command_note}`;
  document.getElementById('shareModal').classList.add('open');
}
async function saveCardOrder(){
  const aliases = [...document.querySelectorAll('#cards .card[data-alias]')].map(el=>el.dataset.alias).filter(Boolean);
  if(!aliases.length) return;
  const res = await api('/api/accounts/reorder', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({aliases})});
  if(!res.ok) alert(res.error || 'Reorder failed');
}
function setupDragReorder(){
  const grid = document.getElementById('cards');
  if(!grid) return;
  let dragged = null;
  grid.querySelectorAll('.card[data-alias]').forEach(card=>{
    card.draggable = true;
    card.addEventListener('dragstart', ev=>{
      if(ev.target.closest('input,button,a')) { ev.preventDefault(); return; }
      dragged = card;
      card.classList.add('dragging');
      ev.dataTransfer.effectAllowed = 'move';
      ev.dataTransfer.setData('text/plain', card.dataset.alias || '');
    });
    card.addEventListener('dragend', async ()=>{
      card.classList.remove('dragging');
      dragged = null;
      await saveCardOrder();
    });
  });
  grid.ondragover = ev=>{
    ev.preventDefault();
    const active = dragged || grid.querySelector('.dragging');
    if(!active) return;
    const cards = [...grid.querySelectorAll('.card[data-alias]:not(.dragging)')];
    const next = cards.find(c => ev.clientX < c.getBoundingClientRect().left + c.offsetWidth / 2);
    grid.insertBefore(active, next || null);
  };
}
async function forceRefresh(){
  document.getElementById('scheduler').innerText = 'Force refresh running…';
  const res = await api('/api/refresh', {method:'POST'});
  if(!res.ok) alert(res.error || 'Refresh failed');
  await refreshAll();
}
function localDatetime(dt){
  const pad=n=>String(n).padStart(2,'0');
  return `${dt.getFullYear()}-${pad(dt.getMonth()+1)}-${pad(dt.getDate())}T${pad(dt.getHours())}:${pad(dt.getMinutes())}`;
}
function initTokenFilters(){
  const end = new Date();
  const start = new Date(end.getTime() - 24*3600*1000);
  document.getElementById('tokenStart').value = localDatetime(start);
  document.getElementById('tokenEnd').value = localDatetime(end);
}
async function loadTokenUsage(){
  const start = encodeURIComponent(document.getElementById('tokenStart').value || '');
  const end = encodeURIComponent(document.getElementById('tokenEnd').value || '');
  const data = await api(`/api/local-token-usage?start=${start}&end=${end}`);
  if(!data.ok){ alert(data.error || 'Token usage load failed'); return; }
  document.getElementById('tokenSummary').innerHTML = `<div><span class="summary-number">${Number(data.total_tokens||0).toLocaleString()}</span> tokens · ${Number(data.thread_count||0).toLocaleString()} threads</div><div class="muted">${escapeHtml(data.start_iso)} → ${escapeHtml(data.end_iso)}</div><div class="muted">${escapeHtml(data.scope)}</div>`;
  const rows = data.rows || [];
  if(!rows.length){ document.getElementById('tokenRows').innerHTML = '<div class="muted">No local Codex token rows found for this filter.</div>'; return; }
  document.getElementById('tokenRows').innerHTML = `<table class="token-table"><thead><tr><th>Time</th><th>Tokens</th><th>Model</th><th>Thread</th></tr></thead><tbody>${rows.slice(0,50).map(r=>r.error ? `<tr><td>${escapeHtml(r.source_db)}</td><td colspan="3" class="bad">${escapeHtml(r.error)}</td></tr>` : `<tr><td>${escapeHtml(r.updated_at_iso||'')}</td><td>${Number(r.tokens_used||0).toLocaleString()}</td><td>${escapeHtml(r.model||'')}</td><td>${escapeHtml(r.title||r.thread_id||'')}</td></tr>`).join('')}</tbody></table>`;
}
async function refreshAll(){
  const data = await api('/api/accounts');
  expected = data.accounts_config || expected || [];
  renderScheduler(data.refresh_state);
  const profiles = data.profiles || [];
  const byAlias = Object.fromEntries(profiles.map(p=>[p.alias,p]));
  const merged = [...expected.map(e=>({expected:e, profile:byAlias[e.alias]})), ...profiles.filter(p=>!expected.find(e=>e.alias===p.alias)).map(p=>({expected:{alias:p.alias,label:p.alias,expected_plan:''}, profile:p}))];
  document.getElementById('cards').innerHTML = merged.map(({expected:e, profile:p})=>{
    const usage=p?.usage||{}; const acct=p?.account||{}; const status=p?'Connected':'Not connected';
    return `<article class="card" data-alias="${escapeHtml(e.alias)}" title="Drag left/right to reorder"><div class="drag-handle">↔ drag to reorder</div><div class="row"><div><div class="alias">${escapeHtml(e.label)}</div><div class="muted">alias: ${escapeHtml(e.alias)}</div></div><span class="pill ${p?'ok':'warn'}">${status}</span></div>
      <label class="muted">Display name</label><input id="name-${escapeHtml(e.alias)}" value="${escapeHtml(e.label)}" maxlength="120" />
      <div class="actions"><button onclick="saveName('${escapeHtml(e.alias)}')">Save name</button></div>
      <p class="muted">Plan: ${escapeHtml(acct.plan||e.expected_plan||'unknown')} ${p?.is_current?'· current':''}</p>
      ${bar('5h', usage.primary)}${bar('7d / weekly', usage.secondary)}
      <div class="actions"><a class="btn primary" href="/auth/${encodeURIComponent(e.alias)}">Auth</a><button onclick='shareAuth(${JSON.stringify(e.alias)}, ${JSON.stringify(e.label)})'>Share Auth</button><button onclick="refreshAll()">Refresh</button><button onclick='removeAccount(${JSON.stringify(e.alias)}, ${JSON.stringify(e.label)})'>Remove</button></div></article>`;
  }).join('');
  setupDragReorder();
  renderAuthLinks();
}
function renderAuthLinks(){
  document.getElementById('authLinks').innerHTML = expected.map(e=>`<a class="btn primary" href="/auth/${encodeURIComponent(e.alias)}">Authorize ${escapeHtml(e.label)}</a>`).join('');
}
initTokenFilters(); refreshAll(); loadTokenUsage(); setInterval(refreshAll, 15000);
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
 if(s.done && s.returncode!==0){ html+=`<p><button onclick="restartAuth()">Start a fresh auth code</button></p><p class="muted">Codes are single-use. If the old code failed, start a fresh one and use it within ~15 minutes.</p>`; }
 if(s.error){ html+=`<h2 style="color:#ff5d5d">Error</h2><p>${s.error}</p>`; }
 html+=`<h3>Log</h3><pre>${(s.lines||[]).map(x=>x.replace(/[<>&]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]))).join('\n')}</pre>`;
 document.getElementById('content').innerHTML=html;
 if(!s.done) setTimeout(poll,2000);
}
async function restartAuth(){
 await fetch(`/api/login/cancel?alias=${encodeURIComponent(alias)}`, {method:'POST'});
 await fetch(`/api/login/start?alias=${encodeURIComponent(alias)}`, {method:'POST'});
 poll();
}
fetch(`/api/login/start?alias=${encodeURIComponent(alias)}`, {method:'POST'}).then(poll);
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "CodexSwitchLocalWeb/0.2"

    def log_message(self, format, *args):
        return

    def _send(self, status: int, body: bytes, content_type: str):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode())

    def send_json(self, obj, status=200):
        self._send(status, json.dumps(obj, indent=2).encode(), "application/json; charset=utf-8")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            return self._send(200, INDEX_HTML.encode(), "text/html; charset=utf-8")
        if path == "/api/accounts":
            return self.send_json(get_profiles())
        if path == "/api/config":
            return self.send_json({"ok": True, "accounts": get_expected_accounts(), "refresh_state": get_refresh_state()})
        if path == "/api/local-token-usage":
            qs = parse_qs(parsed.query)
            try:
                return self.send_json(get_local_token_usage(qs.get("start", [None])[0], qs.get("end", [None])[0]))
            except Exception as exc:
                return self.send_json({"ok": False, "error": str(exc)}, 400)
        if path == "/api/login/status":
            alias = parse_qs(parsed.query).get("alias", [""])[0]
            sess = get_session(alias)
            return self.send_json(sess.status() if sess else {"alias": alias, "ready": False, "done": False, "lines": []})
        if path.startswith("/auth/"):
            alias = unquote(path.rsplit("/", 1)[-1])
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
        if parsed.path == "/api/accounts/name":
            try:
                body = self.read_json_body()
                acct = set_account_label(str(body.get("alias") or ""), str(body.get("label") or ""))
                return self.send_json({"ok": True, "account": acct, "accounts": get_expected_accounts()})
            except Exception as exc:
                return self.send_json({"ok": False, "error": str(exc)}, 400)
        if parsed.path == "/api/accounts/reorder":
            try:
                body = self.read_json_body()
                result = reorder_accounts(body.get("aliases") or [])
                return self.send_json(result)
            except Exception as exc:
                return self.send_json({"ok": False, "error": str(exc)}, 400)
        if parsed.path == "/api/accounts/share-auth":
            try:
                body = self.read_json_body()
                result = build_share_auth(str(body.get("alias") or ""))
                return self.send_json(result)
            except Exception as exc:
                return self.send_json({"ok": False, "error": str(exc)}, 400)
        if parsed.path == "/api/accounts/remove":
            try:
                body = self.read_json_body()
                result = remove_account(str(body.get("alias") or ""))
                return self.send_json(result, 200 if result.get("ok") else 400)
            except Exception as exc:
                return self.send_json({"ok": False, "error": str(exc)}, 400)
        if parsed.path == "/api/refresh":
            return self.send_json(force_refresh("manual"))
        return self.send_json({"error": "not found"}, 404)


def main():
    if not Path(BIN).exists():
        raise SystemExit(f"codex-switch binary not found: {BIN}")
    load_config()
    threading.Thread(target=scheduler_loop, name="melbourne-refresh-scheduler", daemon=True).start()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Codex Switch local web UI: http://{HOST}:{PORT}", flush=True)
    print("Auth links:", flush=True)
    for acct in get_expected_accounts():
        print(f"  {acct['label']}: http://{HOST}:{PORT}/auth/{acct['alias']}", flush=True)
    print("Auto refresh: every 30 min, Mon-Fri 07:00-18:00 Australia/Melbourne", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
