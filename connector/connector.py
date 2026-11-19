#!/usr/bin/env python3
"""
hermes-connectors - Connector Hub core + CLI (Phase 1)

Universal provider connector for the Hermes agent:
  - Register any OpenAI-compatible provider by URL + API key.
  - Discover models, keep FREE models only.
  - Probe every free model (reply probe + tool probe).
  - Write only free + working (+ quota-limited) models into Hermes config.yaml
    providers section, with timestamped backups.
  - Lenient failure policy: failing models stay visible in state with
    "retry later"; they are never silently deleted.
  - NEVER touches the global `model:` default section (policy.touch_default).

Phase 1 scope: core engine + CLI + JSON status API (serve).
Phase 2 scope: web dashboard UI + action API (sweep/retry/add/decide/remove/restart-gateway).
Phase 3 scope: OAuth (RFC 8628 device flow + companion token stores), monitor-only oauth
providers, token import, and logon auto-start (Scheduled Tasks) for hub + bridges.

Zero runtime dependencies (stdlib only). Python >= 3.9.
"""

from __future__ import annotations

import argparse
import copy
import datetime as _dt
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import socket
import concurrent.futures as cf
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------
# Paths & constants
# --------------------------------------------------------------------------

HERMES_HOME = os.environ.get("HERMES_HOME") or os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "hermes"
)
CONNECTOR_DIR = os.path.join(HERMES_HOME, "connector")
STATE_PATH = os.path.join(CONNECTOR_DIR, "state.json")
CONFIG_PATH = os.path.join(HERMES_HOME, "config.yaml")
ENV_PATH = os.path.join(HERMES_HOME, ".env")
LOG_PATH = os.path.join(CONNECTOR_DIR, "connector.log")
TOKENS_DIR = os.path.join(CONNECTOR_DIR, "tokens")


def _load_connector_env() -> None:
    """Load CONNECTOR_* knobs from hermes/.env even when launched via the
    hidden VBS wrapper (which does not source .env into os.environ)."""
    try:
        with open(ENV_PATH, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                # knobs the hub itself honors when launched via the hidden VBS
                prefixes = ("CONNECTOR_", "COMBO_")
                exact = ("CLINE_BRIDGE_PROXY_TIMEOUT", "ANTI_GRAVITY_BRIDGE_PROXY_TIMEOUT")
                if (k.startswith(prefixes) or k in exact) and k not in os.environ:
                    os.environ[k] = v
    except OSError:
        pass


_load_connector_env()

SERVE_PORT = int(os.environ.get("CONNECTOR_SERVE_PORT", "8399"))

PROBE_TIMEOUT_S = int(os.environ.get("CONNECTOR_PROBE_TIMEOUT", "120"))

# Dashboard/service engine state (process-local; cross-process safety via state_lock)
ENGINE = {"busy": False, "current": "", "updated_at": None}
_HERMES_VERIFY = {"state": "never", "at": None, "output": "", "secs": None}

DEFAULT_POLICY = {
    "free_only": True,          # only free models are shown/added
    "auto_add_working": True,   # sync free+working(+quota) models into Hermes
    "lenient_failures": True,   # failing models kept in state, retry later
    "touch_default": False,     # NEVER change the global default model
}

DEFAULT_POLICY_COMMENT = {
    "free_only": "only free models are eligible for Hermes",
    "auto_add_working": "write free+working(+quota) models into Hermes config",
    "lenient_failures": "failing models stay in state with retry-later, never auto-deleted",
    "touch_default": "connector never writes the global model: section",
}

# --------------------------------------------------------------------------
# Small utils
# --------------------------------------------------------------------------

def utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(CONNECTOR_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8", errors="replace") as f:
            f.write(f"{_dt.datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except Exception:
        pass


def http_json(url: str, method: str = "GET", body: dict | None = None,
              key: str | None = None, timeout: int = 30):
    """Return (status, parsed_json_or_text). Never raises for HTTP errors."""
    data = None
    headers = {"content-type": "application/json", "accept": "application/json"}
    if key:
        headers["authorization"] = f"Bearer {key}"
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        status = e.code
    except Exception as e:  # connection refused, timeout, dns, ...
        return 0, {"_error": str(e)}
    try:
        return status, json.loads(raw)
    except Exception:
        return status, raw


def mask(key: str | None) -> str:
    if not key:
        return "(none)"
    if len(key) <= 6:
        return "***"
    return f"***{key[-4:]}"


# --------------------------------------------------------------------------
# OAuth token store (connector-managed tokens; never written into Hermes files)
# --------------------------------------------------------------------------

def token_store_path(slug: str) -> str:
    return os.path.join(TOKENS_DIR, f"{slug}.json")


def harden_file(path: str) -> None:
    """Best-effort restrictive ACL on Windows (current user only)."""
    try:
        user = os.environ.get("USERNAME", "CurrentUser")
        subprocess.run(["icacls", path, "/inheritance:r", "/grant:r", f"{user}:F"],
                       capture_output=True, timeout=30)
    except Exception as e:
        log(f"WARN: could not harden {path}: {e}")


def token_save(slug: str, data: dict) -> None:
    os.makedirs(TOKENS_DIR, exist_ok=True)
    path = token_store_path(slug)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    harden_file(path)


def token_load(slug: str) -> dict | None:
    try:
        with open(token_store_path(slug), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def token_clear(slug: str) -> None:
    try:
        os.unlink(token_store_path(slug))
    except OSError:
        pass


def token_store_info(external_path: str) -> dict | None:
    """Read a companion-managed token store (e.g. ~/.omnirush-proxy/tokens.json)."""
    try:
        with open(external_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        t = raw.get("tokens")[0] if isinstance(raw.get("tokens"), list) and raw["tokens"] else raw
        return {
            "present": bool(t.get("accessToken") or t.get("access_token") or t.get("refreshToken") or t.get("refresh_token")),
            "email": t.get("email"),
            "path": external_path,
        }
    except Exception:
        return {"present": False, "email": None, "path": external_path}


def oauth_refresh(slug: str, p: dict, tok: dict) -> dict | None:
    """Refresh an access token via the provider's token_url (grant_type=refresh_token)."""
    oa = p.get("oauth") or {}
    rt = tok.get("refresh_token")
    if not rt or not oa.get("token_url"):
        return None
    form = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": rt,
        **({"client_id": oa["client_id"]} if oa.get("client_id") else {}),
    }).encode()
    req = urllib.request.Request(oa["token_url"], data=form, method="POST",
                                 headers={"content-type": "application/x-www-form-urlencoded",
                                          "accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            d = json.loads(resp.read().decode())
    except Exception as e:
        log(f"oauth refresh failed for {slug}: {e}")
        return None
    if not d.get("access_token"):
        log(f"oauth refresh returned no access_token for {slug}: {str(d)[:200]}")
        return None
    tok.update({
        "access_token": d["access_token"],
        "refresh_token": d.get("refresh_token") or rt,
        "expires_at": time.time() + (int(d.get("expires_in") or 3600) - 60) * 1,
        "updated_at": utcnow(),
    })
    token_save(slug, tok)
    log(f"oauth token refreshed for {slug} (valid ~{int((tok['expires_at'] - time.time()) / 60)}m)")
    return tok


def provider_bearer(slug: str, p: dict) -> tuple[str | None, str]:
    """Resolve the bearer credential for a provider.
    Returns (token_or_None, display)."""
    auth = p.get("auth", "static")
    if auth == "static":
        ke = p.get("key_env")
        val = env_read().get(ke) if ke else None
        return val, mask(val)
    if auth == "oauth":
        tok = token_load(slug) or {}
        exp = tok.get("expires_at") or 0
        if tok.get("access_token") and exp and time.time() >= exp - 60:
            tok = oauth_refresh(slug, p, tok) or tok
        at = tok.get("access_token")
        if at:
            return at, mask(at)
        return None, "(no token - login required)"
    if auth == "oauth_store":
        info = token_store_info(p.get("token_store") or "")
        if info and info.get("present"):
            ke = p.get("key_env")
            val = env_read().get(ke) if ke else None
            who = f" ({info['email']})" if info.get("email") else ""
            return val, f"oauth-store{who}"
        return None, "(token store empty)"
    return None, "(unknown auth)"


# --------------------------------------------------------------------------
# Hermes .env helpers
# --------------------------------------------------------------------------

def env_read() -> dict:
    out = {}
    try:
        with open(ENV_PATH, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return out


def env_has(key_name: str) -> bool:
    return key_name in env_read()


def env_upsert(key_name: str, value: str, backup: bool = True) -> None:
    """Add or replace KEY=value in Hermes .env (with backup)."""
    if backup:
        bpath = f"{ENV_PATH}.pre-connector-{stamp()}"
        try:
            shutil.copyfile(ENV_PATH, bpath)
        except FileNotFoundError:
            pass
    lines = []
    try:
        with open(ENV_PATH, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        pass
    replaced = False
    out = []
    for line in lines:
        if line.strip().startswith(f"{key_name}="):
            out.append(f"{key_name}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{key_name}={value}")
    with open(ENV_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out).rstrip("\n") + "\n")


def env_remove(key_name: str, backup: bool = True) -> bool:
    if not env_has(key_name):
        return False
    if backup:
        bpath = f"{ENV_PATH}.pre-connector-{stamp()}"
        try:
            shutil.copyfile(ENV_PATH, bpath)
        except FileNotFoundError:
            pass
    lines = []
    try:
        with open(ENV_PATH, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        pass
    out = [ln for ln in lines if not ln.strip().startswith(f"{key_name}=")]
    with open(ENV_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out).rstrip("\n") + ("\n" if out else ""))
    return True


# --------------------------------------------------------------------------
# config.yaml providers-section writer (text-based, additive)
# --------------------------------------------------------------------------

_PROV_LINE = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$")


def yaml_backup() -> str:
    bpath = f"{CONFIG_PATH}.pre-connector-{stamp()}"
    shutil.copyfile(CONFIG_PATH, bpath)
    return bpath


def render_provider_block(slug: str, name: str, url: str, key_env: str,
                          default_model: str, models: list[str]) -> str:
    lines = [
        f"  {slug}:",
        f"    name: {name}",
        f"    api: {url}",
        f"    key_env: {key_env}",
        f"    model: {default_model}",
        f"    discover_models: false",
        f"    models:",
    ]
    for mid in models:
        lines.append(f'      {mid}: "{mid}"')
    return "\n".join(lines) + "\n"


def yaml_list_provider_slugs() -> list[str]:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return []
    slugs, in_providers = [], False
    for line in lines:
        if line.startswith("providers:"):
            in_providers = True
            continue
        if in_providers:
            if line and not line.startswith((" ", "\t", "#")):
                break  # next top-level key
            m = _PROV_LINE.match(line)
            if m:
                slugs.append(m.group(1))
    return slugs


def yaml_upsert_provider(slug: str, name: str, url: str, key_env: str,
                         default_model: str, models: list[str]) -> str:
    """Insert/replace a provider block inside the providers: section.
    Returns backup path. Global `model:` section is never touched."""
    backup = yaml_backup()
    with open(CONFIG_PATH, "r", encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()

    new_block = render_provider_block(slug, name, url, key_env, default_model, models).rstrip("\n").split("\n")

    # locate providers: section and its end (next top-level key)
    prov_idx = None
    for i, line in enumerate(lines):
        if line.startswith("providers:"):
            prov_idx = i
            break
    if prov_idx is None:
        raise RuntimeError("config.yaml has no top-level 'providers:' section")

    end_idx = len(lines)
    for i in range(prov_idx + 1, len(lines)):
        line = lines[i]
        if line and not line.startswith((" ", "\t", "#")):
            end_idx = i
            break

    # find existing block for slug inside [prov_idx+1, end_idx)
    slug_start = None
    for i in range(prov_idx + 1, end_idx):
        m = _PROV_LINE.match(lines[i])
        if m and m.group(1) == slug:
            slug_start = i
            break
    slug_end = None
    if slug_start is not None:
        for i in range(slug_start + 1, end_idx):
            line = lines[i]
            if not line.strip():
                continue
            if _PROV_LINE.match(line) or (line and not line.startswith(" ")):
                slug_end = i
                break
        if slug_end is None:
            slug_end = end_idx

    if slug_start is not None:
        lines = lines[:slug_start] + new_block + lines[slug_end:]
    else:
        lines = lines[:end_idx] + new_block + lines[end_idx:]

    with open(CONFIG_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines).rstrip("\n") + "\n")
    return backup


def yaml_remove_provider(slug: str) -> str | None:
    """Remove a provider block from providers: section. Returns backup path."""
    with open(CONFIG_PATH, "r", encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    prov_idx = None
    for i, line in enumerate(lines):
        if line.startswith("providers:"):
            prov_idx = i
            break
    if prov_idx is None:
        return None
    end_idx = len(lines)
    for i in range(prov_idx + 1, len(lines)):
        if lines[i] and not lines[i].startswith((" ", "\t", "#")):
            end_idx = i
            break
    slug_start = slug_end = None
    for i in range(prov_idx + 1, end_idx):
        m = _PROV_LINE.match(lines[i])
        if m and m.group(1) == slug:
            slug_start = i
            break
    if slug_start is None:
        return None
    for i in range(slug_start + 1, end_idx):
        line = lines[i]
        if not line.strip():
            continue
        if _PROV_LINE.match(line) or (line and not line.startswith(" ")):
            slug_end = i
            break
    if slug_end is None:
        slug_end = end_idx
    backup = yaml_backup()
    lines = lines[:slug_start] + lines[slug_end:]
    with open(CONFIG_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines).rstrip("\n") + "\n")
    return backup


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def state_load() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = {}
    st.setdefault("version", 1)
    st.setdefault("updated_at", utcnow())
    st.setdefault("providers", {})
    st.setdefault("combos", {"free-auto": {"chain": list(DEFAULT_COMBO)}})
    st.setdefault("routing", {"strategy": "adaptive", "stats": {}})
    pol = copy.deepcopy(DEFAULT_POLICY)
    pol.update(st.get("policy") or {})
    st["policy"] = pol
    return st


def state_save(st: dict) -> None:
    st["updated_at"] = utcnow()
    tmp = STATE_PATH + ".tmp"
    os.makedirs(CONNECTOR_DIR, exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2, ensure_ascii=False)
    os.replace(tmp, STATE_PATH)


@contextmanager
def state_lock(timeout: float = 60.0):
    """Cross-process advisory lock (CLI and serve may both mutate state)."""
    path = STATE_PATH + ".lock"
    os.makedirs(CONNECTOR_DIR, exist_ok=True)
    deadline = time.time() + timeout
    fd = None
    while time.time() < deadline:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(path) > 300:  # stale lock
                    os.unlink(path)
                    continue
            except OSError:
                pass
            time.sleep(0.2)
    if fd is not None:
        try:
            os.write(fd, str(os.getpid()).encode())
        except OSError:
            pass
    try:
        yield fd is not None
    finally:
        if fd is not None:
            os.close(fd)
            try:
                os.unlink(path)
            except OSError:
                pass


# --------------------------------------------------------------------------
# Free-model classification
# --------------------------------------------------------------------------

_FREE_HINTS = ("free", "-free", "free-", "(free)")


def classify_free(model_entry: dict, model_id: str) -> bool:
    """Heuristics: id/name contains 'free', or pricing metadata says 0."""
    mid = (model_id or "").lower()
    if "free" in mid:
        return True
    name = str(model_entry.get("name") or "").lower()
    if "free" in name:
        return True
    for pk in ("pricing", "price"):
        p = model_entry.get(pk)
        if isinstance(p, dict):
            prompt = p.get("prompt") or p.get("input") or 0
            comp = p.get("completion") or p.get("output") or 0
            try:
                if float(prompt) == 0.0 and float(comp) == 0.0:
                    return True
            except (TypeError, ValueError):
                pass
        elif isinstance(p, (int, float)) and float(p) == 0.0:
            return True
    return False


# --------------------------------------------------------------------------
# Provider registry helpers
# --------------------------------------------------------------------------

def fetch_model_ids(url: str, key: str | None) -> tuple[list[dict], str]:
    """Returns (models_with_meta, source). source: 'api' | 'fallback'."""
    status, data = http_json(url.rstrip("/") + "/models", key=key, timeout=20)
    if status == 200 and isinstance(data, dict):
        items = data.get("data")
        if isinstance(items, list) and items:
            clean = []
            for it in items:
                if isinstance(it, dict) and it.get("id"):
                    clean.append({"id": str(it["id"]), **{k: v for k, v in it.items() if k != "id"}})
                elif isinstance(it, str):
                    clean.append({"id": it})
            return clean, "api"
    return [], "fallback"


# Bearer resolution happens exclusively via provider_bearer(slug, p) above.


# --------------------------------------------------------------------------
# Probe engine
# --------------------------------------------------------------------------

TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "terminal",
        "description": "Run a shell command and return its output.",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "The command to run"}},
            "required": ["command"],
        },
    },
}


def classify_http_failure(status: int, text) -> str:
    """Map a failed probe to a status bucket: quota | failing."""
    blob = ""
    if isinstance(text, dict):
        blob = json.dumps(text).lower()
    elif isinstance(text, str):
        blob = text.lower()
    if status == 429 or "rate limit" in blob or "free usage limit" in blob or "free-limit" in blob \
            or "quota reached" in blob or "resource_exhausted" in blob or "individual quota" in blob:
        return "quota"
    return "failing"


def probe_reply(url: str, model: str, key: str | None) -> dict:
    marker = f"CONN_OK_{random.randint(1000, 9999)}"
    body = {
        "model": model,
        "messages": [{"role": "user", "content": f"Reply with only this exact text: {marker}"}],
        "max_tokens": 200,
    }
    t0 = time.time()
    status, data = http_json(url.rstrip("/") + "/chat/completions", "POST", body, key=key,
                             timeout=PROBE_TIMEOUT_S)
    ms = int((time.time() - t0) * 1000)
    res = {"kind": "reply", "http": status, "ms": ms, "at": utcnow()}
    if status == 200 and isinstance(data, dict):
        try:
            content = data["choices"][0]["message"].get("content") or ""
        except Exception:
            content = ""
        if marker in content:
            res["ok"] = True
        elif content.strip():
            res["ok"] = True
            res["note"] = "no_marker"
        else:
            res["ok"] = False
            res["note"] = "unusable_reply"
    elif status == 0:
        res["ok"] = False
        res["note"] = "unreachable"
        res["error"] = (data or {}).get("_error", "connection error") if isinstance(data, dict) else "connection error"
    else:
        res["ok"] = False
        res["bucket"] = classify_http_failure(status, data)
        res["error"] = (data.get("error", {}).get("message") if isinstance(data, dict) and isinstance(data.get("error"), dict) else None) or str(data)[:200]
    return res


def probe_tools(url: str, model: str, key: str | None) -> dict:
    body = {
        "model": model,
        "messages": [{"role": "user",
                      "content": "Use the terminal tool to run: echo CONNECTOR_PROBE  Reply with only the exact output."}],
        "tools": [TOOL_SCHEMA],
        "max_tokens": 300,
    }
    t0 = time.time()
    status, data = http_json(url.rstrip("/") + "/chat/completions", "POST", body, key=key,
                             timeout=PROBE_TIMEOUT_S)
    ms = int((time.time() - t0) * 1000)
    res = {"kind": "tool", "http": status, "ms": ms, "at": utcnow()}
    if status == 200 and isinstance(data, dict):
        try:
            msg = data["choices"][0]["message"]
        except Exception:
            msg = {}
        tool_calls = msg.get("tool_calls") or []
        content = msg.get("content") or ""
        if tool_calls:
            res["ok"] = True
            res["mode"] = "tool_calls"
        elif "CONNECTOR_PROBE" in content:
            res["ok"] = True
            res["mode"] = "content"
            res["note"] = "tool via content (emulated)"
        elif content.strip():
            res["ok"] = False
            res["note"] = "no_tool_call"
        else:
            res["ok"] = False
            res["note"] = "unusable_reply"
    elif status == 0:
        res["ok"] = False
        res["note"] = "unreachable"
        res["error"] = (data or {}).get("_error", "connection error") if isinstance(data, dict) else "connection error"
    else:
        res["ok"] = False
        res["bucket"] = classify_http_failure(status, data)
        res["error"] = (data.get("error", {}).get("message") if isinstance(data, dict) and isinstance(data.get("error"), dict) else None) or str(data)[:200]
    return res


def derive_model_status(reply: dict, tool: dict) -> str:
    """working | quota | failing, per blueprint semantics:
    working  -> reply ok (tools informational)
    quota    -> upstream reachable but rate/free limited (429 semantics)
    failing  -> unreachable / auth / not found / unusable"""
    if reply.get("ok"):
        return "working"
    if reply.get("bucket") == "quota":
        return "quota"
    # tolerate a failed tool probe when reply was fine (already handled above)
    if reply.get("bucket") == "failing" or reply.get("note") in ("unreachable", "unusable_reply"):
        return "failing"
    return "failing"


def probe_model(url: str, model: str, key: str | None, do_tools: bool = True) -> dict:
    reply = probe_reply(url, model, key)
    tool = None
    if reply.get("ok") and do_tools:
        tool = probe_tools(url, model, key)
    status = derive_model_status(reply, tool or {})
    return {"status": status, "reply": reply, "tool": tool}


# --------------------------------------------------------------------------
# Hermes sync (config.yaml writers gated on policy)
# --------------------------------------------------------------------------

def eligible_for_hermes(model_rec: dict, policy: dict) -> bool:
    if policy.get("free_only") and not model_rec.get("free"):
        return False
    return model_rec.get("status") in ("working", "quota")


def sync_provider_to_hermes(st: dict, slug: str) -> dict:
    """Write/replace providers.<slug> in Hermes config.yaml with exactly the
    eligible (free + working/quota) models. Returns action summary."""
    p = st["providers"][slug]
    policy = st["policy"]
    if not policy.get("auto_add_working"):
        return {"action": "skipped", "reason": "auto_add_working disabled"}
    auth = p.get("auth", "static")
    if auth != "static" and not p.get("hermes_url"):
        return {"action": "skipped_monitor_only",
                "reason": f"{auth} provider without hermes_url (state + dashboard only)"}
    models_state = p.get("models", {})
    elig = sorted([mid for mid, rec in models_state.items()
                   if isinstance(rec, dict) and eligible_for_hermes(rec, policy)])
    if not elig:
        if yaml_list_provider_slugs() and slug in yaml_list_provider_slugs():
            backup = yaml_remove_provider(slug)
            return {"action": "removed_from_hermes", "models": [], "backup": backup}
        return {"action": "nothing_to_write", "models": []}
    default_model = elig[0]
    target_url = p.get("hermes_url") or p["url"]
    target_key_env = p.get("hermes_key_env") or p.get("key_env") or ""
    backup = yaml_upsert_provider(
        slug=slug,
        name=p.get("name", slug),
        url=target_url,
        key_env=target_key_env,
        default_model=default_model,
        models=elig,
    )
    for mid in elig:
        models_state[mid]["in_hermes"] = True
    for mid, rec in models_state.items():
        if mid not in elig:
            rec["in_hermes"] = False
    return {"action": "written", "models": elig, "default_model": default_model, "backup": backup}


# --------------------------------------------------------------------------
# Seed registry (existing local bridges)
# --------------------------------------------------------------------------

SEED_PROVIDERS = {
    "cline-free": {
        "name": "Cline Free",
        "url": "http://127.0.0.1:8378/v1",
        "key_env": "CLINE_BRIDGE_PROXY_KEY",
        "auth": "static",
        "fallback_models": [
            "cline-free/nemotron-3.5-super-vl",
            "cline-free/solar-mini4",
            "nvidia/nemotron-3.5-lightning-free",
        ],
        "key_env_added_by_connector": False,
    },
    "opencode-free": {
        "name": "OpenCode Free",
        "url": "http://127.0.0.1:8377/v1",
        "key_env": "OPENCODE_BRIDGE_PROXY_KEY",
        "auth": "static",
        "fallback_models": [
            "opencode/beam-free",
            "opencode/big-pickle",
            "opencode/deepseek-v4.1-flash-free",
            "opencode/flint-40b-free",
            "opencode/glm-5.3-flash-free",
            "opencode/iceman-free",
            "opencode/inkling-2-small-free",
            "opencode/longcat-2.5-free",
            "opencode/mimo-v2.6-flash-free",
            "opencode/minimax-m3.1-flash-free",
            "opencode/nemotron-3.5-lightning-free",
            "opencode/nemotron-3.5-super-vl-free",
        ],
        # models that are free per the provider's own free-tier list even
        # though the id has no "free" marker (price metadata unavailable)
        "free_override": ["opencode/big-pickle"],
        "key_env_added_by_connector": False,
    },
    # Google Gemini API bridge (port 8380): OpenAI-compatible proxy over the
    # Gemini free tier using GOOGLE_API_KEY from hermes/.env.
    "gemini-free": {
        "name": "Gemini Free (Google API key)",
        "url": "http://127.0.0.1:8380/v1",
        "key_env": "GEMINI_BRIDGE_PROXY_KEY",
        "auth": "static",
        "fallback_models": [
            "gemini-3.5-flash-lite",
            "gemini-3.1-flash-lite",
            "gemini-flash-lite-latest",
            "gemini-2.5-flash",
            "gemini-3-flash-preview",
            "gemini-flash-latest",
            "gemini-3.5-flash",
        ],
        "free_override": [
            "gemini-3.5-flash-lite",
            "gemini-3.1-flash-lite",
            "gemini-flash-lite-latest",
            "gemini-2.5-flash",
            "gemini-3-flash-preview",
            "gemini-flash-latest",
            "gemini-3.5-flash",
        ],
        "key_env_added_by_connector": False,
    },
    # OpenRouter free models (port 8383): native OpenAI-compatible pass-through.
    "openrouter-free": {
        "name": "OpenRouter Free",
        "url": "http://127.0.0.1:8383/v1",
        "key_env": "OPENROUTER_BRIDGE_PROXY_KEY",
        "auth": "static",
        "fallback_models": [
            "openrouter/liquid/lfm-3-1.7b:free",
            "openrouter/particle-ai/particle-1:free",
            "openrouter/nvidia/nemotron-3.5-lightning:free",
            "openrouter/nvidia/nemotron-3.5-super-vl:free",
            "openrouter/baidu/ernie-5.5-flash-preview:free",
            "openrouter/recursive/flint-v1.0:free",
            "openrouter/dots-studio/dots-3-note-preview:free",
            "openrouter/cohere/north-mini-code:free",
            "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free",
            "openrouter/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
        ],
        "free_override": [
            "openrouter/liquid/lfm-3-1.7b:free",
            "openrouter/particle-ai/particle-1:free",
            "openrouter/nvidia/nemotron-3.5-lightning:free",
            "openrouter/nvidia/nemotron-3.5-super-vl:free",
            "openrouter/baidu/ernie-5.5-flash-preview:free",
            "openrouter/recursive/flint-v1.0:free",
            "openrouter/dots-studio/dots-3-note-preview:free",
            "openrouter/cohere/north-mini-code:free",
            "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free",
            "openrouter/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
        ],
        "key_env_added_by_connector": False,
    },
    # NVIDIA NIM bridge (port 8382): OpenAI-compatible proxy over
    # integrate.api.nvidia.com with a verified working-model whitelist.
    "nvidia-free": {
        "name": "NVIDIA NIM Free",
        "url": "http://127.0.0.1:8382/v1",
        "key_env": "NVIDIA_BRIDGE_PROXY_KEY",
        "auth": "static",
        "fallback_models": [
            "nvidia/nemotron-3.5-lightning-30b-a3b",
            "nvidia/nemotron-3.5-bolt-e4b",
            "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
            "nvidia/nemotron-3.5-super-vl-120b-a12b",
            "minimaxai/minimax-m3.1-flash",
        ],
        "free_override": [
            "nvidia/nemotron-3.5-lightning-30b-a3b",
            "nvidia/nemotron-3.5-bolt-e4b",
            "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
            "nvidia/nemotron-3.5-super-vl-120b-a12b",
            "minimaxai/minimax-m3.1-flash",
        ],
        "key_env_added_by_connector": False,
    },
    # Zhipu / Z.ai GLM free Flash models (port 8381). Paid GLM-5.x excluded.
    "zhipu-free": {
        "name": "Zhipu GLM Free",
        "url": "http://127.0.0.1:8381/v1",
        "key_env": "ZHIPU_BRIDGE_PROXY_KEY",
        "auth": "static",
        "fallback_models": ["glm-4-flash", "glm-4.7-flashx", "glm-4.7-flash", "glm-4.5-flash"],
        "free_override": ["glm-4-flash", "glm-4.7-flashx", "glm-4.7-flash", "glm-4.5-flash"],
        "key_env_added_by_connector": False,
    },
    # Antigravity bridge (port 8379): OpenAI-compatible proxy over the
    # Antigravity CLI (`agy -p`). Free Google tier: all models the account
    # can use via the CLI are free -> free_override covers the whole list.
    "antigravity-free": {
        "name": "Antigravity Free",
        "url": "http://127.0.0.1:8379/v1",
        "key_env": "ANTI_GRAVITY_BRIDGE_PROXY_KEY",
        "auth": "static",
        "fallback_models": [
            "gemini-3.8-flash-low",
            "gemini-3.8-flash-medium",
            "gemini-3.8-flash-high",
            "claude-sonnet-4-6",
            "claude-opus-4-6-thinking",
            "gpt-oss-120b-medium",
        ],
        "free_override": [
            "gemini-3.8-flash-low",
            "gemini-3.8-flash-medium",
            "gemini-3.8-flash-high",
            "claude-sonnet-4-6",
            "claude-opus-4-6-thinking",
            "gpt-oss-120b-medium",
        ],
        "key_env_added_by_connector": False,
    },
}


def ensure_provider(st: dict, slug: str, name: str, url: str, key_env: str,
                    auth: str = "static", models: list[str] | None = None,
                    fallback_models: list[str] | None = None,
                    key_env_added_by_connector: bool = False,
                    free_override: list[str] | None = None,
                    oauth: dict | None = None, token_store: str | None = None,
                    hermes_url: str | None = None,
                    hermes_key_env: str | None = None) -> dict:
    items, source = [], "none"
    # resolve the bearer for model discovery through the auth system, not just
    # the static env key (oauth providers need their access token here)
    probe_p = {**st["providers"].get(slug, {}), "auth": auth,
               "oauth": oauth if oauth is not None else st["providers"].get(slug, {}).get("oauth"),
               "token_store": token_store if token_store is not None else st["providers"].get(slug, {}).get("token_store", ""),
               "key_env": key_env or ""}
    key = provider_bearer(slug, probe_p)[0]
    if models:
        items, source = [{"id": m} for m in models], "provided"
    else:
        items, source = fetch_model_ids(url, key)
    if not items and fallback_models:
        items, source = [{"id": m} for m in fallback_models], "fallback"
    policy = st["policy"]
    override = set(free_override or [])
    prev_models = st["providers"].get(slug, {}).get("models", {})
    prev_p = st["providers"].get(slug, {})
    models_state = {}
    for it in items:
        mid = it["id"]
        is_free = (not policy.get("free_only")) or classify_free(it, mid) or (mid in override)
        old = prev_models.get(mid)
        if isinstance(old, dict):
            # merge: preserve probe results / in_hermes flags, refresh free flag
            old["free"] = is_free
            models_state[mid] = old
        else:
            models_state[mid] = {
                "free": is_free,
                "status": "unknown",
                "in_hermes": False,
                "reply": None,
                "tool": None,
            }
    # retain previously known models that disappeared from discovery
    # (lenient: they stay until probes mark them failing)
    for mid, old in prev_models.items():
        if mid not in models_state and isinstance(old, dict):
            models_state[mid] = old
    p = st["providers"].get(slug, {})
    hermes_url = hermes_url if hermes_url is not None else prev_p.get("hermes_url", "")
    hermes_key_env = hermes_key_env if hermes_key_env is not None else prev_p.get("hermes_key_env", "")
    p.update({
        "name": name,
        "url": url,
        "key_env": key_env or "",
        "auth": auth,
        "key_env_added_by_connector": key_env_added_by_connector,
        "model_source": source,
        "models": models_state,
        "registered_at": p.get("registered_at") or utcnow(),
        "oauth": oauth if oauth is not None else prev_p.get("oauth"),
        "token_store": token_store if token_store is not None else prev_p.get("token_store", ""),
        "hermes_url": hermes_url if hermes_url is not None else prev_p.get("hermes_url", ""),
        "hermes_key_env": hermes_key_env if hermes_key_env is not None else prev_p.get("hermes_key_env", ""),
        "free_override": list(free_override or []),
        "monitor_only": bool(auth != "static" and not (hermes_url if hermes_url is not None
                                                       else prev_p.get("hermes_url", ""))),
    })
    st["providers"][slug] = p
    return p


# --------------------------------------------------------------------------
# Sweep
# --------------------------------------------------------------------------

def sweep(st: dict, only: list[str] | None = None, do_tools: bool = True,
          deep: bool = False, force: bool = False) -> dict:
    """Locked entry point around the sweep engine (safe from CLI and serve)."""
    with state_lock():
        ENGINE.update({"busy": True, "current": "starting sweep", "updated_at": utcnow()})
        try:
            return _sweep_engine(st, only=only, do_tools=do_tools, deep=deep, force=force)
        finally:
            ENGINE.update({"busy": False, "current": "", "updated_at": utcnow()})


def _sweep_engine(st: dict, only: list[str] | None = None, do_tools: bool = True,
          deep: bool = False, force: bool = False) -> dict:
    """Probe providers and sync into Hermes.

    deep=False (sentinel mode, default): quota-aware — probe only models that
      were never probed, plus at most one recently-probed model per provider
      as a sentinel. Avoids burning free-tier daily allowance on every check.
    deep=True: probe every free model of every provider (explicit --all).
    """
    cooldown_s = int(float(os.environ.get("CONNECTOR_PROBE_COOLDOWN_H", "6")) * 3600)
    results = []
    for slug, p in list(st["providers"].items()):
        if only and slug not in only:
            continue
        key, key_disp = provider_bearer(slug, p)
        if p.get("auth", "static") != "static" and not key:
            log(f"provider {slug}: no credential ({key_disp}); skipping probes until login")
            continue
        ENGINE["current"] = f"provider {slug}"
        free_ids = [mid for mid, rec in p["models"].items()
                    if rec.get("free") and not rec.get("removed_upstream")]
        now = time.time()

        def last_probed_ts(mid: str) -> float:
            lp = (p["models"][mid].get("last_probed") or "")
            try:
                return _dt.datetime.fromisoformat(lp.replace("Z", "+00:00")).timestamp()
            except Exception:
                return 0.0

        if deep:
            targets = free_ids
        elif force:
            targets = free_ids[:1]  # single sentinel, bypassing cooldown
        else:
            never = [m for m in free_ids if p["models"][m].get("status") == "unknown"]
            fresh = [m for m in free_ids if now - last_probed_ts(m) < cooldown_s]
            stale = [m for m in free_ids if m not in fresh]
            targets = list(dict.fromkeys(never + stale[:1]))  # at most 1 sentinel per provider
        if not targets:
            log(f"probing provider {slug}: nothing to do (cooldown {cooldown_s // 3600}h; use test --all to force)")
            continue
        log(f"probing provider {slug} ({len(targets)}/{len(free_ids)} free models"
            f"{', deep' if deep else ', sentinel'})")
        account_limited = False
        probe_lock = threading.Lock()

        def _probe_target(mid: str) -> dict:
            nonlocal account_limited
            with probe_lock:
                if account_limited:
                    rec = p["models"][mid]
                    rec["status"] = "quota"
                    rec["account_limit"] = True
                    rec["last_probed"] = utcnow()
                    log(f"  probe {mid} -> QUOTA (account limit propagated, skipped live probe)")
                    return {"provider": slug, "model": mid, "status": "quota",
                            "reply_ms": None, "http": None, "skipped": True}
                ENGINE["current"] = f"probing {mid}"
            log(f"  probe {mid} ...")
            r = probe_model(p["url"], mid, key, do_tools=do_tools)
            rec = p["models"][mid]
            rec["status"] = r["status"]
            rec["reply"] = r["reply"]
            rec["tool"] = r["tool"]
            rec["last_probed"] = utcnow()
            rec.pop("account_limit", None)
            badge = {"working": "OK", "quota": "QUOTA", "failing": "FAIL"}[r["status"]]
            log(f"  probe {mid} -> {badge} reply_ms={r['reply'].get('ms')}")
            out = {"provider": slug, "model": mid, "status": r["status"],
                   "reply_ms": r["reply"].get("ms"), "http": r["reply"].get("http")}
            # account-wide free-usage limit: mark the whole provider quota
            blob = json.dumps(r["reply"].get("error") or "").lower()
            if r["status"] == "quota" and ("free usage limit" in blob or "free-limit" in blob
                                           or "account" in blob or "retry after" in blob
                                           or "quota reached" in blob or "resource_exhausted" in blob):
                with probe_lock:
                    account_limited = True
                log(f"  {slug}: account-level free limit detected; marking remaining models quota")
            return out

        max_workers = max(1, int(os.environ.get("CONNECTOR_PROBE_CONCURRENCY", "4")))
        # CLI-bridge providers serialize calls inside the bridge; keep their
        # parallelism low so probes fail fast instead of queueing on the bridge lock.
        if slug in ("antigravity-free", "cline-free"):
            # These bridges serialize CLI calls internally — extra workers only
            # queue behind the bridge lock and hit the client timeout.
            workers = 1
        else:
            workers = min(max_workers, len(targets))
        if workers > 1:
            with cf.ThreadPoolExecutor(max_workers=workers) as ex:
                for res in ex.map(_probe_target, targets):
                    results.append(res)
        else:
            for mid in targets:
                results.append(_probe_target(mid))
        # account-wide free-usage limit applies to every model of this provider
        if account_limited:
            for mid in free_ids:
                rec = p["models"][mid]
                if rec.get("status") != "quota":
                    rec["status"] = "quota"
                    rec["account_limit"] = True
                    rec["last_probed"] = utcnow()
                    log(f"  {mid} -> QUOTA (account limit propagated)")
        sync = sync_provider_to_hermes(st, slug)
        log(f"sync {slug}: {sync.get('action')} models={len(sync.get('models', []))}")
        results.append({"provider": slug, "model": "(hermes sync)", "status": sync.get("action", ""),
                        "reply_ms": None, "http": None})
    state_save(st)
    return {"results": results}


# --------------------------------------------------------------------------
# Single-model probe + provider add core (shared by CLI and dashboard API)
# --------------------------------------------------------------------------

def probe_one_model(st: dict, slug: str, mid: str, do_tools: bool = True) -> dict:
    with state_lock():
        ENGINE.update({"busy": True, "current": f"probing {mid}", "updated_at": utcnow()})
        try:
            p = st["providers"].get(slug)
            if p is None or mid not in p["models"]:
                return {"error": f"unknown model: {mid}"}
            key, key_disp = provider_bearer(slug, p)
            if p.get("auth", "static") != "static" and not key:
                return {"error": f"no credential for {slug}: {key_disp} "
                                 "(run oauth-login or token-import first)"}
            r = probe_model(p["url"], mid, key, do_tools=do_tools)
            rec = p["models"][mid]
            rec.update({"status": r["status"], "reply": r["reply"], "tool": r["tool"],
                        "last_probed": utcnow()})
            rec.pop("account_limit", None)
            # account-wide limit affects every model of this provider
            blob = json.dumps(r["reply"].get("error") or "").lower()
            if r["status"] == "quota" and ("free usage limit" in blob or "free-limit" in blob
                                           or "retry after" in blob or "quota reached" in blob
                                           or "resource_exhausted" in blob):
                for other, orec in p["models"].items():
                    if orec.get("free") and orec.get("status") != "quota":
                        orec["status"] = "quota"
                        orec["account_limit"] = True
            sync = sync_provider_to_hermes(st, slug)
            state_save(st)
            return {"model": mid, "status": r["status"], "reply": r["reply"],
                    "tool": r["tool"], "sync": sync}
        finally:
            ENGINE.update({"busy": False, "current": "", "updated_at": utcnow()})


def add_provider_core(st: dict, url: str, key: str | None, slug: str, display: str | None,
                      models: list[str] | None, do_tools: bool = True,
                      auth: str = "static", oauth: dict | None = None,
                      token_store: str | None = None, hermes_url: str | None = None,
                      hermes_key: str | None = None) -> dict:
    """Register + deep-probe a new provider (background-friendly).
    Sets pending_decision=True; the caller/CLI/dashboard resolves keep/delete."""
    if slug in st["providers"]:
        return {"error": f"provider '{slug}' already registered; remove it first"}
    if auth not in ("static", "oauth", "oauth_store"):
        return {"error": f"unsupported auth mode: {auth}"}
    if auth == "static" and not key:
        return {"error": "static providers need a key"}
    if auth == "oauth_store" and not token_store:
        return {"error": "oauth_store providers need a token-store path"}
    norm = url.rstrip("/")
    if not norm.endswith("/v1"):
        norm += "/v1"
    key_env = f"{slug.upper().replace('-', '_')}_API_KEY"
    if key:
        env_upsert(key_env, key)
    hermes_key_env = ""
    if hermes_url:
        hermes_key_env = f"{slug.upper().replace('-', '_')}_HERMES_KEY"
        if hermes_key:
            env_upsert(hermes_key_env, hermes_key)
        elif auth == "static":
            # reuse the main key for the hermes-facing endpoint unless told otherwise
            env_upsert(hermes_key_env, key or "")
    with state_lock():
        ensure_provider(st, slug, display or slug, norm, key_env if key else "",
                        auth=auth, models=models, key_env_added_by_connector=bool(key),
                        oauth=oauth, token_store=token_store,
                        hermes_url=hermes_url or "", hermes_key_env=hermes_key_env)
        if hermes_key:
            st["providers"][slug]["hermes_key_env_added_by_connector"] = True
        state_save(st)
    monitor = auth != "static" and not hermes_url
    log(f"added provider {slug} -> {norm} (auth={auth}{' , monitor-only' if monitor else ''});"
        f" deep-probing free models ...")
    st = state_load()
    sweep(st, only=[slug], do_tools=do_tools, deep=True)
    with state_lock():
        st = state_load()
        p = st["providers"].get(slug)
        if p is None:
            return {"error": f"provider {slug} vanished during add"}
        p["pending_decision"] = True
        state_save(st)
    working = [m for m, rec in p["models"].items()
               if rec.get("free") and rec.get("status") in ("working", "quota")]
    failing = [m for m, rec in p["models"].items()
               if rec.get("free") and rec.get("status") == "failing"]
    return {"slug": slug, "url": norm, "key_env": key_env if key else "",
            "auth": auth, "monitor_only": monitor,
            "next_step": ("run oauth-login to obtain a token"
                          if auth == "oauth" and not (p.get("oauth_login") or {}).get("status") == "done"
                          else None),
            "free_models": len(p["models"]), "working": sorted(working),
            "failing": sorted(failing), "pending_decision": True}


# --------------------------------------------------------------------------
# OAuth (RFC 8628 device flow) + token import
# --------------------------------------------------------------------------

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
OAUTH_ACTIVE: set[str] = set()
OAUTH_ACTIVE_LOCK = threading.Lock()


def oauth_progress_set(slug: str, **fields) -> None:
    """Record login progress in state.json (visible in CLI status + dashboard)."""
    with state_lock():
        st = state_load()
        p = st["providers"].get(slug)
        if p is None:
            return
        prog = p.get("oauth_login") or {}
        prog.update(fields)
        prog["updated_at"] = utcnow()
        p["oauth_login"] = prog
        state_save(st)


def oauth_normalize_token(raw: dict) -> dict:
    """Accept camelCase or snake_case token payloads; canonical store dict."""
    access = raw.get("access_token") or raw.get("accessToken") or ""
    refresh = raw.get("refresh_token") or raw.get("refreshToken") or ""
    exp = raw.get("expires_at") or raw.get("expiresAt")
    if not exp:
        expires_in = raw.get("expires_in") or raw.get("expiresIn")
        exp = time.time() + (int(expires_in) - 60) if expires_in else 0
    tok = {"access_token": str(access), "refresh_token": str(refresh),
           "expires_at": float(exp or 0), "updated_at": utcnow()}
    for k in ("email", "account_id", "account", "workspace_id", "organization_id", "scope"):
        if raw.get(k):
            tok[k] = raw[k]
    return tok


def oauth_post_login_discover(slug: str) -> dict:
    """After a token exists: (re)run model discovery + a deep sweep, then flag
    the provider for the keep/delete decision. Bridges the add-before-login gap."""
    with state_lock():
        st = state_load()
        p = st["providers"].get(slug)
        if p is None:
            return {"error": f"unknown provider: {slug}"}
        ensure_provider(st, slug, p.get("name") or slug, p["url"], p.get("key_env") or "",
                        auth=p.get("auth", "oauth"),
                        key_env_added_by_connector=bool(p.get("key_env_added_by_connector")),
                        free_override=(p.get("free_override") or []) or None,
                        oauth=p.get("oauth"), token_store=p.get("token_store") or None,
                        hermes_url=p.get("hermes_url") or None,
                        hermes_key_env=p.get("hermes_key_env") or None)
        state_save(st)
    sweep(state_load(), only=[slug], deep=True)
    with state_lock():
        st = state_load()
        p = st["providers"].get(slug)
        if p is None:
            return {"error": f"provider {slug} vanished during discovery"}
        p["pending_decision"] = True
        state_save(st)
    models = p.get("models") or {}
    working = [m for m, rec in models.items() if rec.get("status") in ("working", "quota")]
    log(f"oauth post-login {slug}: discovered {len(models)} models, working={len(working)}")
    return {"slug": slug, "models": len(models), "working": sorted(working)}


def _oauth_post_form(url: str, form: dict) -> tuple[int, dict]:
    """POST x-www-form-urlencoded, tolerating error bodies (4xx incl. HTTPError)."""
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "content-type": "application/x-www-form-urlencoded",
        "accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"error": "transport_error", "error_description": str(e)}


def oauth_login_core(slug: str, timeout_s: int = 600, open_browser: bool = True,
                     discover: bool = True) -> dict:
    """RFC 8628 device flow: authorize -> show user code -> poll token endpoint.
    Progress is written to providers[slug].oauth_login for dashboard polling."""
    with OAUTH_ACTIVE_LOCK:
        if slug in OAUTH_ACTIVE:
            return {"error": f"oauth login already running for {slug}"}
        OAUTH_ACTIVE.add(slug)
    try:
        st = state_load()
        p = st["providers"].get(slug)
        if p is None:
            return {"error": f"unknown provider: {slug}"}
        oa = p.get("oauth") or {}
        dev_url, token_url, client_id = (oa.get("device_authorization_url"),
                                         oa.get("token_url"), oa.get("client_id"))
        if not (dev_url and token_url and client_id):
            return {"error": "provider oauth config incomplete; need "
                             "device_authorization_url, token_url, client_id"}
        form = {"client_id": client_id}
        if oa.get("scope"):
            form["scope"] = oa["scope"]
        if oa.get("audience"):
            form["audience"] = oa["audience"]
        http, payload = _oauth_post_form(dev_url, form)
        if http != 200 or not payload.get("device_code"):
            return {"error": f"device authorization failed: HTTP {http} {str(payload)[:200]}"}
        device_code = payload["device_code"]
        user_code = payload.get("user_code", "")
        verify_uri = payload.get("verification_uri") or ""
        verify_complete = payload.get("verification_uri_complete") or verify_uri
        interval = int(payload.get("interval") or 5)
        expires_in = int(payload.get("expires_in") or timeout_s)
        deadline = time.time() + min(expires_in, timeout_s)
        oauth_progress_set(slug, status="waiting", user_code=user_code,
                           verification_uri=verify_uri,
                           expires_at=_dt.datetime.fromtimestamp(
                               deadline, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        log(f"oauth login {slug}: user code {user_code} -> {verify_uri}")
        if open_browser:
            try:
                webbrowser.open(verify_complete)
            except Exception:
                pass
        token, err = None, None
        while time.time() < deadline:
            time.sleep(interval)
            http, tok = _oauth_post_form(token_url, {
                "grant_type": DEVICE_GRANT,
                "device_code": device_code,
                "client_id": client_id,
            })
            if http == 200 and tok.get("access_token"):
                token = tok
                break
            gerr = tok.get("error") or ""
            if gerr == "authorization_pending":
                continue
            if gerr == "slow_down":
                interval += 5
                continue
            if gerr:
                err = f"{gerr}: {tok.get('error_description', '')}"[:300]
                break
            if http not in (0, 200, 400, 401, 403, 428):
                err = f"unexpected token response HTTP {http}: {str(tok)[:200]}"
                break
        if token is None:
            reason = err or "timed out waiting for authorization"
            oauth_progress_set(slug, status="error", message=reason)
            return {"error": f"oauth login failed for {slug}: {reason}"}
        saved = oauth_normalize_token(token)
        token_save(slug, saved)
        exp_txt = (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(saved["expires_at"]))
                   if saved.get("expires_at") else "unknown")
        oauth_progress_set(slug, status="done", user_code=user_code,
                           verification_uri=verify_uri, message=f"token stored; expires {exp_txt}")
        log(f"oauth login {slug}: success; token saved ({mask(saved['access_token'])}, "
            f"refresh={'yes' if saved.get('refresh_token') else 'no'})")
        discovery = oauth_post_login_discover(slug) if discover else {}
        return {"ok": True, "slug": slug, "access_token": mask(saved["access_token"]),
                "refresh": bool(saved.get("refresh_token")), "expires_at": exp_txt,
                "discovery": discovery}
    finally:
        with OAUTH_ACTIVE_LOCK:
            OAUTH_ACTIVE.discard(slug)



# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

BADGE = {"working": "OK", "quota": "QUOTA", "failing": "FAIL", "unknown": "?????"}


def render_status(st: dict, in_hermes_only: bool = False, working_only: bool = False) -> str:
    lines = [f"Connector Hub status - {utcnow()}", ""]
    lines.append(f"policy: free_only={st['policy'].get('free_only')} "
                 f"auto_add_working={st['policy'].get('auto_add_working')} "
                 f"lenient_failures={st['policy'].get('lenient_failures')} "
                 f"touch_default={st['policy'].get('touch_default')}")
    lines.append("")
    for slug, p in st["providers"].items():
        key_disp = provider_bearer(slug, p)[1]
        extra = ""
        if p.get("monitor_only"):
            extra = "  [monitor-only: no hermes_url]"
        elif p.get("hermes_url"):
            extra = f"  [hermes via {p['hermes_url']}]"
        lines.append(f"[{slug}]  {p.get('name')}  {p['url']}  key={key_disp}  auth={p.get('auth')}{extra}")
        for mid, rec in sorted(p["models"].items()):
            if in_hermes_only and not rec.get("in_hermes"):
                continue
            if working_only and rec.get("status") != "working":
                continue
            b = BADGE.get(rec.get("status", "unknown"), "?????")
            ms = (rec.get("reply") or {}).get("ms")
            ms_s = f"{ms}ms" if isinstance(ms, int) else "-"
            hermes = "in-hermes" if rec.get("in_hermes") else "not-in-hermes"
            free = "free" if rec.get("free") else "PAID"
            note = ""
            if rec.get("status") == "failing":
                err = (rec.get("reply") or {}).get("error") or (rec.get("reply") or {}).get("note") or ""
                if err:
                    note = f"  ({str(err)[:60]})"
            lines.append(f"   [{b:^5}] {ms_s:>7}  {free:<4}  {hermes:<13} {mid}{note}")
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Keep-or-delete prompt (rule 4A)
# --------------------------------------------------------------------------

def ask_keep_or_delete(st: dict, slug: str, assume_yes: bool) -> str:
    if assume_yes:
        return "keep"
    try:
        ans = input(f"Keep provider '{slug}' in Hermes? [Y/n] ").strip().lower()
    except EOFError:
        ans = "y"
    return "keep" if ans in ("", "y", "yes") else "delete"


# --------------------------------------------------------------------------
# CLI commands
# --------------------------------------------------------------------------

def cmd_seed(args) -> int:
    st = state_load()
    for slug, meta in SEED_PROVIDERS.items():
        ensure_provider(st, slug, meta["name"], meta["url"], meta["key_env"],
                        auth=meta["auth"], fallback_models=meta["fallback_models"],
                        key_env_added_by_connector=meta["key_env_added_by_connector"],
                        free_override=meta.get("free_override"))
        log(f"registered {slug} ({meta['name']})")
    state_save(st)
    log("seed complete; run `test --all` to probe and sync into Hermes")
    return 0


def cmd_test(args) -> int:
    st = state_load()
    if args.target and args.target in st["providers"]:
        sweep(st, only=[args.target], do_tools=not args.no_tools, deep=args.all, force=args.force)
    elif args.all or not args.target:
        sweep(st, do_tools=not args.no_tools, deep=args.all, force=args.force)
    else:
        # target is a model id: find its provider
        hit = None
        for slug, p in st["providers"].items():
            if args.target in p["models"]:
                hit = slug
                break
        if not hit:
            print(f"unknown provider or model: {args.target}", file=sys.stderr)
            return 2
        res = probe_one_model(st, hit, args.target, do_tools=not args.no_tools)
        print(json.dumps(res, indent=2))
        return 0 if "error" not in res else 2
    print(render_status(st))
    return 0


def cmd_status(args) -> int:
    st = state_load()
    print(render_status(st, in_hermes_only=args.in_hermes, working_only=args.working))
    return 0


def cmd_list(args) -> int:
    st = state_load()
    for slug, p in sorted(st["providers"].items()):
        for mid, rec in sorted(p["models"].items()):
            if args.free_only and not rec.get("free"):
                continue
            print(f"{slug}/{mid}" if not mid.startswith(slug + "/") else mid)
    return 0


def cmd_retry(args) -> int:
    st = state_load()
    slug = None
    for s, p in st["providers"].items():
        if args.model in p["models"]:
            slug = s
            break
    if not slug:
        print(f"unknown model: {args.model}", file=sys.stderr)
        return 2
    res = probe_one_model(st, slug, args.model)
    print(json.dumps(res, indent=2))
    return 0 if "error" not in res else 2


def purge_provider_files(slug: str, p: dict) -> None:
    """Remove env keys + oauth token file that belong to a provider being deleted."""
    if p.get("key_env_added_by_connector") and p.get("key_env"):
        env_remove(p["key_env"])
    if p.get("hermes_key_env_added_by_connector") and p.get("hermes_key_env"):
        env_remove(p["hermes_key_env"])
    token_clear(slug)


def reconcile_models(st: dict, target: str | None = None) -> list[str]:
    """Reconcile live model catalogs into state + Hermes config.
    Caller holds state_lock and saves state. Returns human-readable report."""
    report: list[str] = []
    for slug, p in list(st["providers"].items()):
        if target and slug != target:
            continue
        auth = p.get("auth", "static")
        key, key_disp = provider_bearer(slug, p)
        if auth != "static" and not key:
            report.append(f"{slug}: skipped (no credential: {key_disp})")
            continue
        url = p.get("hermes_url") or p["url"]
        items, source = fetch_model_ids(url, key)
        if source != "api" or not items:
            report.append(f"{slug}: skipped (model fetch failed; kept current models)")
            continue
        live = {str(it["id"]) for it in items if isinstance(it, dict) and it.get("id")}
        prev: dict = p.get("models", {}) or {}
        override = set(p.get("free_override") or [])
        policy = st["policy"]
        added: list[str] = []
        removed: list[str] = []
        for it in items:
            mid = str(it.get("id") or "")
            if not mid:
                continue
            if mid in prev:
                prev[mid]["free"] = (not policy.get("free_only")) or \
                    classify_free(it, mid) or (mid in override)
                continue
            is_free = (not policy.get("free_only")) or \
                classify_free(it, mid) or (mid in override)
            prev[mid] = {"free": is_free, "status": "unknown", "in_hermes": False,
                         "reply": None, "tool": None, "added_by_refresh": utcnow()}
            added.append(mid)
        for mid, rec in list(prev.items()):
            if mid in live or not isinstance(rec, dict) or rec.get("removed_upstream"):
                continue
            rec["removed_upstream"] = utcnow()
            rec["status"] = "failing"
            rec["in_hermes"] = False
            rec["error"] = "model no longer available upstream"
            removed.append(mid)
        p["models"] = prev
        p["model_source"] = "api"
        sync = sync_provider_to_hermes(st, slug)
        log(f"refresh-models {slug}: +{len(added)} -{len(removed)} "
            f"(sync: {sync.get('action', 'n/a')})")
        report.append(f"{slug}: added={added or 'none'} removed={removed or 'none'} "
                      f"total={len(prev)} hermes={sync.get('action','n/a')}")
    st["last_model_refresh"] = utcnow()
    return report


def cmd_refresh_models(args) -> int:
    """CLI wrapper around reconcile_models (re-fetch live model catalogs)."""
    with state_lock():
        st = state_load()
        report = reconcile_models(st, getattr(args, "provider", None))
        state_save(st)
    for line in report:
        print(line)
    return 0


def cmd_add(args) -> int:
    if args.auth == "static" and not args.key:
        print("static providers need --key", file=sys.stderr)
        return 2
    oauth = None
    if args.oauth_config:
        try:
            oauth = json.loads(args.oauth_config)
            if not isinstance(oauth, dict):
                raise ValueError("must be a JSON object")
        except Exception as e:
            print(f"--oauth-config is not valid JSON: {e}", file=sys.stderr)
            return 2
    st = state_load()
    result = add_provider_core(st, url=args.url, key=args.key, slug=args.name,
                               display=args.display,
                               models=(args.models.split(",") if args.models else None),
                               do_tools=not args.no_tools, auth=args.auth, oauth=oauth,
                               token_store=args.token_store,
                               hermes_url=args.hermes_url, hermes_key=args.hermes_key)
    if "error" in result:
        print(result["error"], file=sys.stderr)
        return 2
    st = state_load()
    print(render_status(st))
    decision = ask_keep_or_delete(st, args.name, assume_yes=args.yes)
    if decision == "delete":
        backup = yaml_remove_provider(args.name)
        p = st["providers"][args.name]
        purge_provider_files(args.name, p)
        del st["providers"][args.name]
        state_save(st)
        log(f"provider {args.name} deleted (backup: {backup or 'n/a'})")
        print(f"deleted {args.name}")
    else:
        with state_lock():
            st = state_load()
            st["providers"][args.name]["pending_decision"] = False
            state_save(st)
        print(f"kept {args.name}")
    return 0


def cmd_remove(args) -> int:
    st = state_load()
    if args.provider not in st["providers"]:
        print(f"unknown provider: {args.provider}", file=sys.stderr)
        return 2
    backup = yaml_remove_provider(args.provider)
    p = st["providers"][args.provider]
    purge_provider_files(args.provider, p)
    del st["providers"][args.provider]
    state_save(st)
    print(f"removed {args.provider} (config backup: {backup or 'not present in config'})")
    return 0


def cmd_restart_gateway(args) -> int:
    try:
        out = subprocess.run(["hermes", "gateway", "restart"], capture_output=True,
                             text=True, timeout=120, shell=True)
        print((out.stdout or "") + (out.stderr or ""))
        return out.returncode
    except Exception as e:
        print(f"gateway restart failed: {e}", file=sys.stderr)
        return 1


def cmd_oauth_login(args) -> int:
    """Device-flow OAuth login: shows the user code, opens the browser, polls,
    then re-discovers models and deep-probes the provider."""
    print(f"starting device-flow login for {args.slug} ...")
    res = oauth_login_core(args.slug, timeout_s=args.timeout, open_browser=not args.no_browser)
    print(json.dumps(res, indent=2))
    if "error" in res:
        return 2
    st = state_load()
    print(render_status(st))
    return 0


def cmd_token_import(args) -> int:
    st = state_load()
    p = st["providers"].get(args.slug)
    if p is None:
        print(f"unknown provider: {args.slug}", file=sys.stderr)
        return 2
    if p.get("auth") != "oauth":
        print(f"provider {args.slug} auth is '{p.get('auth')}' - token import applies to "
              "oauth providers (re-add with --auth oauth)", file=sys.stderr)
        return 2
    try:
        with open(args.file, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        print(f"cannot read {args.file}: {e}", file=sys.stderr)
        return 2
    if isinstance(raw, dict) and isinstance(raw.get("tokens"), list) and raw["tokens"]:
        raw = raw["tokens"][0]
    if not isinstance(raw, dict):
        print("unexpected token file format", file=sys.stderr)
        return 2
    tok = oauth_normalize_token(raw)
    if not tok.get("access_token") and not tok.get("refresh_token"):
        print("token file has no access_token/accessToken or refresh_token", file=sys.stderr)
        return 2
    token_save(args.slug, tok)
    oauth_progress_set(args.slug, status="done", message=f"token imported from {args.file}")
    log(f"token import {args.slug}: saved ({mask(tok['access_token'])}, "
        f"refresh={'yes' if tok.get('refresh_token') else 'no'})")
    disc = oauth_post_login_discover(args.slug)
    print(f"imported token for {args.slug} ({mask(tok['access_token'])}); "
          f"discovery: {disc.get('models', '?')} models, working={len(disc.get('working', []))}")
    return 0


# --------------------------------------------------------------------------
# Auto-start (logon, no admin required: HKCU Run entries + hidden launcher)
# --------------------------------------------------------------------------

AUTOSTART_DIR = os.path.join(CONNECTOR_DIR, "autostart")
AUTOSTART_RUN_KEY = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_TASKS = [
    ("Hermes_ConnectorHub", "connector-hub.cmd"),
    ("Hermes_ClineBridge", "cline-bridge.cmd"),
    ("Hermes_OpenCodeBridge", "opencode-bridge.cmd"),
    ("Hermes_AntigravityBridge", "antigravity-bridge.cmd"),
    ("Hermes_GeminiBridge", "gemini-bridge.cmd"),
    ("Hermes_ZhipuBridge", "zhipu-bridge.cmd"),
    ("Hermes_NvidiaBridge", "nvidia-bridge.cmd"),
    ("Hermes_OpenRouterBridge", "openrouter-bridge.cmd"),
]


def autostart_write_wrappers() -> None:
    """Regenerate the wrapper .cmd files + hidden launcher (absolute paths only)."""
    os.makedirs(AUTOSTART_DIR, exist_ok=True)
    py = sys.executable
    connector_py = os.path.abspath(__file__)
    wrappers = {
        "connector-hub.cmd": (
            "@echo off\r\nrem autostart: Hermes Connector Hub (dashboard + API, port 8399)\r\n"
            f"cd /d \"{CONNECTOR_DIR}\"\r\n"
            f"\"{py}\" \"{connector_py}\" serve\r\n"
        ),
        "cline-bridge.cmd": (
            "@echo off\r\nrem autostart: Cline bridge proxy (port 8378)\r\n"
            f"cd /d \"{os.path.join(HERMES_HOME, 'cline-bridge', 'proxy')}\"\r\n"
            f"\"{py}\" proxy.py\r\n"
        ),
        "opencode-bridge.cmd": (
            "@echo off\r\nrem autostart: OpenCode bridge proxy (port 8377)\r\n"
            f"cd /d \"{os.path.join(HERMES_HOME, 'opencode-bridge', 'proxy')}\"\r\n"
            f"\"{py}\" proxy.py\r\n"
        ),
        "antigravity-bridge.cmd": (
            "@echo off\r\nrem autostart: Antigravity bridge proxy (port 8379)\r\n"
            f"cd /d \"{os.path.join(HERMES_HOME, 'antigravity-bridge', 'proxy')}\"\r\n"
            "call start-proxy.cmd\r\n"
        ),
        "gemini-bridge.cmd": (
            "@echo off\r\nrem autostart: Gemini bridge proxy (port 8380)\r\n"
            f"cd /d \"{os.path.join(HERMES_HOME, 'gemini-bridge', 'proxy')}\"\r\n"
            "call start-proxy.cmd\r\n"
        ),
        "zhipu-bridge.cmd": (
            "@echo off\r\nrem autostart: Zhipu GLM bridge proxy (port 8381)\r\n"
            f"cd /d \"{os.path.join(HERMES_HOME, 'zhipu-bridge', 'proxy')}\"\r\n"
            "call start-proxy.cmd\r\n"
        ),
        "nvidia-bridge.cmd": (
            "@echo off\r\nrem autostart: NVIDIA NIM bridge proxy (port 8382)\r\n"
            f"cd /d \"{os.path.join(HERMES_HOME, 'nvidia-bridge', 'proxy')}\"\r\n"
            "call start-proxy.cmd\r\n"
        ),
        "openrouter-bridge.cmd": (
            "@echo off\r\nrem autostart: OpenRouter bridge proxy (port 8383)\r\n"
            f"cd /d \"{os.path.join(HERMES_HOME, 'openrouter-bridge', 'proxy')}\"\r\n"
            "call start-proxy.cmd\r\n"
        ),
    }
    for fname, content in wrappers.items():
        with open(os.path.join(AUTOSTART_DIR, fname), "w", encoding="utf-8") as f:
            f.write(content)
    vbs = (
        "' runs a command hidden (used by the HKCU Run autostart entries)\r\n"
        "Set sh = CreateObject(\"WScript.Shell\")\r\n"
        "args = \"\"\r\n"
        "For Each a In WScript.Arguments\r\n"
        "    args = args & \" \" & Chr(34) & a & Chr(34)\r\n"
        "Next\r\n"
        "sh.Run \"cmd /c\" & args, 0, False\r\n"
    )
    with open(os.path.join(AUTOSTART_DIR, "run-hidden.vbs"), "w", encoding="utf-8") as f:
        f.write(vbs)


def _run_entry_command(fname: str) -> str:
    vbs = os.path.join(AUTOSTART_DIR, "run-hidden.vbs")
    return f"wscript.exe \"{vbs}\" \"{os.path.join(AUTOSTART_DIR, fname)}\""


def cmd_autostart(args) -> int:
    autostart_write_wrappers()
    if args.status:
        for name, _fname in AUTOSTART_TASKS:
            out = subprocess.run(["reg", "query", AUTOSTART_RUN_KEY, "/v", name],
                                 capture_output=True, text=True)
            print(f"{name}: {'installed' if name in (out.stdout or '') else 'not installed'}")
        return 0
    if args.off:
        for name, _fname in AUTOSTART_TASKS:
            out = subprocess.run(["reg", "delete", AUTOSTART_RUN_KEY, "/v", name, "/f"],
                                 capture_output=True, text=True)
            print(f"{name}: {'removed' if out.returncode == 0 else 'was not installed'}")
        log("autostart: HKCU Run entries removed")
        return 0
    ok = True
    for name, fname in AUTOSTART_TASKS:
        cmd = _run_entry_command(fname)
        out = subprocess.run(["reg", "add", AUTOSTART_RUN_KEY, "/v", name, "/t", "REG_SZ",
                              "/d", cmd, "/f"], capture_output=True, text=True)
        if out.returncode != 0:
            ok = False
            print(f"{name}: FAILED - {(out.stderr or out.stdout or '').strip()}",
                  file=sys.stderr)
        else:
            print(f"{name}: installed (HKCU Run, hidden) -> {fname}")
    log("autostart: HKCU Run entries installed" if ok else "autostart: install had failures")
    return 0 if ok else 2


DASHBOARD_HTML = """
<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Hermes Connector Hub</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{
 --ink:#0a0c0c; --panel:#121616; --panel2:#171c1b; --line:#28302e; --line2:#3a4542;
 --text:#e9e5d9; --dim:#8d9791; --amber:#ffb454; --amber2:#ff8a3d;
 --mint:#57d6a3; --coral:#ff6b5e; --ice:#7fd4e8; --mono:ui-monospace,"SFMono-Regular","Cascadia Code",Consolas,"Liberation Mono",monospace
}
*{box-sizing:border-box}
html,body{margin:0;min-height:100%}
body{
 background:
  repeating-linear-gradient(0deg,transparent 0 39px,rgba(255,255,255,.018) 39px 40px),
  repeating-linear-gradient(90deg,transparent 0 39px,rgba(255,255,255,.018) 39px 40px),
  radial-gradient(900px 520px at 8% -8%,rgba(255,180,84,.10),transparent 62%),
  linear-gradient(180deg,#0e1111,var(--ink) 55%);
 color:var(--text);font:13px/1.55 var(--mono);letter-spacing:.1px;
}
body:before{content:"";position:fixed;inset:0;pointer-events:none;z-index:9;opacity:.16;
 background-image:radial-gradient(rgba(255,255,255,.22) .5px,transparent .5px);background-size:3px 3px}
header{
 position:sticky;top:0;z-index:8;padding:13px 22px;display:flex;gap:16px;align-items:center;flex-wrap:wrap;
 background:rgba(10,12,12,.92);backdrop-filter:blur(14px);border-bottom:1px solid var(--line2);
 box-shadow:0 8px 30px rgba(0,0,0,.35)
}
.mark{width:32px;height:32px;display:grid;place-items:center;border:1px solid var(--amber);color:var(--amber);
 font-size:15px;font-weight:800;box-shadow:inset 0 0 12px rgba(255,180,84,.12),0 0 14px rgba(255,180,84,.1)}
h1{font-size:15px;margin:0;font-weight:800;letter-spacing:2.4px;text-transform:uppercase;white-space:nowrap}
h1 span{color:var(--amber)}
.tag{font-size:10px;letter-spacing:1.6px;text-transform:uppercase;color:var(--dim);border-left:1px solid var(--line2);padding-left:14px}
.engine{font-size:11px;color:var(--dim);letter-spacing:.6px}
header .spacer{flex:1}
button{
 background:var(--panel2);border:1px solid var(--line2);color:var(--text);border-radius:3px;
 padding:8px 13px;cursor:pointer;font:11px var(--mono);font-weight:700;letter-spacing:1.1px;text-transform:uppercase;
 transition:background .15s,border-color .15s,transform .12s
}
button:hover{border-color:var(--amber);background:#202725;transform:translateY(-1px)}
button.primary{background:var(--amber);border-color:var(--amber);color:#17120a}
button.primary:hover{background:var(--amber2);border-color:var(--amber2)}
button.ghost{border-color:transparent;color:var(--dim)}
button:disabled{opacity:.35;cursor:not-allowed;transform:none}
input,select,textarea{
 background:#0b0e0e;border:1px solid var(--line2);border-radius:3px;color:var(--text);
 padding:8px 10px;font:12px var(--mono);outline:none
}
input:focus,select:focus,textarea:focus{border-color:var(--amber);box-shadow:0 0 0 2px rgba(255,180,84,.1)}
main{padding:20px 22px 64px;max-width:1540px;margin:0 auto;display:grid;gap:14px}
.card{position:relative;background:linear-gradient(180deg,var(--panel2),var(--panel));border:1px solid var(--line);border-radius:4px;padding:16px 18px}
.card:before{content:"";position:absolute;left:0;top:0;width:34px;height:2px;background:var(--amber)}
.card.alt:before{background:var(--ice)}
.card.good:before{background:var(--mint)}
.card h2{margin:0 0 12px;font-size:11px;letter-spacing:2px;text-transform:uppercase;color:var(--dim);font-weight:700}
.muted{color:var(--dim)}
.mono{font-family:var(--mono)}
.led{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:8px;vertical-align:0}
.led.ok{background:var(--mint);box-shadow:0 0 10px var(--mint)}
.led.warn{background:var(--amber);box-shadow:0 0 10px var(--amber)}
.led.bad{background:var(--coral);box-shadow:0 0 10px var(--coral)}
.led.idle{background:#59635f}
.badge{display:inline-block;padding:2px 8px;border-radius:2px;font-size:10px;font-weight:800;letter-spacing:1px;text-transform:uppercase;border:1px solid}
.b-ok{color:var(--mint);border-color:rgba(87,214,163,.4);background:rgba(87,214,163,.08)}
.b-quota{color:var(--amber);border-color:rgba(255,180,84,.4);background:rgba(255,180,84,.08)}
.b-fail{color:var(--coral);border-color:rgba(255,107,94,.4);background:rgba(255,107,94,.08)}
.b-unk{color:var(--dim);border-color:var(--line2);background:rgba(255,255,255,.02)}
.grid-stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(122px,1fr));gap:10px}
.stat{border:1px solid var(--line);background:rgba(255,255,255,.02);padding:11px 13px}
.stat .n{font-size:28px;font-weight:800;line-height:1;font-variant-numeric:tabular-nums}
.stat .l{font-size:9px;letter-spacing:1.5px;text-transform:uppercase;color:var(--dim);margin-top:6px}
.stat.ok .n{color:var(--mint)}.stat.q .n{color:var(--amber)}.stat.f .n{color:var(--coral)}
.hero{background:linear-gradient(115deg,rgba(255,180,84,.06),rgba(127,212,232,.03));border-color:#3a3a2c}
.hero:before{width:80px}
.linkrow{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:14px}
.linknode{border:1px solid var(--line2);padding:7px 12px;font-size:10px;letter-spacing:1.3px;text-transform:uppercase;background:rgba(0,0,0,.25)}
.linknode b{color:var(--amber);font-weight:800}
.linkline{flex:1;min-width:30px;height:1px;background:repeating-linear-gradient(90deg,var(--line2) 0 6px,transparent 6px 11px);position:relative}
.linkline:after{content:"";position:absolute;top:-2px;left:0;width:5px;height:5px;border-radius:50%;background:var(--amber);box-shadow:0 0 10px var(--amber);animation:travel 2.4s linear infinite}
@keyframes travel{0%{left:0;opacity:0}10%{opacity:1}90%{opacity:1}100%{left:100%;opacity:0}}
.hchecks{display:flex;gap:10px;flex-wrap:wrap;margin-top:12px}
.hcheck{border:1px solid var(--line);background:rgba(0,0,0,.22);padding:8px 11px;font-size:11px;min-width:140px}
.hcheck .k{font-size:9px;letter-spacing:1.3px;text-transform:uppercase;color:var(--dim)}
.hcheck .v{margin-top:3px;font-weight:700}
.verify-out{margin-top:12px;border-left:2px solid var(--mint);padding:8px 12px;background:rgba(87,214,163,.05);font-size:11px;white-space:pre-wrap;word-break:break-word;max-height:110px;overflow:auto}
.verify-out.fail{border-color:var(--coral);background:rgba(255,107,94,.05)}
.bridge-strip{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}
.bchip{border:1px solid var(--line);padding:5px 9px;font-size:10px;letter-spacing:.5px;background:rgba(0,0,0,.22)}
.bchip.up{border-color:rgba(87,214,163,.35)}
.bchip.down{border-color:rgba(255,107,94,.5);color:var(--coral)}
.chain{display:flex;align-items:stretch;gap:5px;flex-wrap:wrap;margin-top:12px}
.node{border:1px solid var(--line2);background:rgba(0,0,0,.25);padding:9px 11px;min-width:132px;max-width:190px}
.node.live{border-color:rgba(87,214,163,.5);box-shadow:inset 0 0 18px rgba(87,214,163,.05)}
.node.skip{opacity:.38}
.node .np{font-size:9px;letter-spacing:1.4px;text-transform:uppercase;color:var(--dim)}
.node .nm{font-size:11px;color:#cfe0dc;margin-top:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.node .st{margin-top:7px}
.arrow{align-self:center;color:var(--amber);font-size:15px;animation:fade 1.7s ease-in-out infinite}
@keyframes fade{0%,100%{opacity:.3}50%{opacity:1}}
.combo-head{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
.strats{display:flex;gap:5px;flex-wrap:wrap}
.strats button{font-size:9px;padding:5px 9px}
.toolbar{display:flex;gap:16px;align-items:center;flex-wrap:wrap}
.toolbar label{font-size:11px;letter-spacing:.5px;color:var(--dim);cursor:pointer}
table{width:100%;border-collapse:collapse;margin-top:10px}
th,td{text-align:left;padding:8px 9px;border-bottom:1px solid var(--line);font-size:12px;vertical-align:middle}
th{font-size:9px;letter-spacing:1.4px;text-transform:uppercase;color:var(--dim);font-weight:700;white-space:nowrap}
tr:hover td{background:rgba(255,255,255,.018)}
code{background:rgba(0,0,0,.35);border:1px solid var(--line);padding:2px 5px;color:#bcd6cf;font-size:11px}
.err{margin-top:4px;font-size:10px;color:var(--coral);white-space:normal;max-width:330px}
.pid{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}
.pid .nm{font-size:14px;font-weight:800;letter-spacing:.4px}
.pid .meta{font-size:11px;color:var(--dim);margin-top:4px}
form{display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));margin-top:12px}
label.f{display:block;font-size:9px;letter-spacing:1.3px;text-transform:uppercase;color:var(--dim);margin-bottom:4px}
.pending{border:1px solid var(--amber);background:rgba(255,180,84,.06);padding:10px 12px;margin-top:12px;display:flex;gap:9px;align-items:center;flex-wrap:wrap}
#log{font-size:11px;color:var(--dim);max-height:210px;overflow:auto;white-space:pre-wrap;margin-top:8px}
.spin{display:inline-block;width:9px;height:9px;border:2px solid var(--amber);border-top-color:transparent;border-radius:50%;animation:sp .8s linear infinite;margin-right:7px}
@keyframes sp{to{transform:rotate(360deg)}}
footer{padding:10px 22px 26px;text-align:center;font-size:9px;letter-spacing:1.4px;text-transform:uppercase;color:#5d6863}
footer a{color:#7e8a84;text-decoration:none;border-bottom:1px dotted #5d6863}
footer a:hover{color:var(--amber);border-color:var(--amber)}
@media(max-width:700px){header{padding:11px 14px}h1{font-size:12px}main{padding:14px}.tag{display:none}button{padding:7px 9px;font-size:10px}}
</style></head>
<body>
<header>
 <div class="mark">H</div>
 <h1>Hermes <span>//</span> Connector Hub</h1>
 <div class="tag">telemetry console</div>
 <div class="engine" id="engine"></div>
 <div class="spacer"></div>
 <button class="primary" onclick="testAll()">Test all models</button>
 <button onclick="sweep(false,true)">Sentinel</button>
 <button onclick="sweep(true,true)">Deep sweep</button>
 <button onclick="verifyNow()">Verify Hermes</button>
 <button onclick="restartGateway()">Restart gateway</button>
</header>
<main>

 <section class="card good">
  <div class="combo-head"><h2 style="margin:0">Hermes connection</h2><span class="muted" id="hermesHead">checking...</span></div>
  <div class="linkrow">
   <div class="linknode"><b>HERMES</b> gateway</div><div class="linkline"></div>
   <div class="linknode">Connector <b>HUB</b></div><div class="linkline"></div>
   <div class="linknode"><b>BRIDGES</b></div><div class="linkline"></div>
   <div class="linknode"><b>FREE MODELS</b></div>
  </div>
  <div class="hchecks" id="hchecks"></div>
  <div class="bridge-strip" id="bridgeStrip"></div>
  <div class="verify-out" id="verifyOut" hidden></div>
 </section>

 <section class="card hero">
  <h2>Live model telemetry</h2>
  <div class="grid-stats" id="stats"></div>
 </section>

 <section class="card alt">
  <div class="combo-head"><h2 style="margin:0">Auto combo · failover chain</h2><code>auto-free  /  http://127.0.0.1:8399/auto/v1</code></div>
  <div class="chain" id="chain"></div>
  <div class="muted" style="font-size:10px;margin-top:10px">Requests are scored and sent to the healthiest model; quota or failure advances to the next node.</div>
 </section>

 <section class="card">
  <div class="combo-head"><h2 style="margin:0">Routing brain</h2><div class="strats" id="strats"></div></div>
  <div id="routingBody"></div>
 </section>

 <section class="card">
  <div class="toolbar">
   <label><input type="checkbox" id="fFree" checked onchange="render()"> Free only</label>
   <label><input type="checkbox" id="fHermes" onchange="render()"> In Hermes only</label>
   <label><input type="checkbox" id="fWork" onchange="render()"> Working only</label>
   <span class="muted" id="updated"></span>
  </div>
 </section>

 <section class="card">
  <h2>Register provider</h2>
  <span class="muted">OpenAI-compatible endpoint + key. Keys stay in Hermes .env. Free models only.</span>
  <form onsubmit="return addProvider(event)">
   <div><label class="f">Base URL</label><input id="aUrl" placeholder="https://api.example.com/v1" style="width:100%" required></div>
   <div><label class="f">Auth</label>
    <select id="aAuth" onchange="authChanged()" style="width:100%">
     <option value="static">API key (static)</option>
     <option value="oauth">OAuth device flow</option>
     <option value="oauth_store">OAuth token store</option>
    </select></div>
   <div><label class="f" id="aKeyLbl">API key</label><input id="aKey" type="password" placeholder="sk-..." style="width:100%"></div>
   <div><label class="f">Slug</label><input id="aName" placeholder="my-provider" style="width:100%" required></div>
   <div style="grid-column:1/-1" id="aOauthRow" hidden><label class="f">OAuth config JSON</label><textarea id="aOauth" rows="3" style="width:100%"></textarea></div>
   <div style="grid-column:1/-1" id="aStoreRow" hidden><label class="f">Token store path</label><input id="aStore" style="width:100%"></div>
   <div style="grid-column:1/-1" id="aHermesRow" hidden><label class="f">Hermes endpoint</label><input id="aHermesUrl" placeholder="http://127.0.0.1:8380/v1" style="width:100%"></div>
   <div><label class="f">Model ids (optional)</label><input id="aModels" placeholder="free-model-1,free-model-2" style="width:100%"></div>
   <div style="grid-column:1/-1"><button class="primary" type="submit">Add &amp; probe</button> <span class="muted" id="addmsg"></span></div>
  </form>
 </section>

 <div id="providers"></div>

 <section class="card">
  <h2>Activity log</h2><div id="log" class="muted"></div>
 </section>
</main>
<footer><a href="https://deerflow.tech" target="_blank" rel="noopener">Created By Deerflow</a></footer>
<script>
let S=null,H=null;
async function j(u,o){const r=await fetch(u,o);let d={};try{d=await r.json()}catch(e){}if(!r.ok)throw new Error(d.error||('HTTP '+r.status));return d}
async function post(u,b){return j(u,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(b||{})})}
function esc(s){return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;')}
function ago(t){if(!t)return '-';const d=Date.parse(t);if(isNaN(d))return t;const s=Math.floor((Date.now()-d)/1000);if(s<60)return s+'s ago';if(s<3600)return Math.floor(s/60)+'m ago';if(s<86400)return Math.floor(s/3600)+'h ago';return Math.floor(s/86400)+'d ago'}
async function refresh(){
 try{
  S=await j('/status');
  try{H=await j('/api/hermes')}catch(e){}
  let L={lines:''};try{L=await j('/api/logs')}catch(e){}
  let R={};try{R=await j('/api/routing')}catch(e){}
  render(L.lines,R,H);
 }catch(e){document.getElementById('updated').textContent='error: '+e.message}
}
function verifyNow(){post('/api/hermes/verify').then(()=>{if(H)H.verify={state:'running',at:new Date().toISOString()};render();setTimeout(refresh,2500)}).catch(e=>alert(e.message))}
function setStrategy(s){post('/api/routing',{strategy:s}).then(refresh).catch(e=>alert(e.message))}
function sweep(deep,force){if(deep&&!confirm('Deep sweep probes EVERY free model and consumes provider quota. Continue?'))return;post('/api/sweep',{deep:deep,force:force}).then(refresh).catch(e=>alert(e.message))}
function testAll(){post('/api/sweep',{deep:true,force:true}).then(()=>{if(S&&S.engine){S.engine.busy=true;S.engine.current='all providers'}render()}).catch(e=>alert(e.message))}
function restartGateway(){if(!confirm('Restart the Hermes gateway now?'))return;post('/api/restart-gateway').then(()=>alert('Gateway restart started')).catch(e=>alert(e.message))}
function retryModel(m){post('/api/retry',{model:m}).then(refresh).catch(e=>alert(e.message))}
function removeProvider(s){if(!confirm('Remove provider '+s+' from Hermes and connector state?'))return;post('/api/remove',{slug:s}).then(refresh).catch(e=>alert(e.message))}
function decide(s,d){post('/api/decide',{slug:s,decision:d}).then(refresh).catch(e=>alert(e.message))}
function startOauth(s){addmsg.textContent='oauth login started for '+s;post('/api/oauth-login',{slug:s}).then(()=>{addmsg.textContent='login running - check the provider card';refresh()}).catch(e=>{addmsg.textContent='';alert(e.message)})}
function authChanged(){const m=aAuth.value;aOauthRow.hidden=m!=='oauth';aStoreRow.hidden=m!=='oauth_store';aHermesRow.hidden=m==='static';aKeyLbl.textContent=m==='static'?'API key':(m==='oauth'?'API key (optional)':'Bridge key (optional)')}
function addProvider(ev){ev.preventDefault();const b={url:aUrl.value.trim(),key:aKey.value.trim()||null,name:aName.value.trim(),models:aModels.value.trim()||null,auth:aAuth.value,token_store:aStore.value.trim()||null,hermes_url:aHermesUrl.value.trim()||null};
 if(aAuth.value==='oauth'){const c=aOauth.value.trim();if(!c){addmsg.textContent='oauth config JSON required';return false}try{b.oauth_config=JSON.parse(c)}catch(e){addmsg.textContent='invalid oauth JSON';return false}}
 if(aAuth.value==='static'&&!b.key){addmsg.textContent='API key required';return false}
 addmsg.textContent='registering + probing...';post('/api/add',b).then(()=>{addmsg.textContent='added - probing';refresh()}).catch(e=>{addmsg.textContent='';alert(e.message)});return false}

function renderHermes(){
 const box=document.getElementById('hchecks'),strip=document.getElementById('bridgeStrip'),vout=document.getElementById('verifyOut');
 if(!H){document.getElementById('hermesHead').textContent='checking...';return}
 const g=H.gateway||{},bridges=H.bridges||[],cfg=H.config_providers||[];
 const up=g.up,alive=bridges.filter(x=>x.alive).length,all=bridges.length;
 const ok=up&&alive===all&&(H.verify?.state!=='fail');
 document.getElementById('hermesHead').innerHTML='<span class="led '+(ok?'ok':'warn')+'"></span>'+(ok?'LINK ACTIVE':'CHECK LINK');
 box.innerHTML=
  '<div class="hcheck"><div class="k">Gateway process</div><div class="v"><span class="led '+(up?'ok':'bad')+'"></span>'+(up?'UP · PID '+esc(g.pid):'DOWN')+'</div></div>'+
  '<div class="hcheck"><div class="k">Bridges online</div><div class="v"><span class="led '+(alive===all?'ok':'warn')+'"></span>'+alive+' / '+all+'</div></div>'+
  '<div class="hcheck"><div class="k">Hermes config</div><div class="v"><span class="led '+(cfg.length>=4?'ok':'warn')+'"></span>'+cfg.length+' providers synced</div></div>'+
  '<div class="hcheck"><div class="k">Global default</div><div class="v"><span class="led ok"></span>'+esc(H.default_global||'unchanged')+'</div></div>';
 strip.innerHTML=bridges.map(x=>'<span class="bchip '+(x.alive?'up':'down')+'"><span class="led '+(x.alive?'ok':'bad')+'"></span>'+esc(x.slug)+' · '+x.models+'</span>').join('');
 const v=H.verify||{};
 if(v.state&&v.state!=='never'){
  vout.hidden=false;vout.className='verify-out'+(v.state==='fail'?' fail':'');
  vout.innerHTML='<b>E2E verify · '+esc(v.state.toUpperCase())+'</b> '+(v.secs!=null?v.secs+'s':'')+' · '+ago(v.at)+'\\n'+esc(v.output||'no output');
 }else{vout.hidden=true}
}
function render(logText,R,H){
 R=R||{};renderHermes();
 document.getElementById('updated').textContent='updated '+new Date().toLocaleTimeString()+(S&&S.last_model_refresh?(' · catalog '+ago(S.last_model_refresh)):'');
 document.getElementById('engine').innerHTML=(S&&S.engine&&S.engine.busy)?('<span class="spin"></span>'+esc(S.engine.current||'working')):'idle';
 const showFree=fFree.checked,showHermes=fHermes.checked,showWork=fWork.checked;
 // stat strip
 let tot={ok:0,quota:0,fail:0,unknown:0,total:0};
 for(const c of Object.values((S&&S.summary)||{})){tot.ok+=c.ok;tot.quota+=c.quota;tot.fail+=c.fail;tot.unknown+=c.unknown;tot.total+=c.total}
 document.getElementById('stats').innerHTML=
  '<div class="stat ok"><div class="n">'+tot.ok+'</div><div class="l">Working</div></div>'+
  '<div class="stat q"><div class="n">'+tot.quota+'</div><div class="l">Quota limited</div></div>'+
  '<div class="stat f"><div class="n">'+tot.fail+'</div><div class="l">Failing</div></div>'+
  '<div class="stat"><div class="n">'+tot.unknown+'</div><div class="l">Unknown</div></div>'+
  '<div class="stat"><div class="n">'+tot.total+'</div><div class="l">Total models</div></div>';
 // combo chain
 const chain=(S&&S.combos&&S.combos['free-auto']&&S.combos['free-auto'].chain)||[];
 document.getElementById('chain').innerHTML=chain.map((e,i)=>{
  const p=(S.providers||{})[e.provider]||{},rec=(p.models||{})[e.model]||{};
  const st=rec.status||'unknown';
  const cls=st==='working'?'live':((st==='quota'||st==='failing')?'skip':'');
  const badge=st==='working'?'b-ok':(st==='quota'?'b-quota':(st==='failing'?'b-fail':'b-unk'));
  return (i?'<span class="arrow">›</span>':'')+'<div class="node '+cls+'"><div class="np">'+esc(e.provider)+'</div><div class="nm" title="'+esc(e.model)+'">'+esc(e.model)+'</div><div class="st"><span class="badge '+badge+'">'+esc(st)+'</span></div></div>';
 }).join('')||'<span class="muted">no chain configured</span>';
 // routing brain
 const strat=R.strategy||'adaptive';
 document.getElementById('strats').innerHTML=['adaptive','fastest','priority','round_robin'].map(o=>'<button data-s="'+o+'" class="'+(o===strat?'primary':'')+'" onclick="setStrategy(this.dataset.s)">'+o+'</button>').join('');
 const stats=R.stats||{};
 let rows='';for(const [m,s] of Object.entries(stats)){
  const cb=(s.cooldown_until||0)>Date.now()/1000;
  rows+='<tr><td><code>'+esc(m)+'</code></td><td>'+(s.ok||0)+' / '+(s.fail||0)+'</td><td>'+(s.avg_ms?s.avg_ms+'ms':'-')+'</td><td>'+(cb?'<span class="badge b-fail">cooldown</span>':'<span class="badge b-ok">ready</span>')+'</td></tr>';
 }
 document.getElementById('routingBody').innerHTML='<div class="muted" style="font-size:10px">Strategy '+esc(strat)+' · attempt timeout '+(R.attempt_timeout||35)+'s · '+Object.keys(stats).length+' scored models</div>'+
  (rows?'<table><tr><th>Model</th><th>OK / Fail</th><th>Avg latency</th><th>State</th></tr>'+rows+'</table>':'<div class="muted" style="margin-top:8px;font-size:10px">No routing data yet - send a request to auto.</div>');
 // providers
 let html='';
 for(const [slug,p] of Object.entries((S&&S.providers)||{})){
  const rows=[];
  for(const [mid,rec] of Object.entries(p.models||{})){
   if(showFree&&rec.free===false)continue;
   if(showHermes&&!rec.in_hermes)continue;
   if(showWork&&rec.status!=='working')continue;
   const cls={working:'b-ok',quota:'b-quota',failing:'b-fail'}[rec.status]||'b-unk';
   const ms=(rec.reply&&rec.reply.ms!=null)?rec.reply.ms+'ms':'-';
   const tool=(rec.tool&&rec.tool.ok)?('tools:'+(rec.tool.mode||'ok')):'tools:-';
   const retr=rec.status!=='working'?'<button onclick="retryModel(this.dataset.m)" data-m="'+esc(mid)+'">Retry</button>':'';
   const err=rec.error?('<div class="err">'+esc(String(rec.error).slice(0,150))+'</div>'):'';
   rows.push('<tr><td><code>'+esc(mid)+'</code>'+err+'</td><td><span class="badge '+cls+'">'+esc(rec.status||'unknown')+'</span></td><td>'+esc(ms)+'</td><td>'+esc(tool)+'</td><td>'+ago(rec.last_probed)+'</td><td>'+(rec.in_hermes?'<span class="badge b-ok">in hermes</span>':'&mdash;')+'</td><td>'+retr+'</td></tr>');
  }
  if(!rows.length)continue;
  const pend=p.pending_decision?'<div class="pending"><b>Probe finished.</b> Keep '+esc(slug)+' in Hermes? <button class="ok" onclick="decide(this.dataset.s,\\'keep\\')" data-s="'+esc(slug)+'">Keep</button> <button class="danger" onclick="decide(this.dataset.s,\\'delete\\')" data-s="'+esc(slug)+'">Delete</button></div>':'';
  const oa=p.auth==='oauth'?('<div class="pending"><b>OAuth:</b> '+((p.token&&p.token.present)?'token present':'no token')+' <button onclick="startOauth(this.dataset.s)" data-s="'+esc(slug)+'">Login</button></div>'):'';
  html+='<div class="card"><div class="pid"><div><div class="nm">'+esc(p.name||slug)+' <span class="muted">['+esc(slug)+']</span></div><div class="meta">'+esc(p.url)+' · key '+esc(p.key_masked||'none')+' · '+esc(p.auth||'static')+'</div></div><div><button class="danger" onclick="removeProvider(this.dataset.s)" data-s="'+esc(slug)+'">Remove</button></div></div>'+oa+pend+'<table><tr><th>Model (raw id)</th><th>Status</th><th>Reply</th><th>Tools</th><th>Probed</th><th>Hermes</th><th></th></tr>'+rows.join('')+'</table></div>';
 }
 document.getElementById('providers').innerHTML=html||'<div class="card muted">no providers match the filters</div>';
 if(logText!=null)document.getElementById('log').textContent=logText;
}
refresh();setInterval(refresh,5000);
</script></body></html>
"""


DEFAULT_COMBO = [
    {"provider": "openrouter-free", "model": "openrouter/particle-ai/particle-1:free"},
    {"provider": "openrouter-free", "model": "openrouter/nvidia/nemotron-3.5-lightning:free"},
    {"provider": "gemini-free", "model": "gemini-3.5-flash-lite"},
    {"provider": "nvidia-free", "model": "nvidia/nemotron-3.5-lightning-30b-a3b"},
    {"provider": "gemini-free", "model": "gemini-3.1-flash-lite"},
    {"provider": "zhipu-free", "model": "glm-4.7-flashx"},
    {"provider": "cline-free", "model": "cline-free/nemotron-3.5-super-vl"},
    {"provider": "cline-free", "model": "cline-free/solar-mini4"},
    {"provider": "opencode-free", "model": "opencode/beam-free"},
    {"provider": "opencode-free", "model": "opencode/tidepool-alpha-free"},
    {"provider": "antigravity-free", "model": "gemini-3.8-flash-low"},
    {"provider": "antigravity-free", "model": "claude-sonnet-4-6"},
]


def combo_chain(st: dict) -> list[dict]:
    cfg = (st.get("combos") or {}).get("free-auto") or {}
    return cfg.get("chain") or DEFAULT_COMBO


_ROUTE_CACHE = {"at": 0.0, "st": None}
_ROUTE_LOCK = threading.Lock()
_ROUND_ROBIN = {"i": 0}
COMBO_ATTEMPT_TIMEOUT = max(8, int(os.environ.get("COMBO_ATTEMPT_TIMEOUT", "35")))


def routing_state() -> dict:
    """Cached state snapshot so routing never hits the disk per request."""
    now = time.monotonic()
    cached = _ROUTE_CACHE["st"]
    if cached is not None and now - _ROUTE_CACHE["at"] < 5.0:
        return cached
    st = state_load()
    _ROUTE_CACHE.update(at=now, st=st)
    return st


def tcp_alive(url: str, timeout: float = 0.5) -> bool:
    """Fast preflight: skip a bridge whose port is closed, instantly."""
    try:
        u = urllib.parse.urlparse(url)
        host = u.hostname or "127.0.0.1"
        port = u.port or (443 if u.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def combo_candidates(st: dict) -> list[dict]:
    """Think before acting: score and order the chain using live stats."""
    now = time.time()
    routing = st.setdefault("routing", {})
    strategy = routing.get("strategy", "adaptive")
    stats = routing.setdefault("stats", {})
    out = []
    for idx, e in enumerate(combo_chain(st)):
        slug, model = e.get("provider"), e.get("model")
        p = st.get("providers", {}).get(slug)
        if not p or not model:
            continue
        rec = (p.get("models") or {}).get(model, {})
        if rec.get("removed_upstream") or rec.get("status") in ("quota", "failing"):
            continue
        s = stats.get(model, {})
        if (s.get("cooldown_until") or 0) > now:
            continue
        url = p.get("hermes_url") or p["url"]
        if not tcp_alive(url):
            continue
        ok, fail = s.get("ok", 0), s.get("fail", 0)
        total = ok + fail
        rate = (ok / total) if total else 0.6
        avg = s.get("avg_ms") or 8000
        if strategy == "priority":
            score = 1000000 - idx * 1000
        elif strategy == "fastest":
            score = 1000000 - avg
        elif strategy == "round_robin":
            score = 1000000 - abs(_ROUND_ROBIN["i"] - idx) * 1000
        else:  # adaptive
            score = rate * 500000 - avg * 0.5 + (10000 if not total else 0)
        out.append({"provider": slug, "model": model, "url": url, "score": score})
    out.sort(key=lambda c: -c["score"])
    _ROUND_ROBIN["i"] = (_ROUND_ROBIN["i"] + 1) % max(1, len(combo_chain(st)))
    return out


def combo_note(st: dict, model: str, ms: int, ok: bool) -> None:
    """Record outcome + apply circuit breaker. Persisted atomically."""
    with _ROUTE_LOCK:
        stats = st.setdefault("routing", {}).setdefault("stats", {})
        s = stats.setdefault(model, {"ok": 0, "fail": 0, "avg_ms": None,
                                     "consecutive_fail": 0})
        if ok:
            s["ok"] = s.get("ok", 0) + 1
            s["consecutive_fail"] = 0
            s["last_ms"] = ms
            prev = s.get("avg_ms")
            s["avg_ms"] = int(ms if prev is None else prev * 0.7 + ms * 0.3)
            s.pop("cooldown_until", None)
            s.pop("last_error", None)
        else:
            s["fail"] = s.get("fail", 0) + 1
            s["consecutive_fail"] = s.get("consecutive_fail", 0) + 1
            s["last_fail"] = utcnow()
            if s["consecutive_fail"] >= 3:
                s["cooldown_until"] = time.time() + 300
                log(f"combo: circuit breaker open for {model} (300s)")
        try:
            state_save(st)
        except Exception:
            pass


def combo_chat(payload: dict, st: dict | None = None) -> tuple[int, dict, dict]:
    """Think (score/select) then act (forward), falling back on any failure."""
    st = st or routing_state()
    attempts = []
    wants_stream = bool(payload.get("stream"))
    for cand in combo_candidates(st):
        model, slug = cand["model"], cand["provider"]
        p = st["providers"][slug]
        key, _ = provider_bearer(slug, p)
        url = cand["url"].rstrip("/") + "/chat/completions"
        body = dict(payload or {})
        body["model"] = model
        body["stream"] = False
        body.pop("stream_options", None)
        t0 = time.monotonic()
        try:
            status, data = http_json(url, "POST", body, key=key,
                                     timeout=COMBO_ATTEMPT_TIMEOUT)
        except Exception as exc:
            status, data = 0, {"_error": str(exc)}
        ms = int((time.monotonic() - t0) * 1000)
        if status == 200 and isinstance(data, dict) and data.get("choices"):
            combo_note(st, model, ms, True)
            data = dict(data)
            data["model"] = f"auto/{model}"
            return 200, data, {"model": model, "provider": slug,
                               "attempts": attempts, "stream": wants_stream}
        msg = ""
        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            msg = str(data["error"].get("message") or "")[:120]
        combo_note(st, model, ms, False)
        attempts.append(f"{model}: HTTP {status} {msg} ({ms}ms)")
        log(f"combo: {model} failed (HTTP {status}, {ms}ms) -> next best")
    return 502, openai_error(
        "auto combo: no healthy model available: " + "; ".join(attempts[-6:]),
        "api_error", 502, "combo_exhausted"), {"attempts": attempts}


def combo_models(st: dict) -> list[str]:
    return [f"auto/{e['model']}" for e in combo_chain(st) if e.get("model")]


def combo_sse(data: dict) -> str:
    """Render a normal chat-completion as SSE chunks for streaming callers."""
    out = []
    base = {k: data.get(k) for k in ("id", "object", "created", "model")}
    first = dict(base)
    first["choices"] = [{"index": 0, "delta": {"role": "assistant", "content": ""},
                         "finish_reason": None}]
    out.append(first)
    try:
        msg = data["choices"][0]["message"]
    except Exception:
        msg = {}
    calls = msg.get("tool_calls") or []
    if calls:
        for i, c in enumerate(calls):
            chunk = dict(base)
            chunk["choices"] = [{"index": 0, "delta": {"tool_calls": [{
                "index": i, "id": c.get("id"), "type": "function",
                "function": c.get("function", {})}]}, "finish_reason": None}]
            out.append(chunk)
    else:
        content = msg.get("content") or ""
        for i in range(0, len(content), 200):
            chunk = dict(base)
            chunk["choices"] = [{"index": 0, "delta": {"content": content[i:i + 200]},
                                 "finish_reason": None}]
            out.append(chunk)
    last = dict(base)
    last["choices"] = [{"index": 0, "delta": {},
                        "finish_reason": (data["choices"][0].get("finish_reason") if data.get("choices") else "stop")}]
    out.append(last)
    if data.get("usage") is not None:
        u = dict(base)
        u["choices"] = []
        u["usage"] = data["usage"]
        out.append(u)
    lines = "".join("data: " + json.dumps(c) + "\n\n" for c in out)
    return lines + "data: [DONE]\n\n"


def hermes_connection_status() -> dict:
    """Live view of the Hermes <-> hub connection for the dashboard."""
    st = state_load()
    bridges = []
    for slug, p in st.get("providers", {}).items():
        url = p.get("hermes_url") or p.get("url") or ""
        bridges.append({"slug": slug, "name": p.get("name") or slug, "url": url,
                        "alive": tcp_alive(url, 0.8),
                        "models": len(p.get("models") or {})})
    # lightweight config parse (no PyYAML dependency in the runtime)
    cfg_providers: list[str] = []
    global_model = ""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8", errors="replace") as fh:
            in_providers = False
            for raw in fh:
                line = raw.rstrip("\r\n")
                if line.startswith("providers:"):
                    in_providers = True
                    continue
                if in_providers and line.startswith("  ") and not line.startswith("    ") \
                        and line.strip().endswith(":"):
                    cfg_providers.append(line.strip().rstrip(":"))
                elif in_providers and line and not line.startswith(" "):
                    in_providers = False
                if line.startswith("model:"):
                    global_model = line.split(":", 1)[1].strip()
    except OSError:
        pass
    gateway = {"up": False, "pid": None}
    try:
        ps = ("Get-CimInstance Win32_Process -Filter \"Name like '%python%'\" | "
              "Where-Object { $_.CommandLine -match 'gateway run' } | "
              "Select-Object -First 1 -ExpandProperty ProcessId")
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=8)
        lines = [x.strip() for x in (out.stdout or "").splitlines() if x.strip().isdigit()]
        if lines:
            gateway = {"up": True, "pid": int(lines[0])}
    except Exception:
        pass
    return {"gateway": gateway, "bridges": bridges, "config_providers": cfg_providers,
            "global_model": global_model, "verify": dict(_HERMES_VERIFY),
            "default_global": "unchanged" if not global_model else global_model}


def verify_hermes_bg() -> None:
    """Run a real chat through Hermes and record the result."""
    global _HERMES_VERIFY
    _HERMES_VERIFY = {"state": "running", "at": utcnow(), "output": "", "secs": None}
    t0 = time.monotonic()
    try:
        out = subprocess.run(
            ["hermes", "-z", "Reply with only: CONNECTOR_OK",
             "--provider", "auto-free", "-m", "auto"],
            capture_output=True, text=True, timeout=150, shell=True)
        text = ((out.stdout or "") + (out.stderr or "")).strip()
        ok = out.returncode == 0 and bool(text)
        _HERMES_VERIFY = {"state": "ok" if ok else "fail", "at": utcnow(),
                          "output": text[-400:], "secs": int(time.monotonic() - t0)}
    except Exception as e:
        _HERMES_VERIFY = {"state": "fail", "at": utcnow(),
                          "output": f"{type(e).__name__}: {e}",
                          "secs": int(time.monotonic() - t0)}
    log(f"hermes verify -> {_HERMES_VERIFY['state']} ({_HERMES_VERIFY['secs']}s)")


def cmd_serve(args) -> int:
    """Connector Hub: dashboard UI + JSON API on 127.0.0.1:8399."""
    srv_state = {"restarting": False}

    def tail_log(n: int = 150) -> str:
        try:
            with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
                return "".join(f.readlines()[-n:])
        except FileNotFoundError:
            return ""

    def masked_status() -> dict:
        st = state_load()
        for slug, p in st.get("providers", {}).items():
            p["key_masked"] = provider_bearer(slug, p)[1]
            if p.get("auth") == "oauth":
                tok = token_load(slug) or {}
                p["token"] = {"present": bool(tok.get("access_token")),
                              "refresh": bool(tok.get("refresh_token")),
                              "expires_at": tok.get("expires_at")}
            if p.get("auth") == "oauth_store":
                info = token_store_info(p.get("token_store") or "")
                p["token_store_state"] = {"present": bool(info.get("present")),
                                          "email": info.get("email")}
        st["engine"] = dict(ENGINE)
        summary = {}
        for slug, p in st.get("providers", {}).items():
            c = {"ok": 0, "quota": 0, "fail": 0, "unknown": 0, "total": 0}
            for m in (p.get("models") or {}).values():
                if m.get("removed_upstream"):
                    continue  # vanished models are history, not live status
                c["total"] += 1
                s = m.get("status")
                if s == "working": c["ok"] += 1
                elif s == "quota": c["quota"] += 1
                elif s == "failing": c["fail"] += 1
                else: c["unknown"] += 1
            summary[slug] = c
        st["summary"] = summary
        return st

    def bg(fn, *a, **kw):
        def run():
            try:
                fn(*a, **kw)
            except Exception as e:
                log(f"background task failed: {e}")
        threading.Thread(target=run, daemon=True).start()

    def auto_refresh_loop():
        """Periodically reconcile live model catalogs + run a sentinel probe so
        statuses never go stale (root cause of the false QUOTA badges)."""
        interval = max(5, int(os.environ.get("CONNECTOR_AUTO_REFRESH_MIN", "30"))) * 60
        while True:
            time.sleep(interval)
            if ENGINE["busy"]:
                continue  # a user sweep is running; don't collide
            try:
                log("auto-refresh: reconciling model catalogs")
                with state_lock():
                    st = state_load()
                    reconcile_models(st)
                    state_save(st)
                # sentinel probe respects the cooldown, so this is cheap
                sweep(state_load(), do_tools=False, deep=False, force=False)
                log("auto-refresh: done")
            except Exception as e:
                log(f"auto-refresh failed: {e}")

    bg(auto_refresh_loop)

    def watchdog_loop():
        """Auto-restart any bridge whose port is dead (with storm protection)."""
        interval = max(15, int(os.environ.get("CONNECTOR_WATCHDOG_SEC", "60")))
        wrappers = {"cline-free": "cline-bridge.cmd",
                    "opencode-free": "opencode-bridge.cmd",
                    "antigravity-free": "antigravity-bridge.cmd",
                    "gemini-free": "gemini-bridge.cmd",
                    "zhipu-free": "zhipu-bridge.cmd",
                    "nvidia-free": "nvidia-bridge.cmd",
                    "openrouter-free": "openrouter-bridge.cmd"}
        last: dict[str, float] = {}
        vbs = os.path.join(CONNECTOR_DIR, "autostart", "run-hidden.vbs")
        while True:
            time.sleep(interval)
            try:
                st = state_load()
                for slug, wrapper in wrappers.items():
                    p = st.get("providers", {}).get(slug)
                    if not p or tcp_alive(p["url"]):
                        continue
                    now = time.time()
                    if now - last.get(slug, 0) < 180:
                        continue
                    last[slug] = now
                    cmd = os.path.join(CONNECTOR_DIR, "autostart", wrapper)
                    if os.path.isfile(vbs) and os.path.isfile(cmd):
                        flags = 0x08000000 if os.name == "nt" else 0
                        subprocess.run(["wscript.exe", vbs, cmd],
                                       creationflags=flags, timeout=20)
                        log(f"watchdog: restarted {slug} ({wrapper})")
            except Exception as e:
                log(f"watchdog error: {e}")

    bg(watchdog_loop)

    def restart_gateway_bg():
        srv_state["restarting"] = True
        try:
            out = subprocess.run(["hermes", "gateway", "restart"], capture_output=True,
                                 text=True, timeout=180, shell=True)
            log(f"gateway restart: rc={out.returncode} {(out.stdout or out.stderr or '').strip()[:200]}")
        except Exception as e:
            log(f"gateway restart failed: {e}")
        finally:
            srv_state["restarting"] = False

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, obj, ctype="application/json"):
            body = (json.dumps(obj, indent=2, ensure_ascii=False) if ctype == "application/json"
                    else obj).encode("utf-8")
            self.send_response(code)
            self.send_header("content-type", f"{ctype}; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _err(self, code, msg):
            return self._send(code, {"error": msg})

        def _body(self) -> dict:
            try:
                n = int(self.headers.get("content-length") or 0)
                return json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                return {}

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/ui", "/index.html"):
                return self._send(200, DASHBOARD_HTML, ctype="text/html")
            if path in ("/health", "/healthz"):
                return self._send(200, {"ok": True, "service": "hermes-connector-hub",
                                        "version": "1.2.0-phase3", "port": SERVE_PORT,
                                        "engine": dict(ENGINE)})
            if path in ("/status", "/v1/status"):
                return self._send(200, masked_status())
            if path in ("/auto/v1/models", "/auto/models"):
                return self._send(200, {"object": "list", "data": [
                    {"id": m, "object": "model", "created": 0, "owned_by": "hermes-auto"}
                    for m in combo_models(state_load())]})
            if path == "/api/logs":
                return self._send(200, {"lines": tail_log()})
            if path == "/api/hermes":
                return self._send(200, hermes_connection_status())
            if path == "/api/routing":
                st = state_load()
                routing = st.setdefault("routing", {"strategy": "adaptive", "stats": {}})
                return self._send(200, {"strategy": routing.get("strategy", "adaptive"),
                                        "stats": routing.get("stats", {}),
                                        "attempt_timeout": COMBO_ATTEMPT_TIMEOUT,
                                        "candidates": combo_candidates(st)})
            return self._err(404, f"unknown path {path}")

        def do_POST(self):
            path = self.path.split("?")[0]
            body = self._body()
            if path in ("/auto/v1/chat/completions", "/auto/chat/completions"):
                status, data, route = combo_chat(body)
                log(f"combo route -> {route.get('model', 'n/a')} (HTTP {status})")
                if status == 200 and route.get("stream"):
                    return self._send(200, combo_sse(data), ctype="text/event-stream")
                return self._send(status, data)
            if path == "/api/routing":
                strategy = str(body.get("strategy") or "").strip().lower()
                if strategy not in ("adaptive", "fastest", "priority", "round_robin"):
                    return self._err(400, "strategy must be adaptive|fastest|priority|round_robin")
                with state_lock():
                    st = state_load()
                    st.setdefault("routing", {})["strategy"] = strategy
                    state_save(st)
                _ROUTE_CACHE["at"] = 0.0  # invalidate cache
                log(f"combo strategy -> {strategy}")
                return self._send(200, {"strategy": strategy})
            if path == "/api/hermes/verify":
                if _HERMES_VERIFY.get("state") == "running":
                    return self._err(409, "a Hermes verification is already running")
                bg(verify_hermes_bg)
                return self._send(200, {"started": True})
            if path == "/api/sweep":
                st = state_load()
                if ENGINE["busy"]:
                    return self._err(409, "a probe is already running")
                slug = body.get("provider")
                if slug and slug not in st["providers"]:
                    return self._err(404, f"unknown provider: {slug}")
                only = [slug] if slug else None
                deep = bool(body.get("deep"))
                if deep:
                    log("dashboard: deep sweep requested (consumes provider quota)")
                bg(sweep, state_load(), only=only, deep=deep,
                   force=bool(body.get("force")), do_tools=not body.get("no_tools"))
                return self._send(200, {"started": True, "deep": deep})
            if path == "/api/retry":
                mid = body.get("model")
                st = state_load()
                slug = next((s for s, p in st["providers"].items() if mid in p["models"]), None)
                if not slug:
                    return self._err(404, f"unknown model: {mid}")
                if ENGINE["busy"]:
                    return self._err(409, "a probe is already running")
                bg(probe_one_model, state_load(), slug, mid)
                log(f"dashboard: retry {mid}")
                return self._send(200, {"started": True, "model": mid})
            if path == "/api/add":
                url, key, slug = body.get("url"), body.get("key"), (body.get("name") or "").strip()
                auth = body.get("auth") or "static"
                if auth not in ("static", "oauth", "oauth_store"):
                    return self._err(400, f"unsupported auth: {auth}")
                if not url or not slug:
                    return self._err(400, "url and name are required")
                if auth == "static" and not key:
                    return self._err(400, "key is required for static providers")
                if auth == "oauth_store" and not body.get("token_store"):
                    return self._err(400, "token_store path is required for oauth_store providers")
                if not re.match(r"^[A-Za-z0-9_-]+$", slug):
                    return self._err(400, "name may contain letters, digits, - and _ only")
                if ENGINE["busy"]:
                    return self._err(409, "a probe is already running")
                oauth = None
                if body.get("oauth_config"):
                    oauth = body["oauth_config"] if isinstance(body["oauth_config"], dict) \
                        else None
                    if oauth is None:
                        return self._err(400, "oauth_config must be a JSON object")
                if auth == "oauth" and (not oauth or not oauth.get("device_authorization_url")
                                        or not oauth.get("token_url") or not oauth.get("client_id")):
                    return self._err(400, "oauth providers need oauth_config with "
                                          "device_authorization_url, token_url and client_id")
                models = body.get("models")
                models = [m.strip() for m in models.split(",") if m.strip()] if models else None

                def run_add():
                    res = add_provider_core(state_load(), url=url, key=key, slug=slug,
                                            display=body.get("display"), models=models,
                                            do_tools=not body.get("no_tools"),
                                            auth=auth, oauth=oauth,
                                            token_store=body.get("token_store"),
                                            hermes_url=body.get("hermes_url"),
                                            hermes_key=body.get("hermes_key"))
                    if res.get("next_step"):
                        log(f"provider {slug}: {res['next_step']}")
                bg(run_add)
                log(f"dashboard: add provider {slug} -> {url} (auth={auth})")
                return self._send(200, {"started": True, "slug": slug})
            if path == "/api/oauth-login":
                slug = body.get("slug")
                st0 = state_load()
                if slug not in st0["providers"]:
                    return self._err(404, f"unknown provider: {slug}")
                if st0["providers"][slug].get("auth") != "oauth":
                    return self._err(400, f"provider {slug} auth is "
                                          f"{st0['providers'][slug].get('auth')}, not oauth")
                if slug in OAUTH_ACTIVE:
                    return self._err(409, f"oauth login already running for {slug}")

                def run_oauth():
                    res = oauth_login_core(slug, timeout_s=int(body.get("timeout") or 600))
                    if "error" in res:
                        log(f"oauth login {slug}: {res['error']}")
                bg(run_oauth)
                log(f"dashboard: oauth login started for {slug}")
                return self._send(200, {"started": True, "slug": slug})
            if path == "/api/decide":
                slug, decision = body.get("slug"), body.get("decision")
                with state_lock():
                    st = state_load()
                    if slug not in st["providers"]:
                        return self._err(404, f"unknown provider: {slug}")
                    if decision == "delete":
                        backup = yaml_remove_provider(slug)
                        p = st["providers"][slug]
                        purge_provider_files(slug, p)
                        del st["providers"][slug]
                        state_save(st)
                        log(f"dashboard: provider {slug} deleted (backup: {backup or 'n/a'})")
                        return self._send(200, {"ok": True, "action": "deleted", "backup": backup})
                    if decision == "keep":
                        st["providers"][slug]["pending_decision"] = False
                        state_save(st)
                        log(f"dashboard: provider {slug} kept")
                        return self._send(200, {"ok": True, "action": "kept"})
                    return self._err(400, "decision must be 'keep' or 'delete'")
            if path == "/api/remove":
                slug = body.get("slug")
                with state_lock():
                    st = state_load()
                    if slug not in st["providers"]:
                        return self._err(404, f"unknown provider: {slug}")
                    backup = yaml_remove_provider(slug)
                    p = st["providers"][slug]
                    purge_provider_files(slug, p)
                    del st["providers"][slug]
                    state_save(st)
                log(f"dashboard: provider {slug} removed (backup: {backup or 'not in config'})")
                return self._send(200, {"ok": True, "action": "removed", "backup": backup})
            if path == "/api/restart-gateway":
                if srv_state["restarting"]:
                    return self._err(409, "gateway restart already in progress")
                bg(restart_gateway_bg)
                return self._send(200, {"started": True})
            return self._err(404, f"unknown path {path}")

        def log_message(self, *a):
            pass

    os.makedirs(CONNECTOR_DIR, exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", SERVE_PORT), Handler)
    srv.daemon_threads = True
    log(f"connector hub v1.2 (phase 3: oauth + autostart) on http://127.0.0.1:{SERVE_PORT}/")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


# --------------------------------------------------------------------------
# Argument parser
# --------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="hermes-connectors",
                                 description="Hermes Connector Hub (free-model connector manager)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("seed", help="register the built-in local bridge providers (idempotent)")
    sp.set_defaults(func=cmd_seed)

    sp = sub.add_parser("test", help="probe models now; target = provider slug, model id, or --all")
    sp.add_argument("target", nargs="?", default=None)
    sp.add_argument("--all", action="store_true", help="deep sweep: every free model (uses quota!)")
    sp.add_argument("--force", action="store_true", help="sentinel probe now, ignoring the cooldown")
    sp.add_argument("--no-tools", action="store_true", help="skip the tool-call probe")
    sp.set_defaults(func=cmd_test)

    sp = sub.add_parser("status", help="show provider/model badges")
    sp.add_argument("--in-hermes", action="store_true")
    sp.add_argument("--working", action="store_true")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("list", help="list model ids (raw)")
    sp.add_argument("--free-only", action="store_true", default=True)
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("retry", help="re-probe one model (Retry Later)")
    sp.add_argument("model")
    sp.set_defaults(func=cmd_retry)

    sp = sub.add_parser("refresh-models", help="re-fetch live model catalogs; add new / remove vanished models")
    sp.add_argument("provider", nargs="?", default=None, help="limit to one provider slug")
    sp.set_defaults(func=cmd_refresh_models)

    sp = sub.add_parser("add", help="add a custom provider (free models only)")
    sp.add_argument("--url", required=True)
    sp.add_argument("--key", default=None, help="API key (required for --auth static)")
    sp.add_argument("--name", required=True, help="slug, e.g. my-provider")
    sp.add_argument("--display", default=None, help="display name (defaults to slug)")
    sp.add_argument("--models", default=None, help="comma-separated model ids to use as free (if discovery fails)")
    sp.add_argument("--auth", choices=["static", "oauth", "oauth_store"], default="static",
                    help="static = plain API key; oauth = device flow (RFC 8628); "
                         "oauth_store = companion-managed token store")
    sp.add_argument("--oauth-config", default=None,
                    help='JSON: {"device_authorization_url":..., "token_url":..., '
                         '"client_id":..., "scope":..., "audience":...}')
    sp.add_argument("--token-store", default=None,
                    help="path to a companion-managed token store JSON (oauth_store mode)")
    sp.add_argument("--hermes-url", default=None,
                    help="OpenAI-compatible URL Hermes should point at (defaults to --url; "
                         "oauth providers without it are monitor-only)")
    sp.add_argument("--hermes-key", default=None,
                    help="key for the hermes-facing endpoint (stored in a separate env var)")
    sp.add_argument("--yes", action="store_true", help="auto-keep after verification (skip prompt)")
    sp.add_argument("--no-tools", action="store_true")
    sp.set_defaults(func=cmd_add)

    sp = sub.add_parser("remove", help="remove a provider from Hermes and connector state")
    sp.add_argument("provider")
    sp.set_defaults(func=cmd_remove)

    sp = sub.add_parser("restart-gateway", help="restart the Hermes gateway (pick up config)")
    sp.set_defaults(func=cmd_restart_gateway)

    sp = sub.add_parser("oauth-login", help="RFC 8628 device-flow login for an oauth provider")
    sp.add_argument("slug")
    sp.add_argument("--timeout", type=int, default=600, help="seconds to keep polling")
    sp.add_argument("--no-browser", action="store_true", help="do not open the browser")
    sp.set_defaults(func=cmd_oauth_login)

    sp = sub.add_parser("token-import", help="import a saved OAuth token JSON into the connector store")
    sp.add_argument("--slug", required=True)
    sp.add_argument("--file", required=True)
    sp.set_defaults(func=cmd_token_import)

    sp = sub.add_parser("autostart", help="logon autostart (HKCU Run, no admin) for hub + bridges")
    sp.add_argument("--on", action="store_true", help="install the tasks (default action)")
    sp.add_argument("--off", action="store_true", help="remove the tasks")
    sp.add_argument("--status", action="store_true", help="only show task state")
    sp.set_defaults(func=cmd_autostart)

    sp = sub.add_parser("serve", help="run the connector hub JSON API on 127.0.0.1:8399")
    sp.set_defaults(func=cmd_serve)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        sys.exit(130)
