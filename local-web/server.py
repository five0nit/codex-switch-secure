#!/usr/bin/env python3
"""Local-only web UI for codex-switch.

Binds to 127.0.0.1 only. It never displays OAuth tokens. Auth onboarding uses
OpenAI's device-code flow through the configured local codex-switch binary.
Includes editable display names and a Melbourne-business-hours force refresher.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import html
import io
import json
import os
import re
import secrets
import shlex
import shutil
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

HOST = "127.0.0.1"
PORT = int(os.environ.get("CODEX_SWITCH_WEB_PORT", "8787"))
BIN = os.environ.get("CODEX_SWITCH_BIN", "codex-switch")
CONFIG_PATH = Path(os.environ.get("CODEX_SWITCH_WEB_CONFIG", str(Path.home() / ".codex-switch/local-web-config.json")))
PROFILE_ROOT = Path(os.environ.get("CODEX_SWITCH_PROFILE_ROOT", str(Path.home() / ".codex-switch/profiles")))
SHARE_DIR = Path(os.environ.get("CODEX_SWITCH_SHARE_DIR", str(Path.home() / ".codex-switch/share")))
MACHINE_USAGE_CACHE = Path(os.environ.get("CODEX_SWITCH_MACHINE_USAGE_CACHE", str(Path.home() / ".codex-switch/machine-usage-cache.json")))
ANTHROPIC_ACCOUNTS_PATH = Path(os.environ.get("CODEX_SWITCH_ANTHROPIC_ACCOUNTS", str(Path.home() / ".codex-switch/anthropic-accounts.json")))
GROK_ACCOUNTS_PATH = Path(os.environ.get("CODEX_SWITCH_GROK_ACCOUNTS", str(Path.home() / ".codex-switch/grok-accounts.json")))
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
    return {"accounts": DEFAULT_ACCOUNTS, "machine_usage_sheet_csv_url": "", "machine_overrides": {}, "default_share_model": ""}


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
        if not isinstance(cfg.get("machine_overrides"), dict):
            cfg["machine_overrides"] = {}
        if "default_share_model" not in cfg:
            cfg["default_share_model"] = ""
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
        plan_label = str(acct.get("plan_label") or acct.get("expected_plan") or "")[:120]
        accounts.append({
            "alias": alias,
            "label": str(acct.get("label") or alias)[:120],
            "expected_plan": plan_label,
            "plan_label": plan_label,
            "plan_details": str(acct.get("plan_details") or "")[:500],
            "codex_spark_access": bool(acct.get("codex_spark_access")),
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


def set_account_details(alias: str, label: str | None = None, plan_label: str | None = None, plan_details: str | None = None, codex_spark_access: bool | str | None = None) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", alias):
        raise ValueError("invalid alias")
    with config_lock:
        cfg = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else _default_config()
        accounts = cfg.setdefault("accounts", [])
        acct = next((a for a in accounts if isinstance(a, dict) and a.get("alias") == alias), None)
        if acct is None:
            acct = {"alias": alias, "label": alias, "expected_plan": ""}
            accounts.append(acct)
        if label is not None:
            clean = str(label or "").strip()[:120]
            if not clean:
                raise ValueError("display name cannot be empty")
            acct["label"] = clean
        if plan_label is not None:
            clean_plan = str(plan_label or "").strip()[:120]
            acct["plan_label"] = clean_plan
            acct["expected_plan"] = clean_plan
        if plan_details is not None:
            acct["plan_details"] = str(plan_details or "").strip()[:500]
        if codex_spark_access is not None:
            if isinstance(codex_spark_access, bool):
                acct["codex_spark_access"] = bool(codex_spark_access)
            else:
                raw = str(codex_spark_access).strip().lower()
                acct["codex_spark_access"] = raw in {"1", "true", "yes", "on", "y", "t"}
        save_config_unlocked(cfg)
        return acct


def _machine_override_key(agent: str, machine: str) -> str:
    return f"{str(agent or '').strip()}|||{str(machine or '').strip()}"


def set_machine_override(agent: str, machine: str, display_machine: str, machine_details: str) -> dict:
    agent = str(agent or "").strip()[:120]
    machine = str(machine or "").strip()[:160]
    if not (agent or machine):
        raise ValueError("agent or machine is required")
    display_machine = str(display_machine or "").strip()[:160]
    machine_details = str(machine_details or "").strip()[:500]
    key = _machine_override_key(agent, machine)
    with config_lock:
        cfg = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else _default_config()
        overrides = cfg.setdefault("machine_overrides", {})
        if display_machine or machine_details:
            overrides[key] = {"display_machine": display_machine, "machine_details": machine_details}
        else:
            overrides.pop(key, None)
        save_config_unlocked(cfg)
    return {"ok": True, "key": key, "override": {"display_machine": display_machine, "machine_details": machine_details}}


def apply_machine_overrides(rows: list[dict]) -> list[dict]:
    cfg = load_config()
    overrides = cfg.get("machine_overrides") if isinstance(cfg.get("machine_overrides"), dict) else {}
    for row in rows:
        key = _machine_override_key(row.get("agent"), row.get("machine"))
        override = overrides.get(key) if isinstance(overrides.get(key), dict) else {}
        row["machine_key"] = key
        row["display_machine"] = str(override.get("display_machine") or row.get("machine") or "")[:160]
        row["machine_details"] = str(override.get("machine_details") or "")[:500]
    return rows


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


def short_fingerprint(value: str | bytes | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.encode()
    return hashlib.sha256(value).hexdigest()[:12]


def extract_codex_tokens(auth_payload: dict) -> dict:
    tokens = auth_payload.get("tokens")
    if not isinstance(tokens, dict):
        raise ValueError("auth file is missing tokens object")
    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    if not isinstance(access_token, str) or not access_token:
        raise ValueError("auth file is missing tokens.access_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise ValueError("auth file is missing tokens.refresh_token")
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "account_id": tokens.get("account_id"),
        "id_token": tokens.get("id_token"),
        "last_refresh": auth_payload.get("last_refresh"),
    }


def hermes_auth_targets() -> list[Path]:
    hermes_home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    targets = [hermes_home / "auth.json"]
    profiles_root = hermes_home / "profiles"
    if profiles_root.exists():
        targets.extend(sorted(profiles_root.glob("*/auth.json")))
    # Only write stores that already exist; avoid inventing profile state.
    return [p for p in targets if p.exists()]


def safe_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    os.chmod(path, 0o600)



def sanitize_default_model(model: str | None) -> str:
    model = str(model or "").strip()[:120]
    if not model:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9._:/+-]{1,120}", model):
        raise ValueError("default model contains unsupported characters")
    return model


def get_current_default_model() -> str:
    # Prefer Hermes' configured default, then Codex CLI's top-level model.
    hermes_cfg = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "config.yaml"
    try:
        text = hermes_cfg.read_text()
        in_model = False
        for line in text.splitlines():
            if re.match(r"^model:\s*$", line):
                in_model = True
                continue
            if in_model and line and not line.startswith((" ", "\t")):
                in_model = False
            if in_model:
                m = re.match(r"^\s+default:\s*['\"]?([^'\"#\s]+)", line)
                if m:
                    return sanitize_default_model(m.group(1))
    except Exception:
        pass
    codex_cfg = CODEX_HOME / "config.toml"
    try:
        text = codex_cfg.read_text()
        m = re.search(r"(?m)^\s*model\s*=\s*['\"]([^'\"]+)['\"]", text)
        if m:
            return sanitize_default_model(m.group(1))
    except Exception:
        pass
    cfg_model = load_config().get("default_share_model")
    try:
        return sanitize_default_model(cfg_model)
    except Exception:
        return ""


def _toml_quote(value: str) -> str:
    return json.dumps(value)


def set_codex_default_model_config(path: Path, model: str) -> bool:
    model = sanitize_default_model(model)
    if not model:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text() if path.exists() else ""
    line = f"model = {_toml_quote(model)}"
    if re.search(r"(?m)^\s*model\s*=\s*['\"][^'\"]*['\"]\s*$", text):
        text = re.sub(r"(?m)^\s*model\s*=\s*['\"][^'\"]*['\"]\s*$", line, text, count=1)
    else:
        text = line + "\n" + text
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text if text.endswith("\n") else text + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    os.chmod(path, 0o600)
    return True


def set_hermes_default_model_config(path: Path, model: str) -> bool:
    model = sanitize_default_model(model)
    if not model:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text() if path.exists() else "model:\n  provider: openai-codex\n"
    lines = text.splitlines()
    out = []
    in_model = False
    saw_model = False
    set_default = False
    inserted = False
    for line in lines:
        if re.match(r"^model:\s*$", line):
            if in_model and not set_default:
                out.append(f"  default: {model}")
                inserted = True
            in_model = True
            saw_model = True
            set_default = False
            out.append(line)
            continue
        if in_model and line and not line.startswith((" ", "\t")):
            if not set_default:
                out.append(f"  default: {model}")
                inserted = True
            in_model = False
        if in_model and re.match(r"^\s+default:\s*", line):
            out.append(f"  default: {model}")
            set_default = True
            continue
        out.append(line)
    if in_model and not set_default:
        out.append(f"  default: {model}")
        inserted = True
    if not saw_model:
        out = ["model:", f"  default: {model}", "  provider: openai-codex", *out]
        inserted = True
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text("\n".join(out).rstrip() + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    os.chmod(path, 0o600)
    return True


def apply_default_model_to_local_configs(model: str) -> dict:
    model = sanitize_default_model(model)
    if not model:
        return {"requested": False, "model": "", "codex_config_updated": False, "hermes_configs_updated": []}
    codex_updated = set_codex_default_model_config(CODEX_HOME / "config.toml", model)
    hermes_home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    hermes_paths = [hermes_home / "config.yaml"]
    profiles = hermes_home / "profiles"
    if profiles.exists():
        hermes_paths.extend(sorted(profiles.glob("*/config.yaml")))
    hermes_results = []
    for path in hermes_paths:
        if path.exists() or path == hermes_home / "config.yaml":
            try:
                hermes_results.append({"path": str(path), "updated": set_hermes_default_model_config(path, model)})
            except Exception as exc:
                hermes_results.append({"path": str(path), "updated": False, "error": str(exc)})
    return {"requested": True, "model": model, "codex_config_updated": codex_updated, "hermes_configs_updated": hermes_results}

def update_hermes_codex_auth_store(path: Path, codex_tokens: dict, alias: str) -> dict:
    before = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(before, dict):
        raise ValueError(f"{path} is not a JSON object")
    data = json.loads(json.dumps(before))
    now = datetime.utcnow().isoformat(timespec="microseconds") + "Z"
    last_refresh = codex_tokens.get("last_refresh") or now

    providers = data.setdefault("providers", {})
    provider = providers.setdefault("openai-codex", {})
    provider["tokens"] = {
        "access_token": codex_tokens["access_token"],
        "refresh_token": codex_tokens["refresh_token"],
    }
    provider["last_refresh"] = last_refresh
    provider["auth_mode"] = provider.get("auth_mode") or "chatgpt"
    provider["synced_from"] = "codex-switch-share-auth"
    provider["synced_from_alias"] = alias
    provider["synced_at"] = now
    # A stale relogin_required error can make the UI/runtime look broken after a valid sync.
    provider.pop("last_auth_error", None)

    pool = data.setdefault("credential_pool", {})
    entries = pool.setdefault("openai-codex", [])
    if not isinstance(entries, list):
        entries = []
        pool["openai-codex"] = entries
    if not entries:
        entries.append({
            "id": hashlib.sha256((codex_tokens["refresh_token"] + alias).encode()).hexdigest()[:6],
            "label": f"codex-switch:{alias}",
            "auth_type": "oauth",
            "priority": 0,
            "source": "codex-switch-share-auth",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "request_count": 0,
        })
    updated_entries = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        is_oauth = entry.get("auth_type") == "oauth" or "access_token" in entry or "refresh_token" in entry
        if not is_oauth:
            continue
        entry["access_token"] = codex_tokens["access_token"]
        entry["refresh_token"] = codex_tokens["refresh_token"]
        entry["last_refresh"] = last_refresh
        entry["source"] = entry.get("source") or "codex-switch-share-auth"
        entry["synced_from_alias"] = alias
        entry["synced_at"] = now
        for key in ("last_status", "last_error_code", "last_error_reason", "last_error_message", "last_error_reset_at"):
            entry[key] = None
        updated_entries += 1

    data.setdefault("version", 1)
    data["active_provider"] = data.get("active_provider") or "openai-codex"
    data["updated_at"] = datetime.now().astimezone().isoformat()

    safe_write_json(path, data)
    return {
        "path": str(path),
        "provider_updated": True,
        "pool_entries_updated": updated_entries,
        "access_fingerprint": short_fingerprint(codex_tokens["access_token"]),
        "refresh_fingerprint": short_fingerprint(codex_tokens["refresh_token"]),
    }


def sync_auth_to_local_stores(alias: str, default_model: str | None = None) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", alias):
        raise ValueError("invalid alias")
    source_path = PROFILE_ROOT / alias / "auth.json"
    if not source_path.exists():
        raise FileNotFoundError(f"saved auth profile not found for {alias}")
    raw = source_path.read_bytes()
    source_payload = json.loads(raw.decode())
    if not isinstance(source_payload, dict):
        raise ValueError("auth file is not a JSON object")
    codex_tokens = extract_codex_tokens(source_payload)
    clean_default_model = sanitize_default_model(default_model)

    codex_target = CODEX_HOME / "auth.json"
    codex_target.parent.mkdir(parents=True, exist_ok=True)
    codex_target.write_bytes(raw)
    os.chmod(codex_target, 0o600)

    hermes_results = []
    for target in hermes_auth_targets():
        hermes_results.append(update_hermes_codex_auth_store(target, codex_tokens, alias))
    model_result = apply_default_model_to_local_configs(clean_default_model) if clean_default_model else {"requested": False, "model": "", "codex_config_updated": False, "hermes_configs_updated": []}

    return {
        "ok": True,
        "alias": alias,
        "source": str(source_path),
        "codex_target": str(codex_target),
        "hermes_targets": hermes_results,
        "fingerprints": {
            "payload": short_fingerprint(raw),
            "access_token": short_fingerprint(codex_tokens["access_token"]),
            "refresh_token": short_fingerprint(codex_tokens["refresh_token"]),
        },
        "default_model": model_result,
        "restart_note": "On-disk auth stores were updated. Long-lived Hermes processes may need an explicit approved restart to use the new in-memory credentials. Restart command after approval: hermes gateway restart (or hermes --profile <profile> gateway restart for profile-specific gateways).",
    }


def build_bash_multi_auth_installer(b64_payload: str, default_model: str = "") -> str:
    """Build a pasteable installer that syncs Codex CLI plus existing Hermes auth stores.

    The command contains the sensitive auth payload by design, but prints only safe
    fingerprints and paths when run.
    """
    clean_default_model = sanitize_default_model(default_model)
    return f"""set -euo pipefail
python3 - <<'PY_INSTALL'
import base64, hashlib, json, os, re
from datetime import datetime
from pathlib import Path

raw = base64.b64decode({b64_payload!r})
default_model = {clean_default_model!r}
home = Path.home()
codex_auth = home / '.codex' / 'auth.json'
codex_auth.parent.mkdir(parents=True, exist_ok=True)
codex_auth.write_bytes(raw)
os.chmod(codex_auth, 0o600)

payload = json.loads(raw.decode())
tokens = payload.get('tokens') if isinstance(payload, dict) else None
if not isinstance(tokens, dict):
    raise SystemExit('auth payload is missing tokens object')
access_token = tokens.get('access_token')
refresh_token = tokens.get('refresh_token')
if not isinstance(access_token, str) or not access_token:
    raise SystemExit('auth payload is missing tokens.access_token')
if not isinstance(refresh_token, str) or not refresh_token:
    raise SystemExit('auth payload is missing tokens.refresh_token')
last_refresh = payload.get('last_refresh') or (datetime.utcnow().isoformat(timespec='microseconds') + 'Z')
now = datetime.utcnow().isoformat(timespec='microseconds') + 'Z'

def fp(value):
    if isinstance(value, str):
        value = value.encode()
    return hashlib.sha256(value).hexdigest()[:12]

def write_json(path, data):
    tmp = path.with_name(f'.{{path.name}}.{{os.getpid()}}.tmp')
    tmp.write_text(json.dumps(data, indent=2, sort_keys=False) + '\\n')
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    os.chmod(path, 0o600)

def write_text_600(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'.{{path.name}}.{{os.getpid()}}.tmp')
    tmp.write_text(text if text.endswith('\\n') else text + '\\n')
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    os.chmod(path, 0o600)

def set_codex_default_model(path, model):
    if not model:
        return False
    if not re.fullmatch(r'[A-Za-z0-9._:/+-]{{1,120}}', model):
        raise SystemExit('default model contains unsupported characters')
    text = path.read_text() if path.exists() else ''
    line = 'model = ' + json.dumps(model)
    pattern = '''(?m)^\\s*model\\s*=\\s*['"][^'"]*['"]\\s*$'''
    if re.search(pattern, text):
        text = re.sub(pattern, line, text, count=1)
    else:
        text = line + '\\n' + text
    write_text_600(path, text)
    return True

def set_hermes_default_model(path, model):
    if not model:
        return False
    if not re.fullmatch(r'[A-Za-z0-9._:/+-]{{1,120}}', model):
        raise SystemExit('default model contains unsupported characters')
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text() if path.exists() else 'model:\\n  provider: openai-codex\\n'
    lines = text.splitlines()
    out = []
    in_model = False
    saw_model = False
    set_default = False
    for line in lines:
        if re.match(r'^model:\s*$', line):
            if in_model and not set_default:
                out.append(f'  default: {{model}}')
            in_model = True
            saw_model = True
            set_default = False
            out.append(line)
            continue
        if in_model and line and not line.startswith((' ', '\t')):
            if not set_default:
                out.append(f'  default: {{model}}')
            in_model = False
        if in_model and re.match(r'^\s+default:\s*', line):
            out.append(f'  default: {{model}}')
            set_default = True
            continue
        out.append(line)
    if in_model and not set_default:
        out.append(f'  default: {{model}}')
    if not saw_model:
        out = ['model:', f'  default: {{model}}', '  provider: openai-codex', *out]
    write_text_600(path, '\\n'.join(out).rstrip() + '\\n')
    return True

def apply_default_model(model):
    if not model:
        return []
    updated = []
    codex_config = home / '.codex' / 'config.toml'
    updated.append(('codex', str(codex_config), set_codex_default_model(codex_config, model)))
    hermes_home = Path(os.environ.get('HERMES_HOME', str(home / '.hermes')))
    config_targets = [hermes_home / 'config.yaml']
    profiles = hermes_home / 'profiles'
    if profiles.exists():
        config_targets.extend(sorted(profiles.glob('*/config.yaml')))
    for path in config_targets:
        if path.exists() or path == hermes_home / 'config.yaml':
            try:
                updated.append(('hermes', str(path), set_hermes_default_model(path, model)))
            except Exception as exc:
                updated.append(('hermes', str(path), f'ERROR: {{exc}}'))
    return updated

def sync_hermes_store(path):
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f'{{path}} is not a JSON object')
    provider = data.setdefault('providers', {{}}).setdefault('openai-codex', {{}})
    provider['tokens'] = {{'access_token': access_token, 'refresh_token': refresh_token}}
    provider['last_refresh'] = last_refresh
    provider['auth_mode'] = provider.get('auth_mode') or 'chatgpt'
    provider['synced_from'] = 'codex-switch-share-auth-paste-installer'
    provider['synced_at'] = now
    provider.pop('last_auth_error', None)

    pool = data.setdefault('credential_pool', {{}})
    entries = pool.setdefault('openai-codex', [])
    if not isinstance(entries, list):
        entries = []
        pool['openai-codex'] = entries
    if not entries:
        entries.append({{
            'id': hashlib.sha256(refresh_token.encode()).hexdigest()[:6],
            'label': 'codex-switch pasted auth',
            'auth_type': 'oauth',
            'priority': 0,
            'source': 'codex-switch-share-auth-paste-installer',
            'base_url': 'https://chatgpt.com/backend-api/codex',
            'request_count': 0,
        }})
    updated = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get('auth_type') == 'oauth' or 'access_token' in entry or 'refresh_token' in entry:
            entry['access_token'] = access_token
            entry['refresh_token'] = refresh_token
            entry['last_refresh'] = last_refresh
            entry['synced_at'] = now
            for key in ('last_status', 'last_error_code', 'last_error_reason', 'last_error_message', 'last_error_reset_at'):
                entry[key] = None
            updated += 1
    data.setdefault('version', 1)
    data['active_provider'] = data.get('active_provider') or 'openai-codex'
    data['updated_at'] = datetime.now().astimezone().isoformat()
    write_json(path, data)
    return updated

hermes_home = Path(os.environ.get('HERMES_HOME', str(home / '.hermes')))
targets = [hermes_home / 'auth.json']
profiles = hermes_home / 'profiles'
if profiles.exists():
    targets.extend(sorted(profiles.glob('*/auth.json')))
existing_targets = [p for p in targets if p.exists()]
results = []
for target in existing_targets:
    try:
        results.append((str(target), sync_hermes_store(target)))
    except Exception as exc:
        results.append((str(target), f'ERROR: {{exc}}'))

print('Installed Codex auth:', codex_auth)
print('Payload fp:', fp(raw))
print('Access token fp:', fp(access_token))
print('Refresh token fp:', fp(refresh_token))
print('Hermes stores updated:')
if results:
    for path, updated in results:
        print(f'- {{path}} (pool entries updated: {{updated}})')
else:
    print('- none found')
model_updates = apply_default_model(default_model)
if default_model:
    print('Default model set:', default_model)
    for kind, path, updated in model_updates:
        print(f'- {{kind}} config {{path}}: {{updated}}')
else:
    print('Default model unchanged')
print('Restart Hermes after approval if a gateway/session is already running: hermes gateway restart')
print('Profile-specific example: hermes --profile <profile> gateway restart')
PY_INSTALL"""


def build_share_auth(alias: str, default_model: str | None = None) -> dict:
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
    clean_default_model = sanitize_default_model(default_model)
    q_alias = shlex.quote(alias)
    q_bundle = shlex.quote(str(bundle_path))
    q_bin = shlex.quote(BIN)
    bash_payload_cmd = build_bash_multi_auth_installer(b64, clean_default_model)
    powershell_wsl_payload_cmd = (
        "$script = @'\n"
        f"{bash_payload_cmd}\n"
        "'@\n"
        "$script | wsl.exe bash -lc 'tmp=$(mktemp); cat > \"$tmp\"; bash \"$tmp\"; rc=$?; rm -f \"$tmp\"; exit $rc'"
    )
    powershell_windows_payload_cmd = (
        "$payload = @'\n"
        f"{b64}\n"
        "'@\n"
        "$dir = \"$HOME\\.codex\"\n"
        "New-Item -ItemType Directory -Force $dir | Out-Null\n"
        "[IO.File]::WriteAllBytes(\"$dir\\auth.json\", [Convert]::FromBase64String($payload))\n"
        + (f"$model = {json.dumps(clean_default_model)}\n$config = \"$dir\\config.toml\"\nif ($model) {{ if (Test-Path $config) {{ $text = Get-Content -Raw $config }} else {{ $text = \"\" }}; $line = \"model = \\\"$model\\\"\"; if ($text -match \"(?m)^\\s*model\\s*=\\s*[\\'\\\"][^\\'\\\"]*[\\'\\\"]\\s*$\") {{ $text = [regex]::Replace($text, \"(?m)^\\s*model\\s*=\\s*[\\'\\\"][^\\'\\\"]*[\\'\\\"]\\s*$\", $line, 1) }} else {{ $text = $line + [Environment]::NewLine + $text }}; Set-Content -Path $config -Value $text -NoNewline }}" if clean_default_model else "")
    )
    return {
        "ok": True,
        "alias": alias,
        "bundle_path": str(bundle_path),
        "warning": "Sensitive OAuth auth payload. Only paste/share with your own trusted agent machine. Rotate/re-auth if exposed.",
        "same_machine_switch_command": f"{q_bin} use {q_alias}",
        "same_machine_install_command": f"umask 077; mkdir -p ~/.codex; install -m 600 {q_bundle} ~/.codex/auth.json" + (f"; python3 - <<'PY_MODEL'\nfrom pathlib import Path\nimport json, re, os\nmodel = {clean_default_model!r}\npath = Path.home()/'.codex'/'config.toml'\ntext = path.read_text() if path.exists() else ''\nline = 'model = ' + json.dumps(model)\ntext = re.sub(r'(?m)^\\s*model\\s*=\\s*[\"\\\'][^\"\\\']*[\"\\\']\\s*$', line, text, count=1) if re.search(r'(?m)^\\s*model\\s*=\\s*[\"\\\'][^\"\\\']*[\"\\\']\\s*$', text) else line + '\\n' + text\npath.parent.mkdir(parents=True, exist_ok=True); path.write_text(text if text.endswith('\\n') else text+'\\n'); os.chmod(path,0o600)\nprint('Default Codex model set:', model)\nPY_MODEL" if clean_default_model else ""),
        # Backwards-compatible field. Default to the PowerShell->WSL installer because Windows Terminal/PowerShell often targets WSL-backed Hermes/Codex agents.
        "target_machine_payload_install_command": powershell_wsl_payload_cmd,
        "target_machine_powershell_wsl_install_command": powershell_wsl_payload_cmd,
        "target_machine_bash_install_command": bash_payload_cmd,
        "target_machine_powershell_windows_install_command": powershell_windows_payload_cmd,
        "target_machine_import_command_note": f"If you copy {bundle_path.name} to another machine, import it there with: {q_bin} import /path/to/{shlex.quote(bundle_path.name)} {q_alias}",
        "default_model": clean_default_model,
        "default_model_note": (f"Generated installers will set Codex + Hermes default model to {clean_default_model}." if clean_default_model else "Generated installers leave default model unchanged."),
    }


def get_machine_usage_source() -> str:
    cfg = load_config()
    return str(cfg.get("machine_usage_sheet_csv_url") or os.environ.get("CODEX_SWITCH_MACHINE_USAGE_SHEET_CSV_URL") or "").strip()


def set_machine_usage_source(url: str) -> dict:
    url = str(url or "").strip()
    if url and not (url.startswith("https://") or url.startswith("http://")):
        raise ValueError("sheet CSV URL must start with http:// or https://")
    with config_lock:
        cfg = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else _default_config()
        cfg["machine_usage_sheet_csv_url"] = url
        save_config_unlocked(cfg)
    return {"ok": True, "machine_usage_sheet_csv_url": url}


def _intish(value) -> int:
    try:
        return int(float(str(value or "0").replace(",", "").strip() or "0"))
    except Exception:
        return 0


def _row_value(row: dict, *names: str) -> str:
    lower = {str(k).strip().lower(): v for k, v in row.items()}
    for name in names:
        if name.lower() in lower:
            return str(lower[name.lower()] or "").strip()
    return ""


def _parse_iso_ts(value: str) -> datetime | None:
    value = str(value or "").strip()
    if not value:
        return None
    try:
        cleaned = value[:-1] + "+00:00" if value.endswith("Z") else value
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=MELBOURNE_TZ)
        return dt
    except Exception:
        return None


def get_machine_usage(force: bool = False) -> dict:
    source = get_machine_usage_source()
    now = datetime.now(MELBOURNE_TZ)
    if not source:
        return {"ok": True, "configured": False, "source": "google_sheet_csv", "rows": [], "totals": {"tokens_30m": 0, "tokens_1h": 0, "tokens_2h": 0, "tokens_3h": 0, "tokens_24h": 0, "tokens_7d": 0}, "message": "Set a published Google Sheet CSV URL first."}

    if not force and MACHINE_USAGE_CACHE.exists():
        try:
            cached = json.loads(MACHINE_USAGE_CACHE.read_text())
            fetched_at = _parse_iso_ts(cached.get("fetched_at"))
            if fetched_at and (now - fetched_at).total_seconds() < REFRESH_INTERVAL_SECONDS:
                cached["cache_hit"] = True
                cached["rows"] = apply_machine_overrides(cached.get("rows") or [])
                return cached
        except Exception:
            pass

    req = Request(source, headers={"User-Agent": "codex-switch-local-dashboard/1"})
    with urlopen(req, timeout=25) as resp:
        text = resp.read().decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for raw in reader:
        agent = _row_value(raw, "agent", "agent_label", "label")
        machine = _row_value(raw, "machine", "hostname", "host")
        if not (agent or machine):
            continue
        generated = _parse_iso_ts(_row_value(raw, "generated_at", "heartbeat_at", "updated_at"))
        age_minutes = None
        status = "gray"
        if generated:
            age_minutes = max(0, round((now - generated.astimezone(MELBOURNE_TZ)).total_seconds() / 60, 1))
            status = "green" if age_minutes <= 30 else "warn" if age_minutes <= 90 else "bad"
        row = {
            "agent": agent,
            "machine": machine,
            "os": _row_value(raw, "os"),
            "current_account_alias": _row_value(raw, "current_account_alias", "account_alias", "alias"),
            "auth_fingerprint": _row_value(raw, "auth_fingerprint", "auth_hash")[:80],
            "tokens_30m": _intish(_row_value(raw, "tokens_30m", "tokens_30min", "tokens_last_30m")),
            "tokens_1h": _intish(_row_value(raw, "tokens_1h", "tokens_60m", "tokens_last_1h")),
            "tokens_2h": _intish(_row_value(raw, "tokens_2h", "tokens_120m", "tokens_last_2h")),
            "tokens_3h": _intish(_row_value(raw, "tokens_3h", "tokens_180m", "tokens_last_3h")),
            "tokens_24h": _intish(_row_value(raw, "tokens_24h", "tokens_1d")),
            "tokens_7d": _intish(_row_value(raw, "tokens_7d", "tokens_week")),
            "thread_count_24h": _intish(_row_value(raw, "thread_count_24h", "threads_24h")),
            "thread_count_7d": _intish(_row_value(raw, "thread_count_7d", "threads_7d")),
            "rate_limit_errors_24h": _intish(_row_value(raw, "rate_limit_errors_24h", "rate_limits_24h")),
            "last_used_at": _row_value(raw, "last_used_at"),
            "generated_at": generated.isoformat() if generated else _row_value(raw, "generated_at", "heartbeat_at", "updated_at"),
            "age_minutes": age_minutes,
            "status": status,
        }
        rows.append(row)
    rows.sort(key=lambda r: r.get("tokens_24h", 0), reverse=True)
    result = {
        "ok": True,
        "configured": True,
        "source": "google_sheet_csv",
        "sheet_csv_url": source,
        "fetched_at": now.isoformat(),
        "row_count": len(rows),
        "totals": {
            "tokens_30m": sum(r["tokens_30m"] for r in rows),
            "tokens_1h": sum(r["tokens_1h"] for r in rows),
            "tokens_2h": sum(r["tokens_2h"] for r in rows),
            "tokens_3h": sum(r["tokens_3h"] for r in rows),
            "tokens_24h": sum(r["tokens_24h"] for r in rows),
            "tokens_7d": sum(r["tokens_7d"] for r in rows),
            "rate_limit_errors_24h": sum(r["rate_limit_errors_24h"] for r in rows),
        },
        "rows": apply_machine_overrides(rows),
    }
    MACHINE_USAGE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    MACHINE_USAGE_CACHE.write_text(json.dumps(result, indent=2) + "\n")
    os.chmod(MACHINE_USAGE_CACHE, 0o600)
    return result


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


def _safe_key_fingerprint(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()[:12] if api_key else "missing"


def load_anthropic_accounts() -> list[dict]:
    if not ANTHROPIC_ACCOUNTS_PATH.exists():
        return []
    data = json.loads(ANTHROPIC_ACCOUNTS_PATH.read_text())
    accounts = data.get("accounts") if isinstance(data, dict) else data
    if not isinstance(accounts, list):
        return []
    clean = []
    for idx, acct in enumerate(accounts):
        if not isinstance(acct, dict):
            continue
        key = str(acct.get("api_key") or "").strip()
        label = str(acct.get("label") or acct.get("alias") or f"Anthropic {idx + 1}")[:120]
        alias = str(acct.get("alias") or re.sub(r"[^a-z0-9._-]+", "-", label.lower()).strip("-") or f"anthropic-{idx + 1}")[:64]
        clean.append({"alias": alias, "label": label, "api_key": key})
    return clean


def anthropic_get(api_key: str, path: str, params: dict | None = None) -> dict:
    query = urlencode(params or {}, doseq=True)
    url = f"https://api.anthropic.com{path}" + (f"?{query}" if query else "")
    req = Request(url, headers={
        "anthropic-version": "2023-06-01",
        "x-api-key": api_key,
        "User-Agent": "codex-account-usage-local/1.0",
    })
    try:
        with urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return {"ok": True, "status": resp.status, "data": json.loads(raw) if raw else {}}
    except Exception as exc:
        status = getattr(exc, "code", None)
        body = ""
        try:
            reader = getattr(exc, "read", None)
            body = reader().decode("utf-8", "replace")[:1000] if reader else str(exc)[:1000]
        except Exception:
            body = str(exc)[:1000]
        return {"ok": False, "status": status, "error": body or str(exc)}


def anthropic_admin_get(api_key: str, path: str, params: dict) -> dict:
    return anthropic_get(api_key, path, params)


def _sum_numbers(obj, wanted_keys: set[str]) -> float:
    total = 0.0
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in wanted_keys and isinstance(value, (int, float)):
                total += float(value)
            elif isinstance(value, (dict, list)):
                total += _sum_numbers(value, wanted_keys)
    elif isinstance(obj, list):
        for item in obj:
            total += _sum_numbers(item, wanted_keys)
    return total


def get_anthropic_usage() -> dict:
    accounts = load_anthropic_accounts()
    now = datetime.now(MELBOURNE_TZ)
    ending = now.astimezone(ZoneInfo("UTC")).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    start_1d = (now - timedelta(days=1)).astimezone(ZoneInfo("UTC")).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    start_7d = (now - timedelta(days=7)).astimezone(ZoneInfo("UTC")).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    rows = []
    for acct in accounts:
        api_key = acct["api_key"]
        row = {
            "alias": acct["alias"],
            "label": acct["label"],
            "key_fingerprint": _safe_key_fingerprint(api_key),
            "configured": bool(api_key and not api_key.endswith("...FQAA") and not api_key.endswith("...UQAA") and "..." not in api_key),
        }
        if not row["configured"]:
            row.update({"ok": False, "error": "API key not configured in local anthropic-accounts.json"})
            rows.append(row)
            continue
        models = anthropic_get(api_key, "/v1/models")
        row["key_valid"] = bool(models.get("ok"))
        row["models_count"] = len((models.get("data") or {}).get("data", [])) if models.get("ok") else 0
        usage_1d = anthropic_admin_get(api_key, "/v1/organizations/usage_report/messages", {"starting_at": start_1d, "ending_at": ending, "bucket_width": "1d"})
        usage_7d = anthropic_admin_get(api_key, "/v1/organizations/usage_report/messages", {"starting_at": start_7d, "ending_at": ending, "bucket_width": "1d"})
        cost_7d = anthropic_admin_get(api_key, "/v1/organizations/cost_report", {"starting_at": start_7d, "ending_at": ending, "group_by[]": ["description"]})
        row["usage_1d_ok"] = usage_1d.get("ok")
        row["usage_7d_ok"] = usage_7d.get("ok")
        row["cost_7d_ok"] = cost_7d.get("ok")
        row["ok"] = bool(usage_1d.get("ok") or usage_7d.get("ok") or cost_7d.get("ok"))
        row["input_tokens_1d"] = int(_sum_numbers(usage_1d.get("data"), {"input_tokens", "input_token_count", "uncached_input_tokens"})) if usage_1d.get("ok") else 0
        row["output_tokens_1d"] = int(_sum_numbers(usage_1d.get("data"), {"output_tokens", "output_token_count"})) if usage_1d.get("ok") else 0
        row["total_tokens_1d"] = int(_sum_numbers(usage_1d.get("data"), {"input_tokens", "input_token_count", "uncached_input_tokens", "output_tokens", "output_token_count"})) if usage_1d.get("ok") else 0
        row["input_tokens_7d"] = int(_sum_numbers(usage_7d.get("data"), {"input_tokens", "input_token_count", "uncached_input_tokens"})) if usage_7d.get("ok") else 0
        row["output_tokens_7d"] = int(_sum_numbers(usage_7d.get("data"), {"output_tokens", "output_token_count"})) if usage_7d.get("ok") else 0
        row["total_tokens_7d"] = int(_sum_numbers(usage_7d.get("data"), {"input_tokens", "input_token_count", "uncached_input_tokens", "output_tokens", "output_token_count"})) if usage_7d.get("ok") else 0
        row["cost_usd_7d"] = round(_sum_numbers(cost_7d.get("data"), {"cost", "amount", "amount_usd", "total_cost", "total_cost_usd"}), 4) if cost_7d.get("ok") else None
        errors = []
        for name, result in (("usage_1d", usage_1d), ("usage_7d", usage_7d), ("cost_7d", cost_7d)):
            if not result.get("ok"):
                errors.append({"source": name, "status": result.get("status"), "error": result.get("error")})
        row["errors"] = errors[:3]
        row["credit_note"] = "Usage/cost/credit require Anthropic Admin Usage & Cost API access. This key is valid for normal API calls but admin reports are blocked." if row.get("key_valid") and not row.get("ok") else "Anthropic's documented Admin API exposes usage and cost reports; credit/balance may not be available through public API for this key."
        rows.append(row)
    return {
        "ok": True,
        "configured_count": len([r for r in rows if r.get("configured")]),
        "account_count": len(rows),
        "source": "anthropic_admin_usage_cost_api",
        "config_path": str(ANTHROPIC_ACCOUNTS_PATH),
        "ending_at": ending,
        "start_1d": start_1d,
        "start_7d": start_7d,
        "rows": rows,
    }


def load_grok_accounts() -> list[dict]:
    if not GROK_ACCOUNTS_PATH.exists():
        return []
    data = json.loads(GROK_ACCOUNTS_PATH.read_text())
    accounts = data.get("accounts") if isinstance(data, dict) else data
    if not isinstance(accounts, list):
        return []
    clean = []
    for idx, acct in enumerate(accounts):
        if not isinstance(acct, dict):
            continue
        key = str(acct.get("api_key") or "").strip()
        label = str(acct.get("label") or acct.get("alias") or f"Grok {idx + 1}")[:120]
        alias = str(acct.get("alias") or re.sub(r"[^a-z0-9._-]+", "-", label.lower()).strip("-") or f"grok-{idx + 1}")[:64]
        clean.append({"alias": alias, "label": label, "api_key": key})
    return clean


def _api_key_accounts_path(provider: str) -> Path:
    if provider == "anthropic":
        return ANTHROPIC_ACCOUNTS_PATH
    if provider == "grok":
        return GROK_ACCOUNTS_PATH
    raise ValueError("provider must be anthropic or grok")


def add_api_key_account(provider: str, alias: str, label: str, api_key: str) -> dict:
    provider = str(provider or "").strip().lower()
    if provider not in {"anthropic", "grok"}:
        raise ValueError("provider must be anthropic or grok")
    alias = str(alias or "").strip()
    label = str(label or "").strip()[:120]
    api_key = str(api_key or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", alias):
        raise ValueError("invalid alias")
    if not label:
        raise ValueError("display name cannot be empty")
    if not api_key or "..." in api_key:
        raise ValueError("paste the full API key; placeholders are not saved")
    if provider == "anthropic" and not api_key.startswith("sk-ant-"):
        raise ValueError("Anthropic keys normally start with sk-ant-")
    if provider == "grok" and not (api_key.startswith("xai-") or api_key.startswith("sk-")):
        raise ValueError("xAI/Grok keys normally start with xai- or sk-")

    path = _api_key_accounts_path(provider)
    if path.exists():
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            data = {"accounts": data if isinstance(data, list) else []}
    else:
        data = {"accounts": []}
    accounts = data.setdefault("accounts", [])
    if not isinstance(accounts, list):
        accounts = []
        data["accounts"] = accounts

    before_count = len(accounts)
    accounts[:] = [acct for acct in accounts if not (isinstance(acct, dict) and str(acct.get("alias") or "") == alias)]
    accounts.append({"alias": alias, "label": label, "api_key": api_key})
    safe_write_json(path, data)
    return {
        "ok": True,
        "provider": provider,
        "alias": alias,
        "label": label,
        "config_path": str(path),
        "replaced": len(accounts) < before_count + 1,
        "key_fingerprint": _safe_key_fingerprint(api_key),
        "note": "Saved locally with chmod 600. Raw API key is not returned; refresh the provider panel to validate it.",
    }


def grok_get(api_key: str, path: str, params: dict | None = None) -> dict:
    query = urlencode(params or {}, doseq=True)
    url = f"https://api.x.ai{path}" + (f"?{query}" if query else "")
    req = Request(url, headers={
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "codex-account-usage-local/1.0",
    })
    try:
        with urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return {"ok": True, "status": resp.status, "data": json.loads(raw) if raw else {}}
    except Exception as exc:
        status = getattr(exc, "code", None)
        body = ""
        try:
            reader = getattr(exc, "read", None)
            body = reader().decode("utf-8", "replace")[:1000] if reader else str(exc)[:1000]
        except Exception:
            body = str(exc)[:1000]
        return {"ok": False, "status": status, "error": body or str(exc)}


def get_grok_usage() -> dict:
    accounts = load_grok_accounts()
    rows = []
    for acct in accounts:
        api_key = acct["api_key"]
        configured = bool(api_key and "..." not in api_key)
        row = {
            "alias": acct["alias"],
            "label": acct["label"],
            "key_fingerprint": _safe_key_fingerprint(api_key),
            "configured": configured,
        }
        if not configured:
            row.update({"ok": False, "key_valid": False, "error": "Grok/xAI API key not configured in local grok-accounts.json"})
            rows.append(row)
            continue
        models = grok_get(api_key, "/v1/models")
        model_items = (models.get("data") or {}).get("data", []) if models.get("ok") else []
        row.update({
            "ok": bool(models.get("ok")),
            "key_valid": bool(models.get("ok")),
            "models_count": len(model_items),
            "models": [str(m.get("id") or m.get("model") or "") for m in model_items[:20] if isinstance(m, dict)],
            "usage_note": "xAI/Grok public API key validation is available via /v1/models. Usage, spend, or credit balance API was not found in the public docs during implementation; add it here if xAI exposes an account usage endpoint.",
        })
        if not models.get("ok"):
            row["errors"] = [{"source": "models", "status": models.get("status"), "error": models.get("error")}]
        rows.append(row)
    return {
        "ok": True,
        "configured_count": len([r for r in rows if r.get("configured")]),
        "account_count": len(rows),
        "source": "xai_grok_api_models_probe",
        "config_path": str(GROK_ACCOUNTS_PATH),
        "checked_at": datetime.now(MELBOURNE_TZ).isoformat(),
        "rows": rows,
    }


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
    :root { color-scheme: dark; --bg:#0b0f14; --card:#111923; --muted:#8da2b5; --text:#e9f0f6; --line:#223142; --green:#41d17d; --yellow:#ffd166; --red:#ff5d5d; --blue:#62a8ff; --orange:#ff9f43; --purple:#b38cff; }
    * { box-sizing:border-box; }
    body { margin:0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif; background:radial-gradient(circle at 12% -10%, rgba(98,168,255,.16), transparent 34%), linear-gradient(135deg,#07111f,#101018); color:var(--text); overflow-x:hidden; }
    header { padding:28px 28px 16px; border-bottom:1px solid var(--line); background:rgba(0,0,0,.34); position:sticky; top:0; backdrop-filter: blur(10px); z-index:2; }
    h1 { margin:0 0 8px; font-size:28px; }
    .sub { color:var(--muted); }
    main { padding:18px; max-width:none; width:100%; margin:0; }
    .grid { display:flex; flex-wrap:nowrap; gap:14px; overflow-x:auto; overflow-y:hidden; padding:4px 4px 20px; scroll-snap-type:x proximity; width:100%; max-width:100%; }
    .grid .card { flex:0 0 312px; min-width:312px; scroll-snap-align:start; }
    .grid .card.dragging { opacity:.45; outline:2px dashed var(--blue); }
    .drag-handle { cursor:grab; user-select:none; color:var(--muted); font-size:12px; margin-bottom:8px; display:inline-flex; gap:5px; align-items:center; }
    .drag-handle:active { cursor:grabbing; }
    .card { background:rgba(17,25,35,.94); border:1px solid var(--line); border-radius:16px; padding:14px; box-shadow:0 8px 30px rgba(0,0,0,.25); }
    .usage-card { position:relative; overflow:hidden; padding:0; isolation:isolate; transition:transform .18s ease, border-color .18s ease, box-shadow .18s ease; }
    .usage-card:hover { transform:translateY(-2px); border-color:#365674; }
    .usage-card::before { content:""; position:absolute; inset:0 0 auto; height:4px; background:linear-gradient(90deg,var(--green),var(--blue)); opacity:.85; }
    .usage-card.is-warn::before { background:linear-gradient(90deg,var(--yellow),var(--orange)); animation:warningSweep 2.6s ease-in-out infinite; }
    .usage-card.is-bad::before { background:linear-gradient(90deg,var(--red),var(--orange),var(--red)); animation:criticalSweep 1.25s ease-in-out infinite; }
    .usage-card.is-warn { border-color:rgba(255,209,102,.55); box-shadow:0 0 0 1px rgba(255,209,102,.08),0 14px 42px rgba(255,159,67,.12); }
    .usage-card.is-bad { border-color:rgba(255,93,93,.74); box-shadow:0 0 0 1px rgba(255,93,93,.18),0 18px 58px rgba(255,93,93,.18); animation:dangerPulse 1.6s ease-in-out infinite; }
    .usage-card-inner { padding:16px; }
    .row { display:flex; justify-content:space-between; gap:8px; align-items:center; }
    .alias { font-size:16px; font-weight:800; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:190px; }
    .pill { padding:3px 7px; border-radius:999px; background:#1c2a38; color:var(--muted); font-size:11px; white-space:nowrap; }
    .status-badge { border:1px solid #2c4056; background:#101923; }
    .status-badge.ok { color:var(--green); border-color:rgba(65,209,125,.35); background:rgba(65,209,125,.08); }
    .status-badge.warn { color:var(--yellow); border-color:rgba(255,209,102,.55); background:rgba(255,209,102,.1); animation:badgeBlink 2s ease-in-out infinite; }
    .status-badge.bad { color:var(--red); border-color:rgba(255,93,93,.7); background:rgba(255,93,93,.12); animation:badgeBlink 1s ease-in-out infinite; }
    .ok { color:var(--green); } .warn { color:var(--yellow); } .bad { color:var(--red); }
    .bar { height:10px; background:#0b1118; border-radius:999px; overflow:hidden; border:1px solid #243447; margin:6px 0 2px; position:relative; }
    .bar::after { content:""; position:absolute; inset:0; background:linear-gradient(90deg, transparent, rgba(255,255,255,.18), transparent); transform:translateX(-110%); animation:barScan 3.4s ease-in-out infinite; opacity:.45; }
    .fill { height:100%; width:0%; background:var(--green); transition:width .55s cubic-bezier(.2,.8,.2,1); }
    .fill.warn { background:linear-gradient(90deg,var(--yellow),var(--orange)); }
    .fill.bad { background:linear-gradient(90deg,var(--red),var(--orange)); }
    .usage-visual { display:grid; grid-template-columns:96px 1fr; gap:12px; align-items:center; margin:12px 0 10px; }
    .usage-ring { --p:0; --ring:var(--green); width:88px; height:88px; border-radius:50%; display:grid; place-items:center; background:conic-gradient(var(--ring) calc(var(--p)*1%), #0b1118 0); box-shadow:inset 0 0 0 1px #25364a,0 0 22px rgba(98,168,255,.08); position:relative; }
    .usage-ring::after { content:""; position:absolute; inset:10px; border-radius:50%; background:#0f1721; border:1px solid #26384c; }
    .usage-ring.warn { --ring:var(--yellow); box-shadow:0 0 24px rgba(255,209,102,.18), inset 0 0 0 1px #4d3e14; }
    .usage-ring.bad { --ring:var(--red); box-shadow:0 0 30px rgba(255,93,93,.25), inset 0 0 0 1px #5a2424; animation:ringAlarm 1.35s ease-in-out infinite; }
    .ring-text { position:relative; z-index:1; text-align:center; font-weight:900; font-size:19px; line-height:1; }
    .ring-text span { display:block; font-size:10px; color:var(--muted); font-weight:700; margin-top:3px; }
    .usage-meta { display:grid; gap:7px; min-width:0; }
    .meter { border:1px solid #26384c; background:#0c131c; border-radius:12px; padding:8px; }
    .metric-strip { display:grid; grid-template-columns:repeat(3,1fr); gap:6px; margin:10px 0; }
    .mini-metric { border:1px solid #26384c; background:#0b121a; border-radius:10px; padding:8px; min-width:0; }
    .mini-metric b { display:block; font-size:15px; }
    .mini-metric span { color:var(--muted); font-size:10px; text-transform:uppercase; letter-spacing:.06em; }
    .warn-callout { display:none; align-items:flex-start; gap:8px; border-radius:12px; padding:10px; margin:10px 0; border:1px solid rgba(255,209,102,.45); background:rgba(255,209,102,.09); color:#ffe2a3; }
    .usage-card.is-warn .warn-callout, .usage-card.is-bad .warn-callout { display:flex; }
    .usage-card.is-bad .warn-callout { border-color:rgba(255,93,93,.6); background:rgba(255,93,93,.1); color:#ffb7b7; }
    .sparkline { width:100%; height:28px; overflow:visible; }
    .sparkline path { fill:none; stroke:var(--blue); stroke-width:2; stroke-linecap:round; stroke-linejoin:round; filter:drop-shadow(0 0 5px rgba(98,168,255,.3)); stroke-dasharray:120; animation:drawLine 1.1s ease both; }
    .sparkline.warn path { stroke:var(--yellow); filter:drop-shadow(0 0 5px rgba(255,209,102,.35)); }
    .sparkline.bad path { stroke:var(--red); filter:drop-shadow(0 0 6px rgba(255,93,93,.45)); animation:drawLine .85s ease both, jitter .35s steps(2,end) infinite; }
    button, a.btn { display:inline-flex; align-items:center; justify-content:center; gap:6px; border:1px solid #2f4761; background:#16263a; color:var(--text); padding:8px 10px; border-radius:10px; cursor:pointer; text-decoration:none; font-weight:700; font-size:13px; }
    button:hover, a.btn:hover { background:#1d3450; }
    .btn.primary { background:#14539a; border-color:#2e7ccc; }
    .muted { color:var(--muted); font-size:12px; }
    .code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; background:#05080c; border:1px solid var(--line); border-radius:10px; padding:9px; overflow:auto; }
    input, select { width:100%; background:#05080c; color:var(--text); border:1px solid #2d4158; border-radius:9px; padding:8px; margin-top:6px; font-size:13px; }
    .inline-label { display:block; margin-top:8px; }
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
    textarea.compact { min-height:64px; max-height:110px; margin-top:6px; font:12px Inter, ui-sans-serif, system-ui, sans-serif; }
    .danger-note { color:#ffd166; border:1px solid #5d4a16; background:#241b08; border-radius:12px; padding:10px; margin:10px 0; }
    @keyframes warningSweep { 0%,100%{opacity:.7} 50%{opacity:1} }
    @keyframes criticalSweep { 0%,100%{filter:brightness(.9)} 50%{filter:brightness(1.45)} }
    @keyframes dangerPulse { 0%,100%{transform:translateY(0)} 50%{transform:translateY(-1px)} }
    @keyframes badgeBlink { 0%,100%{box-shadow:0 0 0 rgba(255,209,102,0)} 50%{box-shadow:0 0 16px currentColor} }
    @keyframes ringAlarm { 0%,100%{transform:scale(1)} 50%{transform:scale(1.025)} }
    @keyframes barScan { 0%{transform:translateX(-110%)} 55%,100%{transform:translateX(110%)} }
    @keyframes drawLine { from{stroke-dashoffset:120} to{stroke-dashoffset:0} }
    @keyframes jitter { 50%{transform:translateX(1px)} }
    @media (prefers-reduced-motion: reduce) { *, *::before, *::after { animation:none !important; transition:none !important; } }
    @media (max-width: 560px) { header{padding:20px 16px 12px} main{padding:12px} .grid .card{flex-basis:86vw; min-width:86vw} .usage-visual{grid-template-columns:82px 1fr} .usage-ring{width:76px;height:76px}.metric-strip{grid-template-columns:1fr 1fr}.alias{max-width:150px} }
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
    <div class="alias">Add auth</div>
    <p class="muted">Choose the provider first. Codex keeps the existing device-code flow. Anthropic and Grok save a local API key config used by the usage/auth panels.</p>
    <div class="row" style="align-items:flex-end; flex-wrap:wrap">
      <label style="flex:1; min-width:190px"><span class="muted">Provider</span><select id="newProvider" onchange="updateAddAuthRequirements()"><option value="codex">OpenAI Codex OAuth</option><option value="anthropic">Anthropic API key</option><option value="grok">Grok / xAI API key</option></select></label>
      <label style="flex:1; min-width:220px"><span class="muted">Display name</span><input id="newLabel" placeholder="e.g. Spare Pro account" maxlength="120" /></label>
      <label style="flex:1; min-width:180px"><span class="muted">Alias</span><input id="newAlias" placeholder="e.g. spare-pro" maxlength="64" /></label>
      <label id="newApiKeyWrap" style="flex:2; min-width:260px; display:none"><span class="muted">API key</span><input id="newApiKey" type="password" autocomplete="off" placeholder="Paste provider API key; saved locally only" /></label>
      <button class="btn primary" onclick="addAccount()">Add auth</button>
    </div>
    <pre class="muted" id="addAuthRequirements" style="margin-top:10px"></pre>
    <div class="muted" id="addAccountStatus"></div>
  </section>
  <section class="grid" id="cards" aria-label="Account cards. Drag cards left or right to reorder."></section>
  <section class="card" id="codexSparkCardsSection" style="margin-top:18px; display:none">
    <div class="alias">Codex Spark eligible accounts</div>
    <div class="muted" id="codexSparkSummary">Only accounts marked for Codex Spark usage access are shown here.</div>
    <section class="grid" id="codexSparkCards" aria-label="Codex Spark account cards."></section>
  </section>
  <section class="card" style="margin-top:18px">
    <div class="alias">Anthropic API usage + cost</div>
    <p class="muted">Tracks configured Anthropic Admin/API keys from local config. Shows 24h/7d token usage and 7d cost when the key has Admin Usage & Cost API access. Credit/balance is reported only if Anthropic exposes it through the API.</p>
    <div class="actions"><button onclick="loadAnthropicUsage(true)">Refresh Anthropic usage</button><button onclick="toggleAnthropicApi()">Show API</button><a class="btn" href="/api/anthropic-usage" target="_blank">Raw Anthropic JSON</a></div>
    <pre id="anthropicApiDetails" class="muted" style="display:none; margin-top:10px"></pre>
    <div id="anthropicUsageSummary" style="margin-top:10px"></div>
    <div id="anthropicUsageRows"></div>
  </section>
  <section class="card" style="margin-top:18px">
    <div class="alias">Grok / xAI API auth</div>
    <p class="muted">Tracks configured Grok/xAI API keys from local config. Validates auth via xAI's models endpoint. Usage/spend/credit will stay noted as unavailable unless xAI exposes a public usage/billing endpoint for the key.</p>
    <div class="actions"><button onclick="loadGrokUsage(true)">Refresh Grok auth</button><button onclick="toggleGrokApi()">Show API</button><a class="btn" href="/api/grok-usage" target="_blank">Raw Grok JSON</a></div>
    <pre id="grokApiDetails" class="muted" style="display:none; margin-top:10px"></pre>
    <div id="grokUsageSummary" style="margin-top:10px"></div>
    <div id="grokUsageRows"></div>
  </section>
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
    <div class="alias">Machine usage — Google Sheet</div>
    <p class="muted">Each machine updates one row in a shared Google Sheet. Paste the Sheet's CSV export/publish URL here; dashboard ranks machines by local observed tokens.</p>
    <div class="row" style="align-items:flex-end; flex-wrap:wrap">
      <label style="flex:1; min-width:360px"><span class="muted">Published/export CSV URL</span><input id="machineSheetUrl" placeholder="https://docs.google.com/spreadsheets/d/.../export?format=csv&gid=0" /></label>
      <button onclick="saveMachineSheetUrl()">Save source</button>
      <button onclick="loadMachineUsage(true)">Refresh machine usage</button>
    </div>
    <div class="row" style="align-items:flex-end; flex-wrap:wrap; margin-top:8px">
      <label style="flex:1; min-width:260px"><span class="muted">Usage log filter</span><input id="machineUsageFilter" placeholder="Filter by agent, machine, account, OS, status…" oninput="renderMachineUsageTable()" /></label>
      <button onclick="document.getElementById('machineUsageFilter').value=''; renderMachineUsageTable();">Clear filter</button>
    </div>
    <div id="machineUsageSummary" style="margin-top:10px"></div>
    <div id="machineUsageRows"></div>
  </section>
  <section class="card" style="margin-top:18px">
    <div class="alias">Auth setup links</div>
    <p class="muted">Open each local link below, then click the OpenAI device page and enter the displayed code. Use different browser profiles/incognito sessions if you need to keep accounts separate.</p>
    <div class="actions" id="authLinks"></div>
    <hr style="border:0; border-top:1px solid var(--line); margin:16px 0" />
    <div class="alias">Sharefile setup</div>
    <p class="muted">Copy/paste this instruction into each agent/machine. It tells the agent how to access the shared Google Sheet and maintain its own row with a background cron job.</p>
    <textarea id="sharefilePrompt" readonly style="min-height:360px"></textarea>
    <div class="actions"><button onclick="copyText('sharefilePrompt')">Copy sharefile setup instruction</button><button onclick="renderSharefilePrompt()">Regenerate instruction</button></div>
  </section>
</main>
<div class="modal" id="shareModal">
  <div class="modal-card">
    <div class="row"><div class="alias" id="shareTitle">Share auth</div><button onclick="closeShareModal()">Close</button></div>
    <div class="danger-note">Sensitive OAuth auth payload. Only paste/share with your own trusted agent machine. Anyone with this payload can use that Codex auth until revoked/re-authenticated.</div>
    <label class="inline-label"><span class="muted">Default model to set on target machine (optional)</span><select id="shareDefaultModel" onchange="regenerateShareAuth()"><option value="">Leave target default model unchanged</option><option value="gpt-5.5">gpt-5.5</option><option value="gpt-5.4">gpt-5.4</option><option value="gpt-5.3-codex-spark">gpt-5.3-codex-spark</option><option value="gpt-5.3-codex">gpt-5.3-codex</option><option value="custom">Custom…</option></select></label>
    <label class="inline-label" id="shareCustomModelWrap" style="display:none"><span class="muted">Custom default model</span><input id="shareCustomModel" placeholder="e.g. gpt-5.5" oninput="debouncedRegenerateShareAuth()" /></label>
    <p class="muted" id="shareModelNote">Generated installers can also set ~/.codex/config.toml and Hermes config.yaml model defaults.</p>
    <p class="muted">Fast local repoint on this same machine:</p>
    <textarea id="shareLocal" readonly></textarea>
    <div class="actions"><button onclick="copyText('shareLocal')">Copy local command</button><button onclick="downloadText('shareLocal','local-command')">Download as text file</button><button id="syncLocalAuthButton" onclick="syncLocalAuth()">Apply to this machine: Codex + Hermes stores</button></div>
    <pre id="shareSyncResult" class="muted" style="display:none"></pre>
    <p class="muted">PowerShell → WSL installer for Windows Terminal/PowerShell when Hermes/Codex runs inside WSL. This contains the auth payload and syncs Codex + existing Hermes stores inside WSL; do not paste into chats/logs.</p>
    <textarea id="sharePayload" readonly></textarea>
    <div class="actions"><button onclick="copyText('sharePayload')">Copy PowerShell → WSL installer</button><button onclick="downloadText('sharePayload','powershell-wsl-installer')">Download as text file</button><button onclick="downloadRunner('sharePayload','powershell-wsl-installer','ps1')">Download self-deleting runner</button></div>
    <p class="muted">Bash installer for Linux/macOS/WSL bash terminals. Syncs `~/.codex/auth.json` plus existing `~/.hermes` auth stores and prints safe fingerprints only:</p>
    <textarea id="sharePayloadBash" readonly></textarea>
    <div class="actions"><button onclick="copyText('sharePayloadBash')">Copy bash installer</button><button onclick="downloadText('sharePayloadBash','bash-installer')">Download as text file</button><button onclick="downloadRunner('sharePayloadBash','bash-installer','command')">Download self-deleting runner</button></div>
    <p class="muted">Native Windows Codex installer, only if Codex runs outside WSL:</p>
    <textarea id="sharePayloadWindows" readonly></textarea>
    <div class="actions"><button onclick="copyText('sharePayloadWindows')">Copy native Windows installer</button><button onclick="downloadText('sharePayloadWindows','native-windows-installer')">Download as text file</button><button onclick="downloadRunner('sharePayloadWindows','native-windows-installer','ps1')">Download self-deleting runner</button></div>
    <p class="muted" id="shareBundle"></p>
  </div>
</div>
<script>
let expected = [];
let latestMachineUsageRows = [];
let latestAnthropicUsage = null;
let latestGrokUsage = null;
let currentShareAlias = '';
let currentShareLabel = '';
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
function usageLevel(primary, secondary, connected=true){
  if(!connected) return 'warn';
  const maxUsed = Math.max(Number(primary?.used_percent||0), Number(secondary?.used_percent||0));
  const soonestReset = Math.min(...[primary?.resets_in_seconds, secondary?.resets_in_seconds].filter(v=>Number(v)>0));
  if(Number(secondary?.used_percent||0) >= 99) return 'bad';
  if(maxUsed >= 90 || Number(primary?.used_percent||0) >= 88) return 'bad';
  if(maxUsed >= 70 || (Number.isFinite(soonestReset) && soonestReset < 45*60 && maxUsed >= 55)) return 'warn';
  return 'ok';
}
function levelLabel(level, connected=true, primary=null, secondary=null){
  if(!connected) return '⚠ Needs auth';
  if(Number(secondary?.used_percent||0) >= 99) return '⛔ Rate limit warning';
  if(Number(primary?.used_percent||0) >= 60) return '⚡ 5h burn high';
  return level === 'bad' ? '🚨 Switch soon' : level === 'warn' ? '⚡ Watch usage' : '✓ Healthy';
}
function resetText(win){
  if(!win || !win.resets_at) return 'reset unknown';
  const hrs = Math.max(0, (win.resets_in_seconds||0)/3600);
  const rel = hrs < 1 ? `${Math.round(hrs*60)}m` : `${hrs.toFixed(1)}h`;
  return `${rel} to reset`;
}
function sparkline(primary, secondary, level){
  const a = pct(primary?.used_percent||0), b = pct(secondary?.used_percent||0);
  const mid = Math.max(8, Math.min(25, 28 - ((a+b)/2)*.18));
  const end = Math.max(4, 28 - Math.max(a,b)*.24);
  return `<svg class="sparkline ${level}" viewBox="0 0 120 30" aria-hidden="true"><path d="M2 25 C18 ${mid+3}, 32 ${mid}, 46 ${24-a*.18} S78 ${26-b*.18}, 94 ${end+4} S112 ${end}, 118 ${end+1}"/></svg>`;
}
function bar(label, win){
  if(!win) return `<div class="muted">${label}: no data</div>`;
  const p=pct(win.used_percent), cls=p>=90?'bad':p>=70?'warn':'ok';
  return `<div class="meter"><div class="row"><span>${label}</span><span class="${cls}">${p.toFixed(1)}% used · ${(100-p).toFixed(1)}% left</span></div><div class="bar"><div class="fill ${cls}" style="width:${p}%"></div></div>${resetLine(label, win)}</div>`;
}
function freshWeeklyResetNote(secondary){
  if(!secondary) return '';
  const used = Number(secondary.used_percent || 0);
  const resetSeconds = Number(secondary.resets_in_seconds || 0);
  if(used <= 10 && resetSeconds >= 6*24*3600){
    return `Weekly window looks freshly reset: ${used.toFixed(1)}% used · ${resetText(secondary)}.`;
  }
  return '';
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
async function saveAccountDetails(alias){
  const label = document.getElementById(`name-${alias}`).value;
  const plan_label = document.getElementById(`plan-${alias}`).value;
  const plan_details = document.getElementById(`plan-details-${alias}`).value;
  const codex_spark_access = !!document.getElementById(`spark-access-${alias}`)?.checked;
  const res = await api('/api/accounts/details', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({alias,label,plan_label,plan_details,codex_spark_access})});
  if(!res.ok) alert(res.error || 'Save account details failed');
  await refreshAll();
}
function slugifyAlias(label){
  return String(label||'').toLowerCase().replace(/[^a-z0-9._-]+/g,'-').replace(/^-+|-+$/g,'').slice(0,64);
}
function addAuthRequirements(provider){
  const home = '~';
  if(provider === 'anthropic') return `Anthropic requirements:\n- Paste a full Anthropic API key here (normally starts sk-ant-).\n- The key is saved only to ${home}/.codex-switch/anthropic-accounts.json with chmod 600.\n- Normal key validation uses GET /v1/models.\n- Usage/cost requires Anthropic Admin Usage & Cost API access; valid normal keys may still show admin blocked.\n- This does not touch Codex OAuth or Hermes openai-codex auth.`;
  if(provider === 'grok') return `Grok / xAI requirements:\n- Paste a full xAI API key here (normally starts xai- or sk-).\n- The key is saved only to ${home}/.codex-switch/grok-accounts.json with chmod 600.\n- Validation uses https://api.x.ai/v1/models.\n- xAI public usage/spend/credit endpoints are not wired because no public usage endpoint was available when implemented.\n- This does not touch Codex OAuth or Hermes openai-codex auth.`;
  return `OpenAI Codex requirements:\n- Uses the existing Codex device-code login flow; no API key is pasted here.\n- Creates/updates a dashboard slot, then opens /auth/<alias>.\n- Auth is saved under ${home}/.codex-switch/profiles/<alias>/auth.json by codex-switch-secure.\n- Existing Share Auth behavior remains unchanged: it can sync Codex CLI plus Hermes openai-codex stores when explicitly clicked.`;
}
function updateAddAuthRequirements(){
  const provider = document.getElementById('newProvider')?.value || 'codex';
  const keyWrap = document.getElementById('newApiKeyWrap');
  const req = document.getElementById('addAuthRequirements');
  if(keyWrap) keyWrap.style.display = provider === 'codex' ? 'none' : 'block';
  if(req) req.innerText = addAuthRequirements(provider);
}
async function addAccount(){
  const providerEl = document.getElementById('newProvider');
  const labelEl = document.getElementById('newLabel');
  const aliasEl = document.getElementById('newAlias');
  const keyEl = document.getElementById('newApiKey');
  const statusEl = document.getElementById('addAccountStatus');
  const provider = providerEl?.value || 'codex';
  const label = labelEl.value.trim();
  let alias = aliasEl.value.trim() || slugifyAlias(label);
  if(!label){ alert('Enter a display name first.'); return; }
  if(!alias){ alert('Enter an alias.'); return; }
  if(!/^[A-Za-z0-9._-]{1,64}$/.test(alias)){ alert('Alias can only use letters, numbers, dot, underscore, and dash.'); return; }
  if(provider !== 'codex'){
    const api_key = keyEl.value.trim();
    if(!api_key){ alert(`Paste the ${provider === 'anthropic' ? 'Anthropic' : 'Grok/xAI'} API key first.`); return; }
    statusEl.innerText = `Saving ${provider} key for ${label}…`;
    const res = await api('/api/provider-keys/add', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({provider,alias,label,api_key})});
    if(!res.ok){ alert(res.error || 'Add provider key failed'); statusEl.innerText = ''; return; }
    labelEl.value = ''; aliasEl.value = ''; keyEl.value = '';
    statusEl.innerText = `Saved ${provider} auth for ${label}. Key fp: ${res.key_fingerprint}. Refreshing validation…`;
    if(provider === 'anthropic') await loadAnthropicUsage(true);
    if(provider === 'grok') await loadGrokUsage(true);
    return;
  }
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
function safeDownloadName(part){
  return String(part || '').toLowerCase().replace(/[^a-z0-9._-]+/g,'-').replace(/^-+|-+$/g,'').slice(0,80) || 'share-auth';
}
function downloadBlobText(text, filename, mime='text/plain;charset=utf-8'){
  const blob = new Blob([text], {type:mime});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
function shareDownloadBaseName(label){
  const alias = safeDownloadName(currentShareAlias || 'account');
  const model = safeDownloadName(selectedShareDefaultModel() || 'model-unchanged');
  return `codex-share-auth-${alias}-${safeDownloadName(label)}-${model}`;
}
function downloadText(id, label){
  const el = document.getElementById(id);
  if(!el){ alert('Nothing to download yet.'); return; }
  const text = el.value || '';
  if(!text.trim()){ alert('Generate the Share Auth command first.'); return; }
  downloadBlobText(text, `${shareDownloadBaseName(label)}.txt`);
}
function buildSelfDeletingRunner(rawText, kind){
  if(kind === 'ps1'){
    return `$ErrorActionPreference = "Stop"\n$__codexShareAuthExit = 0\ntry {\n${rawText}\n  if ($LASTEXITCODE -ne $null) { $__codexShareAuthExit = $LASTEXITCODE }\n}\ncatch {\n  Write-Error $_\n  $__codexShareAuthExit = 1\n}\nfinally {\n  if ($PSCommandPath) {\n    try { Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue } catch {}\n  }\n}\nexit $__codexShareAuthExit\n`;
  }
  return `#!/usr/bin/env bash\nset -euo pipefail\n__codex_share_auth_self="${'${BASH_SOURCE[0]:-$0}'}"\ncleanup_codex_share_auth(){ /bin/rm -f -- "$__codex_share_auth_self" 2>/dev/null || true; }\ntrap cleanup_codex_share_auth EXIT\n${rawText}\n`;
}
function downloadRunner(id, label, kind){
  const el = document.getElementById(id);
  if(!el){ alert('Nothing to download yet.'); return; }
  const text = el.value || '';
  if(!text.trim()){ alert('Generate the Share Auth command first.'); return; }
  const ext = kind === 'ps1' ? 'ps1' : 'command';
  const filename = `${shareDownloadBaseName(label)}-self-deleting.${ext}`;
  downloadBlobText(buildSelfDeletingRunner(text, kind), filename, kind === 'ps1' ? 'text/plain;charset=utf-8' : 'application/x-sh;charset=utf-8');
}
function selectedShareDefaultModel(){
  const sel = document.getElementById('shareDefaultModel');
  const custom = document.getElementById('shareCustomModel');
  if(!sel) return '';
  return sel.value === 'custom' ? (custom?.value || '').trim() : sel.value;
}
let shareRegenTimer = null;
function debouncedRegenerateShareAuth(){ clearTimeout(shareRegenTimer); shareRegenTimer = setTimeout(regenerateShareAuth, 350); }
async function regenerateShareAuth(){
  if(!currentShareAlias) return;
  const customWrap = document.getElementById('shareCustomModelWrap');
  const sel = document.getElementById('shareDefaultModel');
  if(customWrap && sel) customWrap.style.display = sel.value === 'custom' ? 'block' : 'none';
  const default_model = selectedShareDefaultModel();
  const res = await api('/api/accounts/share-auth', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({alias: currentShareAlias, default_model})});
  if(!res.ok){ alert(res.error || 'Share auth failed'); return; }
  document.getElementById('shareLocal').value = `${res.same_machine_switch_command}
# If the target local agent only reads ~/.codex/auth.json, use:
${res.same_machine_install_command}`;
  document.getElementById('sharePayload').value = res.target_machine_powershell_wsl_install_command || res.target_machine_payload_install_command;
  document.getElementById('sharePayloadBash').value = res.target_machine_bash_install_command || '';
  document.getElementById('sharePayloadWindows').value = res.target_machine_powershell_windows_install_command || '';
  document.getElementById('shareBundle').innerText = `Local bundle written: ${res.bundle_path}
${res.target_machine_import_command_note}`;
  document.getElementById('shareModelNote').innerText = res.default_model_note || (default_model ? `Generated installers will set default model to ${default_model}.` : 'Generated installers leave default model unchanged.');
}
async function shareAuth(alias, label){
  currentShareAlias = alias;
  currentShareLabel = label || alias;
  document.getElementById('shareTitle').innerText = `Share auth: ${currentShareLabel}`;
  const syncResult = document.getElementById('shareSyncResult');
  syncResult.style.display = 'none';
  syncResult.innerText = '';
  document.getElementById('shareModal').classList.add('open');
  await regenerateShareAuth();
}
async function syncLocalAuth(){
  if(!currentShareAlias){ alert('Open Share Auth for an account first.'); return; }
  const default_model = selectedShareDefaultModel();
  const modelText = default_model ? ` and set default model to ${default_model}` : '';
  if(!confirm(`Apply ${currentShareAlias} to ~/.codex/auth.json and existing Hermes auth stores on this machine${modelText}? This does not restart live Hermes gateways.`)) return;
  const btn = document.getElementById('syncLocalAuthButton');
  const out = document.getElementById('shareSyncResult');
  btn.disabled = true;
  out.style.display = 'block';
  out.innerText = 'Syncing local Codex + Hermes auth stores…';
  const res = await api('/api/accounts/sync-auth', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({alias: currentShareAlias, default_model})});
  btn.disabled = false;
  if(!res.ok){ out.innerText = `Sync failed: ${res.error || 'unknown error'}`; return; }
  const targets = (res.hermes_targets||[]).map(t => `- ${t.path} · access ${t.access_fingerprint} · refresh ${t.refresh_fingerprint} · pool entries ${t.pool_entries_updated}`).join('\n');
  const modelLine = res.default_model?.requested ? `\nDefault model: ${res.default_model.model} · Codex config ${res.default_model.codex_config_updated} · Hermes configs ${(res.default_model.hermes_configs_updated||[]).length}` : '\nDefault model: unchanged';
  out.innerText = `Synced without printing secrets.\nCodex: ${res.codex_target}\nPayload fp: ${res.fingerprints?.payload}\nAccess fp: ${res.fingerprints?.access_token}\nRefresh fp: ${res.fingerprints?.refresh_token}${modelLine}\nHermes stores:\n${targets || '- none found'}\n\n${res.restart_note}`;
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
function renderAnthropicApiDetails(){
  const el = document.getElementById('anthropicApiDetails');
  if(!el) return;
  if(!latestAnthropicUsage){
    el.innerText = 'Anthropic API status has not loaded yet.';
    return;
  }
  const rows = latestAnthropicUsage.rows || [];
  const details = {
    endpoint: '/api/anthropic-usage',
    config_path: latestAnthropicUsage.config_path,
    source: latestAnthropicUsage.source,
    upstream_api_checks: [
      'GET https://api.anthropic.com/v1/models',
      'GET https://api.anthropic.com/v1/organizations/usage_report/messages',
      'GET https://api.anthropic.com/v1/organizations/cost_report'
    ],
    window: { start_1d: latestAnthropicUsage.start_1d, start_7d: latestAnthropicUsage.start_7d, ending_at: latestAnthropicUsage.ending_at },
    accounts: rows.map(r => ({
      alias: r.alias,
      label: r.label,
      key_fingerprint: r.key_fingerprint,
      key_valid: r.key_valid,
      admin_usage_readable: r.ok,
      models_count: r.models_count,
      usage_1d_ok: r.usage_1d_ok,
      usage_7d_ok: r.usage_7d_ok,
      cost_7d_ok: r.cost_7d_ok,
      note: r.credit_note
    }))
  };
  el.innerText = JSON.stringify(details, null, 2);
}
function toggleAnthropicApi(){
  const el = document.getElementById('anthropicApiDetails');
  if(!el) return;
  const willShow = el.style.display === 'none' || !el.style.display;
  if(willShow) renderAnthropicApiDetails();
  el.style.display = willShow ? 'block' : 'none';
}
async function loadAnthropicUsage(force=false){
  const summary = document.getElementById('anthropicUsageSummary');
  const table = document.getElementById('anthropicUsageRows');
  summary.innerHTML = '<div class="muted">Loading Anthropic usage…</div>';
  const data = await api('/api/anthropic-usage');
  latestAnthropicUsage = data;
  const apiDetails = document.getElementById('anthropicApiDetails');
  if(apiDetails && apiDetails.style.display !== 'none' && apiDetails.style.display) renderAnthropicApiDetails();
  if(!data.ok){ summary.innerHTML = `<div class="bad">${escapeHtml(data.error||'Anthropic usage load failed')}</div>`; table.innerHTML=''; return; }
  const rows = data.rows || [];
  if(!rows.length){ summary.innerHTML = `<div class="muted">No Anthropic accounts configured. Add local accounts to ${escapeHtml(data.config_path||'~/.codex-switch/anthropic-accounts.json')}.</div>`; table.innerHTML=''; return; }
  const okRows = rows.filter(r=>r.ok);
  const total7d = rows.reduce((a,r)=>a+Number(r.total_tokens_7d||0),0);
  const cost7d = rows.reduce((a,r)=>a+Number(r.cost_usd_7d||0),0);
  summary.innerHTML = `<div><span class="summary-number">${total7d.toLocaleString()}</span> Anthropic tokens / 7d · $${cost7d.toFixed(4)} / 7d</div><div class="muted">${okRows.length}/${rows.length} accounts readable · ${escapeHtml(data.start_7d||'')} → ${escapeHtml(data.ending_at||'')} · config: ${escapeHtml(data.config_path||'')}</div>`;
  table.innerHTML = `<table class="token-table"><thead><tr><th>Account</th><th>Status</th><th>24h tokens</th><th>7d tokens</th><th>7d cost</th><th>Credit/balance</th><th>Key fp</th><th>Notes</th></tr></thead><tbody>${rows.map(r=>{ const errs=(r.errors||[]).map(e=>`${e.source} ${e.status||''}: ${String(e.error||'').slice(0,180)}`).join(' | '); const statusText=r.ok?'admin readable':(r.key_valid?'valid key · admin blocked':'blocked'); const statusClass=r.ok?'ok':(r.key_valid?'warn':'bad'); return `<tr><td>${escapeHtml(r.label||r.alias)}</td><td class="${statusClass}">${statusText}</td><td>${Number(r.total_tokens_1d||0).toLocaleString()}</td><td>${Number(r.total_tokens_7d||0).toLocaleString()}</td><td>${r.cost_usd_7d==null?'unknown':('$'+Number(r.cost_usd_7d||0).toFixed(4))}</td><td>${escapeHtml(r.credit_note||'unknown')}</td><td>${escapeHtml(r.key_fingerprint||'')}</td><td class="muted">${escapeHtml(r.error||errs||'')}</td></tr>`; }).join('')}</tbody></table>`;
}
function renderGrokApiDetails(){
  const el = document.getElementById('grokApiDetails');
  if(!el) return;
  if(!latestGrokUsage){ el.innerText = 'Grok/xAI API status has not loaded yet.'; return; }
  const rows = latestGrokUsage.rows || [];
  const details = {
    endpoint: '/api/grok-usage',
    config_path: latestGrokUsage.config_path,
    source: latestGrokUsage.source,
    upstream_api_checks: ['GET https://api.x.ai/v1/models'],
    checked_at: latestGrokUsage.checked_at,
    accounts: rows.map(r => ({
      alias: r.alias,
      label: r.label,
      key_fingerprint: r.key_fingerprint,
      key_valid: r.key_valid,
      models_count: r.models_count,
      usage_note: r.usage_note || r.error
    }))
  };
  el.innerText = JSON.stringify(details, null, 2);
}
function toggleGrokApi(){
  const el = document.getElementById('grokApiDetails');
  if(!el) return;
  const willShow = el.style.display === 'none' || !el.style.display;
  if(willShow) renderGrokApiDetails();
  el.style.display = willShow ? 'block' : 'none';
}
async function loadGrokUsage(force=false){
  const summary = document.getElementById('grokUsageSummary');
  const table = document.getElementById('grokUsageRows');
  summary.innerHTML = '<div class="muted">Loading Grok/xAI auth status…</div>';
  const data = await api('/api/grok-usage');
  latestGrokUsage = data;
  const apiDetails = document.getElementById('grokApiDetails');
  if(apiDetails && apiDetails.style.display !== 'none' && apiDetails.style.display) renderGrokApiDetails();
  if(!data.ok){ summary.innerHTML = `<div class="bad">${escapeHtml(data.error||'Grok usage load failed')}</div>`; table.innerHTML=''; return; }
  const rows = data.rows || [];
  if(!rows.length){ summary.innerHTML = `<div class="muted">No Grok/xAI accounts configured. Add local accounts to ${escapeHtml(data.config_path||'~/.codex-switch/grok-accounts.json')}.</div>`; table.innerHTML=''; return; }
  const validRows = rows.filter(r=>r.key_valid);
  summary.innerHTML = `<div><span class="summary-number">${validRows.length}</span> valid Grok/xAI key${validRows.length===1?'':'s'}</div><div class="muted">${validRows.length}/${rows.length} accounts valid · checked: ${escapeHtml(data.checked_at||'')} · config: ${escapeHtml(data.config_path||'')}</div>`;
  table.innerHTML = `<table class="token-table"><thead><tr><th>Account</th><th>Status</th><th>Models</th><th>Key fp</th><th>Usage / credit</th><th>Notes</th></tr></thead><tbody>${rows.map(r=>{ const errs=(r.errors||[]).map(e=>`${e.source} ${e.status||''}: ${String(e.error||'').slice(0,180)}`).join(' | '); return `<tr><td>${escapeHtml(r.label||r.alias)}</td><td class="${r.key_valid?'ok':'bad'}">${r.key_valid?'valid':'blocked'}</td><td>${Number(r.models_count||0).toLocaleString()}</td><td>${escapeHtml(r.key_fingerprint||'')}</td><td>${escapeHtml(r.usage_note||'usage/credit unavailable')}</td><td class="muted">${escapeHtml(r.error||errs||'')}</td></tr>`; }).join('')}</tbody></table>`;
}
async function saveMachineSheetUrl(){
  const url = document.getElementById('machineSheetUrl').value.trim();
  const res = await api('/api/machine-usage/source', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({url})});
  if(!res.ok){ alert(res.error || 'Save machine sheet URL failed'); return; }
  await loadMachineUsage(true);
}
async function saveMachineDetails(agent, machine){
  const id = btoa(unescape(encodeURIComponent(`${agent}|||${machine}`))).replace(/[^A-Za-z0-9_-]/g,'');
  const display_machine = document.getElementById(`machine-name-${id}`)?.value || '';
  const machine_details = document.getElementById(`machine-details-${id}`)?.value || '';
  const res = await api('/api/machine-usage/details', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({agent,machine,display_machine,machine_details})});
  if(!res.ok){ alert(res.error || 'Save machine details failed'); return; }
  await loadMachineUsage(true);
}
function machineDomId(r){ return btoa(unescape(encodeURIComponent(`${r.agent||''}|||${r.machine||''}`))).replace(/[^A-Za-z0-9_-]/g,''); }
async function loadMachineUsage(force=false){
  const data = await api(`/api/machine-usage${force?'?force=1':''}`);
  if(data.sheet_csv_url) document.getElementById('machineSheetUrl').value = data.sheet_csv_url;
  renderSharefilePrompt();
  const summary = document.getElementById('machineUsageSummary');
  const table = document.getElementById('machineUsageRows');
  if(!data.ok){ summary.innerHTML = `<div class="bad">${escapeHtml(data.error||'Machine usage load failed')}</div>`; table.innerHTML=''; return; }
  if(!data.configured){ summary.innerHTML = `<div class="muted">${escapeHtml(data.message||'Set a Google Sheet CSV URL first.')}</div>`; table.innerHTML=''; return; }
  summary.innerHTML = `<div><span class="summary-number">${Number(data.totals?.tokens_30m||0).toLocaleString()}</span> tokens / 30m · ${Number(data.totals?.tokens_1h||0).toLocaleString()} / 1h · ${Number(data.totals?.tokens_2h||0).toLocaleString()} / 2h · ${Number(data.totals?.tokens_3h||0).toLocaleString()} / 3h</div><div class="muted">24h: ${Number(data.totals?.tokens_24h||0).toLocaleString()} · 7d: ${Number(data.totals?.tokens_7d||0).toLocaleString()} · ${Number(data.row_count||0)} machines · fetched: ${escapeHtml(data.fetched_at||'')} ${data.cache_hit?'· cached':''}</div>`;
  latestMachineUsageRows = data.rows || [];
  renderMachineUsageTable();
}
function renderMachineUsageTable(){
  const table = document.getElementById('machineUsageRows');
  if(!table) return;
  const filter = String(document.getElementById('machineUsageFilter')?.value || '').toLowerCase().trim();
  let rows = latestMachineUsageRows || [];
  if(filter){
    rows = rows.filter(r => [r.status,r.agent,r.machine,r.os,r.current_account_alias,r.last_used_at,r.generated_at].some(v => String(v||'').toLowerCase().includes(filter)));
  }
  if(!rows.length){ table.innerHTML = '<div class="muted">No machine rows match the current filter.</div>'; return; }
  table.innerHTML = `<table class="token-table"><thead><tr><th>Status</th><th>Agent</th><th>Machine / details</th><th>Account</th><th>30m</th><th>1h</th><th>2h</th><th>3h</th><th>24h</th><th>7d</th><th>Last used</th><th>Heartbeat age</th><th>Rate limits</th></tr></thead><tbody>${rows.map(r=>{ const id=machineDomId(r); return `<tr><td class="${escapeHtml(r.status||'')}">●</td><td>${escapeHtml(r.agent)}</td><td><div class="muted">raw: ${escapeHtml(r.machine)} · ${escapeHtml(r.os||'')}</div><input id="machine-name-${id}" value="${escapeHtml(r.display_machine||r.machine||'')}" maxlength="160" /><textarea class="compact" id="machine-details-${id}" placeholder="Machine details / owner / location / notes" maxlength="500">${escapeHtml(r.machine_details||'')}</textarea><div class="actions"><button onclick='saveMachineDetails(${JSON.stringify(r.agent||'')}, ${JSON.stringify(r.machine||'')})'>Save machine</button></div></td><td>${escapeHtml(r.current_account_alias||'')}</td><td>${Number(r.tokens_30m||0).toLocaleString()}</td><td>${Number(r.tokens_1h||0).toLocaleString()}</td><td>${Number(r.tokens_2h||0).toLocaleString()}</td><td>${Number(r.tokens_3h||0).toLocaleString()}</td><td>${Number(r.tokens_24h||0).toLocaleString()}</td><td>${Number(r.tokens_7d||0).toLocaleString()}</td><td>${escapeHtml(r.last_used_at||'')}</td><td>${r.age_minutes==null?'unknown':`${r.age_minutes}m`}</td><td>${Number(r.rate_limit_errors_24h||0).toLocaleString()}</td></tr>`; }).join('')}</tbody></table>`;
}

function getSparkUsage(profile){
  const usage = profile?.usage || {};
  return usage?.spark_usage || usage?.codex_spark || null;
}

function resolveUsageSource(profile, preferSpark=false){
  const usage = profile?.usage || {};
  if(!preferSpark) return usage;
  const spark = getSparkUsage(profile);
  if (spark && (spark.primary || spark.secondary)) {
    return spark;
  }
  // Spark cards must not silently fall back to the normal Codex/GPT usage windows.
  // If the API did not return an additional Spark allowance, show that explicitly
  // instead of making a pro/Spark slot look like a free weekly-only card.
  return {primary:null, secondary:null, spark_missing:true};
}

function isCodexSparkAccount(expected, profile){
  const explicit = expected?.codex_spark_access;
  if (typeof explicit === 'boolean') return explicit;
  if (String(explicit || '').trim()) return /^(1|true|yes|on|y|t)$/i.test(String(explicit));

  const fields = [
    expected?.plan_label,
    expected?.expected_plan,
    profile?.account?.plan,
    expected?.plan_details,
  ];
  const haystack = fields.map(v => String(v || '').toLowerCase()).join(' ');
  return /\bcodex\s*spark\b/.test(haystack) || /\bspark\b/.test(haystack);
}

function usageCardData(expected, profile, showSparkUsage=false){
  const usage = resolveUsageSource(profile, showSparkUsage);
  const account = profile?.account || {};
  const connected = !!profile;
  const level = usageLevel(usage.primary, usage.secondary, connected);
  const status = connected ? 'Connected' : 'Not connected';
  const primaryPct = pct(usage.primary?.used_percent || 0);
  const secondaryPct = pct(usage.secondary?.used_percent || 0);
  const maxPct = Math.max(primaryPct, secondaryPct);
  const leftPct = Math.max(0, 100 - maxPct);
  const current = (profile?.is_current) ? '<span class="pill ok">active now</span>' : '';
  const rateLimitBlocked = Number(usage.secondary?.used_percent || 0) >= 99;
  const planLabel = expected?.plan_label || expected?.expected_plan || account?.plan || '';
  const expectedPlan = String(expected?.plan_label || expected?.expected_plan || '').trim().toLowerCase();
  const apiPlan = String(account?.plan || '').trim().toLowerCase();
  const entitlementMismatch = !!(expectedPlan && apiPlan && expectedPlan !== apiPlan);
  const sparkMissing = !!usage?.spark_missing;
  const warning = sparkMissing
    ? 'Codex Spark access is marked locally, but the live usage API did not return a Spark allowance for this auth. Re-authorize or check the OpenAI entitlement; the normal usage card may still show standard/free limits.'
    : (entitlementMismatch
        ? `Entitlement mismatch: dashboard expects ${expectedPlan}, but live OpenAI auth/API reports ${apiPlan}. Treat this auth as ${apiPlan} until re-authorized or fixed upstream.`
        : (rateLimitBlocked
            ? '7-day usage is 100% used. This is a rate limit warning: 5h usage may be unavailable or irrelevant until the weekly window resets.'
            : (level === 'bad'
                ? 'High burn: switch or let this account cool down before the next heavy run.'
                : (level === 'warn'
                    ? 'Approaching warning zone: keep an eye on reset timers before launching big jobs.'
                    : 'Usage is below warning threshold.'))));
  const isFreePlan = String(planLabel || '').trim().toLowerCase() === 'free' || (!expectedPlan && apiPlan === 'free');

  return {
    expected,
    profile,
    usage,
    account,
    level,
    status,
    primaryPct,
    secondaryPct,
    maxPct,
    leftPct,
    current,
    warning,
    planLabel,
    isFreePlan,
    connected,
    sparkLabel: usage?.limit_name || usage?.metered_feature || '',
    weeklyResetNote: freshWeeklyResetNote(usage.secondary),
    entitlementMismatch,
    expectedPlan,
    apiPlan,
    sparkMissing,
    };
}

    function renderUsageBars(primary, secondary, isFreePlan){
    if(!primary && isFreePlan && secondary){
    return `<div class="muted">Free plan: no 5h window; showing 7d usage only.</div>${bar('7d / weekly', secondary)}`;
    }
    return `${bar('5h', primary)}${bar('7d / weekly', secondary)}`;
    }

    function renderWindowMetrics(maxPct, leftPct, primary, secondary, isFreePlan){
      const fiveHourUsed = primary ? pct(primary.used_percent).toFixed(0) : '—';
      const weeklyUsed = secondary ? pct(secondary.used_percent).toFixed(0) : '—';
      const weeklyLeft = secondary ? (100 - pct(secondary.used_percent)).toFixed(0) : '—';
      if(!primary && isFreePlan && secondary){
        return `<div class="mini-metric"><b>${weeklyUsed}%</b><span>weekly used</span></div>` +
          `<div class="mini-metric"><b>${weeklyLeft}%</b><span>weekly left</span></div>` +
          `<div class="mini-metric"><b>${resetText(secondary)}</b><span>weekly reset</span></div>`;
      }
      return `<div class="mini-metric"><b>${fiveHourUsed}${fiveHourUsed==='—'?'':'%'}</b><span>5h used</span></div>` +
        `<div class="mini-metric"><b>${weeklyUsed}${weeklyUsed==='—'?'':'%'}</b><span>weekly used</span></div>` +
        `<div class="mini-metric"><b>${resetText(secondary)}</b><span>weekly reset</span></div>`;
    }

    function escapeJsString(value){
      return String(value || '').replace(/\\/g, '\\').replace(/"/g, '\\"').replace(/\n/g, '\\\\n').replace(/\r/g, '');

}

function renderAccountCard(expected, profile){
  const d = usageCardData(expected, profile);
  const alias = escapeHtml(d.expected?.alias || '');
  return `<article class="card usage-card is-${d.level}" data-alias="${alias}" title="Drag left/right to reorder"><div class="usage-card-inner"><div class="drag-handle">↔ drag to reorder</div><div class="row"><div><div class="alias">${escapeHtml(d.expected?.label || '')}</div><div class="muted">alias: ${alias}</div></div><span class="pill status-badge ${d.level}">${levelLabel(d.level, d.connected, d.usage.primary, d.usage.secondary)}</span></div>
      <div class="usage-visual"><div class="usage-ring ${d.level}" style="--p:${d.maxPct}"><div class="ring-text">${d.maxPct.toFixed(0)}%<span>max used</span></div></div><div class="usage-meta"><div class="row"><span class="muted">${escapeHtml(d.status)}</span>${d.current}${d.entitlementMismatch ? '<span class="pill bad">auth says '+escapeHtml(d.apiPlan || 'unknown')+'</span>' : ''}</div>${sparkline(d.usage.primary, d.usage.secondary, d.level)}<div class="muted">Plan: ${escapeHtml(d.planLabel || 'unknown')}${d.apiPlan ? ` · live: ${escapeHtml(d.apiPlan)}` : ''}</div>${d.expected?.plan_details ? `<div class="muted">${escapeHtml(d.expected.plan_details)}</div>` : ''}</div></div>
      <div class="metric-strip">${renderWindowMetrics(d.maxPct, d.leftPct, d.usage.primary, d.usage.secondary, d.isFreePlan)}</div>
      ${d.weeklyResetNote ? `<div class="muted">${escapeHtml(d.weeklyResetNote)}</div>` : ''}
      <div class="warn-callout"><b>${d.level==='bad' ? '🚨' : '⚠'}</b><div>${escapeHtml(d.warning)}</div></div>
      <label class="muted inline-label">Display name</label><input id="name-${alias}" value="${escapeHtml(d.expected?.label || '')}" maxlength="120" />
      <label class="muted inline-label">Plan name</label><input id="plan-${alias}" value="${escapeHtml(d.planLabel)}" placeholder="e.g. Team / Pro / Codex Spark" maxlength="120" />
      <label class="muted inline-label">Plan details</label><textarea class="compact" id="plan-details-${alias}" placeholder="Plan notes, limits, owner, reset notes…" maxlength="500">${escapeHtml(d.expected?.plan_details || '')}</textarea>
      <label class="muted inline-label"><input id="spark-access-${alias}" type="checkbox" ${d.expected?.codex_spark_access ? 'checked' : ''}/> Grant this account Codex Spark usage access</label>
      <div class="actions"><button onclick="saveAccountDetails('${escapeJsString(d.expected?.alias || '')}')">Save account details</button></div>
      ${renderUsageBars(d.usage.primary, d.usage.secondary, d.isFreePlan)}
      <div class="actions"><a class="btn primary" href="/auth/${encodeURIComponent(d.expected?.alias || '')}">Auth</a><button onclick='shareAuth(${JSON.stringify(d.expected?.alias || '')}, ${JSON.stringify(d.expected?.label || d.expected?.alias || '')})'>Share Auth</button><button onclick="refreshAll()">Refresh</button><button onclick='removeAccount(${JSON.stringify(d.expected?.alias || '')}, ${JSON.stringify(d.expected?.label || d.expected?.alias || '')})'>Remove</button></div></div></article>`;
}

function renderCodexSparkUsageCard(expected, profile){
  const d = usageCardData(expected, profile, true);
  return `<article class="card usage-card is-${d.level}" data-alias="${escapeHtml(d.expected?.alias || '')}"><div class="usage-card-inner"><div class="row"><div><div class="alias">${escapeHtml(d.expected?.label || '')}</div><div class="muted">alias: ${escapeHtml(d.expected?.alias || '')}</div></div><span class="pill status-badge ${d.level}">${levelLabel(d.level, d.connected, d.usage.primary, d.usage.secondary)}</span></div>
      <div class="usage-visual"><div class="usage-ring ${d.level}" style="--p:${d.maxPct}"><div class="ring-text">${d.maxPct.toFixed(0)}%<span>max used</span></div></div><div class="usage-meta"><div class="row"><span class="muted">${escapeHtml(d.status)}</span>${d.current}${d.sparkMissing ? '<span class="pill bad">no Spark allowance from API</span>' : ''}</div>${d.sparkLabel ? `<div class="muted">Allowance: ${escapeHtml(d.sparkLabel)}</div>` : ''}<div class="muted">Plan: ${escapeHtml(d.planLabel || 'unknown')}${d.apiPlan ? ` · live: ${escapeHtml(d.apiPlan)}` : ''}</div>${d.expected?.plan_details ? `<div class="muted">${escapeHtml(d.expected.plan_details)}</div>` : ''}</div></div>
      <div class="metric-strip">${renderWindowMetrics(d.maxPct, d.leftPct, d.usage.primary, d.usage.secondary, d.isFreePlan)}</div>
      ${d.weeklyResetNote ? `<div class="muted">${escapeHtml(d.weeklyResetNote)}</div>` : ''}
      ${d.sparkMissing ? `<div class="warn-callout"><b>⚠</b><div>${escapeHtml(d.warning)}</div></div>` : ''}
      ${renderUsageBars(d.usage.primary, d.usage.secondary, d.isFreePlan)}
    </div></article>`;
}


async function refreshAll(){
  const data = await api('/api/accounts');
  expected = data.accounts_config || expected || [];
  renderScheduler(data.refresh_state);
  const profiles = data.profiles || [];
  const byAlias = Object.fromEntries(profiles.map(p=>[p.alias,p]));
  const merged = [...expected.map(e=>({expected:e, profile:byAlias[e.alias]})), ...profiles.filter(p=>!expected.find(e=>e.alias===p.alias)).map(p=>({expected:{alias:p.alias,label:p.alias,expected_plan:''}, profile:p}))];
    const codexSparkRows = merged.filter(({expected, profile}) => isCodexSparkAccount(expected, profile));
  document.getElementById('cards').innerHTML = merged.map(({expected, profile}) => renderAccountCard(expected, profile)).join('');
  const sparkSection = document.getElementById('codexSparkCardsSection');
  const sparkSectionSummary = document.getElementById('codexSparkSummary');
  const sparkCards = document.getElementById('codexSparkCards');
  const sparkCount = codexSparkRows.length;
  if(sparkSection && sparkSectionSummary && sparkCards){
    sparkSection.style.display = sparkCount ? 'block' : 'none';
    sparkSectionSummary.innerText = sparkCount
      ? `${sparkCount} account${sparkCount===1?'':'s'} with Codex Spark access`
      : 'No accounts currently marked for Codex Spark access.';
    sparkCards.innerHTML = sparkCount
      ? codexSparkRows.map(({expected, profile}) => renderCodexSparkUsageCard(expected, profile)).join('')
      : '';
  }
  setupDragReorder();
  renderAuthLinks();
}
function renderAuthLinks(){
  document.getElementById('authLinks').innerHTML = expected.map(e=>`<a class="btn primary" href="/auth/${encodeURIComponent(e.alias)}">Authorize ${escapeHtml(e.label)}</a>`).join('');
  renderSharefilePrompt();
}
function sheetEditUrlFromCsv(csvUrl){
  const m = String(csvUrl||'').match(/\/spreadsheets\/d\/([^/]+)/);
  return m ? `https://docs.google.com/spreadsheets/d/${m[1]}/edit` : '';
}
function renderSharefilePrompt(){
  const el = document.getElementById('sharefilePrompt');
  if(!el) return;
  const csvUrl = document.getElementById('machineSheetUrl')?.value?.trim() || '<PASTE_MACHINE_USAGE_CSV_URL_HERE>';
  const sheetUrl = sheetEditUrlFromCsv(csvUrl) || '<PASTE_GOOGLE_SHEET_EDIT_URL_HERE>';
  const cols = 'agent,machine,os,generated_at,current_account_alias,auth_fingerprint,tokens_30m,tokens_1h,tokens_2h,tokens_3h,tokens_24h,tokens_7d,thread_count_24h,thread_count_7d,last_used_at,rate_limit_errors_24h';
  el.value = `You are setting up this machine/agent to report Codex usage into a shared Google Sheet. Do this end-to-end: inspect local Codex state/logs, create a small updater script, schedule it as a background cron job, run it once, and verify the row appears/updates in the Sheet. Do not expose OAuth tokens, API keys, or raw auth.json contents in chat/logs.

Shared usage Sheet:
- Edit URL: ${sheetUrl}
- CSV/read URL: ${csvUrl}
- Tab name: machine_usage
- Required header row: ${cols}

Goal:
- Maintain exactly one row for this machine/agent.
- The row must be updated every 15 minutes by a background cron job.
- If a row for this agent/machine already exists, update it in place; do not append duplicates.
- If authentication is missing, use the Google account associated with this machine to authorize Sheets/Drive access, or ask the sheet owner for the correct shared-file auth method.

Row fields to write:
1. agent: stable agent name, e.g. Tony-MacMini, Backup-WSL, generalist1, work-mac-codex.
2. machine: hostname or stable machine label.
3. os: macos, wsl, linux, windows, etc.
4. generated_at: current ISO-8601 timestamp with timezone.
5. current_account_alias: active Codex/OpenAI account alias if known; otherwise blank/unknown.
6. auth_fingerprint: non-secret fingerprint only, e.g. sha256 of auth file prefix or active account label. Never write raw tokens.
7. tokens_30m: locally observed Codex token total over the last 30 minutes. Use 0 if unavailable.
8. tokens_1h: locally observed Codex token total over the last 1 hour. Use 0 if unavailable.
9. tokens_2h: locally observed Codex token total over the last 2 hours. Use 0 if unavailable.
10. tokens_3h: locally observed Codex token total over the last 3 hours. Use 0 if unavailable.
11. tokens_24h: locally observed Codex token total over the last 24 hours. Use 0 if unavailable.
12. tokens_7d: locally observed Codex token total over the last 7 days. Use 0 if unavailable.
13. thread_count_24h: count of local Codex threads/sessions touched in last 24h.
14. thread_count_7d: count of local Codex threads/sessions touched in last 7d.
15. last_used_at: latest observed Codex thread/log activity timestamp.
16. rate_limit_errors_24h: count of local rate-limit/quota errors in logs over last 24h.

Preferred local data sources:
- ~/.codex/state_*.sqlite for thread/token counters when present.
- ~/.codex/logs, ~/.codex/*.log, Hermes/OpenClaw logs, or Codex CLI logs for rate-limit/quota strings.
- ~/.codex/auth.json or ~/.codex-switch current profile only for a non-secret fingerprint/alias. Never print/copy token fields.

Implementation requirements:
- Write a script named something like codex_usage_sheet_update.py under a user-local scripts directory.
- The script should be idempotent and safe to run repeatedly.
- It must update this machine's row only.
- It must fail loudly in its own log file but stay quiet in normal cron output.
- Store local config separately with: sheet URL/id, tab name, agent name, machine label, auth method.
- Schedule with cron/system scheduler every 15 minutes.
- Run once immediately after installing, then read back the row from the Sheet or CSV URL to verify.

Cron target:
*/15 * * * * /usr/bin/env bash -lc 'python3 /path/to/codex_usage_sheet_update.py >> ~/.codex-switch/codex_usage_sheet_update.log 2>&1'

Success criteria to report back:
- Script path
- Cron entry installed
- Agent/machine row key used
- One successful immediate run timestamp
- Read-back evidence from the shared Sheet/CSV showing the updated row
- Any limitations, e.g. token totals unavailable so 0 is reported
`;
}
updateAddAuthRequirements(); initTokenFilters(); refreshAll(); loadTokenUsage(); loadAnthropicUsage(); loadGrokUsage(); loadMachineUsage(); setInterval(refreshAll, 15000); setInterval(loadMachineUsage, 30000);
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
        if path == "/api/anthropic-usage":
            try:
                return self.send_json(get_anthropic_usage())
            except Exception as exc:
                return self.send_json({"ok": False, "error": str(exc)}, 400)
        if path == "/api/grok-usage":
            try:
                return self.send_json(get_grok_usage())
            except Exception as exc:
                return self.send_json({"ok": False, "error": str(exc)}, 400)
        if path == "/api/machine-usage":
            qs = parse_qs(parsed.query)
            try:
                return self.send_json(get_machine_usage(force=qs.get("force", [""])[0] in {"1", "true", "yes"}))
            except Exception as exc:
                return self.send_json({"ok": False, "error": str(exc), "configured": bool(get_machine_usage_source())}, 400)
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
        if parsed.path == "/api/accounts/details":
            try:
                body = self.read_json_body()
                acct = set_account_details(str(body.get("alias") or ""), body.get("label"), body.get("plan_label"), body.get("plan_details"), body.get("codex_spark_access"))
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
                result = build_share_auth(str(body.get("alias") or ""), body.get("default_model"))
                return self.send_json(result)
            except Exception as exc:
                return self.send_json({"ok": False, "error": str(exc)}, 400)
        if parsed.path == "/api/accounts/sync-auth":
            try:
                body = self.read_json_body()
                result = sync_auth_to_local_stores(str(body.get("alias") or ""), body.get("default_model"))
                return self.send_json(result)
            except Exception as exc:
                return self.send_json({"ok": False, "error": str(exc)}, 400)
        if parsed.path == "/api/provider-keys/add":
            try:
                body = self.read_json_body()
                result = add_api_key_account(
                    str(body.get("provider") or ""),
                    str(body.get("alias") or ""),
                    str(body.get("label") or ""),
                    str(body.get("api_key") or ""),
                )
                return self.send_json(result)
            except Exception as exc:
                return self.send_json({"ok": False, "error": str(exc)}, 400)
        if parsed.path == "/api/machine-usage/source":
            try:
                body = self.read_json_body()
                return self.send_json(set_machine_usage_source(str(body.get("url") or "")))
            except Exception as exc:
                return self.send_json({"ok": False, "error": str(exc)}, 400)
        if parsed.path == "/api/machine-usage/details":
            try:
                body = self.read_json_body()
                return self.send_json(set_machine_override(str(body.get("agent") or ""), str(body.get("machine") or ""), str(body.get("display_machine") or ""), str(body.get("machine_details") or "")))
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
    if os.sep in BIN and not Path(BIN).exists():
        raise SystemExit(f"codex-switch binary not found: {BIN}")
    if os.sep not in BIN and shutil.which(BIN) is None:
        raise SystemExit(
            f"codex-switch binary not found on PATH: {BIN}. "
            "Install it or set CODEX_SWITCH_BIN=/absolute/path/to/codex-switch."
        )
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
