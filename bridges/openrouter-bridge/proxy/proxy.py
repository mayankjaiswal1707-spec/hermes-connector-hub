#!/usr/bin/env python3
"""openrouter-bridge - transparent OpenAI-compatible proxy for OpenRouter FREE models.

Unlike the CLI bridges, this is a NATIVE pass-through: Hermes' original
messages + tools are forwarded unchanged, so OpenRouter models handle tool
calls themselves (no JSON decision-engine contract, no tool loops).

Hermes -> this proxy -> https://openrouter.ai/api/v1/chat/completions

Only verified-working `:free` models are allowed. Bearer-key enforced.
Env:
  OPENROUTER_API_KEY              key (in hermes .env)
  OPENROUTER_BRIDGE_PROXY_KEY     bearer key Hermes uses (in hermes .env)
  OPENROUTER_BRIDGE_PROXY_PORT    default 8383
  OPENROUTER_BRIDGE_TIMEOUT       default 60s
"""

import json
import os
import socket
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROXY_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("OPENROUTER_BRIDGE_PROXY_PORT", "8383"))
BIND = os.environ.get("OPENROUTER_BRIDGE_PROXY_BIND", "127.0.0.1")
TIMEOUT = int(os.environ.get("OPENROUTER_BRIDGE_TIMEOUT", "60"))
API = "https://openrouter.ai/api/v1/chat/completions"

# Verified working free models (429-prone ones excluded).
FREE_MODELS = (
    "baidu/ernie-5.5-flash-preview:free",
    "liquid/lfm-3-1.7b:free",
    "particle-ai/particle-1:free",
    "recursive/flint-v1.0:free",
    "nvidia/nemotron-3.5-super-vl:free",
    "dots-studio/dots-3-note-preview:free",
    "nvidia/nemotron-3.5-lightning:free",
    "cohere/north-mini-code:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
)
PREFERRED = ("liquid/lfm-3-1.7b:free", "particle-ai/particle-1:free",
             "nvidia/nemotron-3.5-lightning:free")


# ---- single-instance guard: only ONE process may bind this bridge's port ----
def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _already_running() -> bool:
    lock = os.path.join(PROXY_DIR, "..", ".bridge.lock")
    # A live lock-holder PID means another instance is starting or running —
    # treat it as running even before its port binds (prevents startup races).
    try:
        with open(lock, "r", encoding="utf-8") as f:
            pid = int(f.read().strip())
        if pid != os.getpid() and _pid_alive(pid):
            return True
    except Exception:
        pass
    return False


def _claim_lock() -> bool:
    lock = os.path.join(PROXY_DIR, "..", ".bridge.lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        # Another process owns the lock. If it's alive, stand down. If dead
        # (stale lock after a crash), remove it and retry once.
        if _already_running():
            return False
        try:
            os.remove(lock)
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            return False
        except Exception:
            return True
    except Exception:
        return True

_stats = {"requests": 0, "errors": 0, "failovers": 0, "started": time.time(),
          "last_ms": None, "last_model": None}


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(os.path.join(PROXY_DIR, "..", "openrouter-bridge.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def api_key():
    return os.environ.get("OPENROUTER_API_KEY") or ""


def resolve_model(model):
    m = (model or "").strip().lower()
    m = m.removeprefix("openrouter-free/").removeprefix("openrouter/")
    if m in FREE_MODELS:
        return m
    # accept bare ids too
    for full in FREE_MODELS:
        if full == m or full.split("/")[-1] == m:
            return full
    return None


def order_models(requested):
    out = []
    req = resolve_model(requested) if requested else None
    if req:
        out.append(req)
    for m in PREFERRED:
        if m not in out:
            out.append(m)
    for m in FREE_MODELS:
        if m not in out:
            out.append(m)
    return out


def err_obj(msg, code=500, etype="api_error"):
    return {"error": {"message": msg, "type": etype, "param": None, "code": code}}


class _NoReuseServer(ThreadingHTTPServer):
    # Refuse a second bind on the same port at the OS level (atomic).
    allow_reuse_address = False


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _auth(self):
        want = os.environ.get("OPENROUTER_BRIDGE_PROXY_KEY") or ""
        return (not want) or self.headers.get("Authorization") == f"Bearer {want}"

    def _json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.send_header("connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass
        self.close_connection = True

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/health", "/healthz"):
            return self._json(200, {"ok": True, "port": PORT, "key": bool(api_key()),
                                    "models": list(FREE_MODELS),
                                    "stats": {**_stats, "uptime_s": int(time.time() - _stats["started"])}})
        if path in ("/v1/models", "/models"):
            if not self._auth():
                return self._json(401, err_obj("unauthorized", 401))
            return self._json(200, {"object": "list", "data": [
                {"id": m, "object": "model", "created": 0, "owned_by": "openrouter-bridge"}
                for m in FREE_MODELS]})
        return self._json(404, err_obj(f"unknown path {path}", 404))

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path not in ("/v1/chat/completions", "/chat/completions"):
            return self._json(404, err_obj(f"unknown path {path}", 404))
        if not self._auth():
            return self._json(401, err_obj("unauthorized: bad bearer key", 401))
        try:
            n = int(self.headers.get("content-length") or 0)
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            return self._json(400, err_obj(f"bad request: {e}", 400))
        _stats["requests"] += 1
        try:
            self._chat(payload)
        except Exception as e:
            _stats["errors"] += 1
            log(f"internal error: {type(e).__name__}: {e}")
            try:
                self._json(500, err_obj(f"{type(e).__name__}: {e}"))
            except Exception:
                pass

    def _chat(self, payload):
        model_req = str(payload.get("model") or "")
        if model_req and not resolve_model(model_req):
            return self._json(400, err_obj(
                f"model '{model_req}' not allowed. Available: {', '.join(FREE_MODELS)}", 400))
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            return self._json(400, err_obj("messages must be non-empty", 400))
        stream = bool(payload.get("stream"))
        key = api_key()
        if not key:
            return self._json(500, err_obj("OPENROUTER_API_KEY not configured", 500))

        last = "no attempt"
        for model in order_models(model_req)[:4]:
            body = dict(payload)
            body["model"] = model
            req = urllib.request.Request(
                API, data=json.dumps(body).encode("utf-8"),
                headers={"content-type": "application/json",
                         "authorization": f"Bearer {key}",
                         "HTTP-Referer": "http://localhost",
                         "X-Title": "HermesConnector"},
                method="POST")
            t0 = time.monotonic()
            try:
                resp = urllib.request.urlopen(req, timeout=TIMEOUT)
            except urllib.error.HTTPError as e:
                raw = e.read().decode("utf-8", errors="replace")
                last = f"HTTP {e.code}: {raw[:160]}"
                log(f"{model} -> {last[:100]}")
                _stats["failovers"] += 1
                continue
            except Exception as e:
                last = f"{type(e).__name__}: {e}"
                log(f"{model} -> {last[:100]}")
                _stats["failovers"] += 1
                continue
            ms = int((time.monotonic() - t0) * 1000)
            _stats["last_ms"] = ms
            _stats["last_model"] = model
            log(f"{model} -> stream={stream} in {ms}ms")
            if stream:
                return self._stream(resp)
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                obj = json.loads(raw)
            except Exception:
                return self._json(502, err_obj(f"bad upstream JSON: {raw[:200]}", 502))
            return self._json(200, obj)
        _stats["errors"] += 1
        return self._json(502, err_obj(f"openrouter-bridge: no free model responded: {last}", 502))

    def _stream(self, resp):
        self.send_response(200)
        self.send_header("content-type", "text/event-stream; charset=utf-8")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "close")
        self.end_headers()
        try:
            while True:
                line = resp.readline()
                if not line:
                    break
                self.wfile.write(line)
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except Exception:
            pass
        self.close_connection = True


def main():
    if _already_running() or not _claim_lock():
        print("another instance is already running; exiting", flush=True)
        return

    try:
        srv = _NoReuseServer((BIND, PORT), Handler)
    except OSError as e:
        print(f'port {PORT} already bound; exiting: {e}', flush=True)
        return
    srv.daemon_threads = True
    log(f"openrouter-bridge on http://{BIND}:{PORT}/v1 (key check: {'ON' if os.environ.get('OPENROUTER_BRIDGE_PROXY_KEY') else 'OFF'})")
    log(f"free models: {list(FREE_MODELS)}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
