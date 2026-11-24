#!/usr/bin/env python3
"""freetoken-bridge - OpenAI-compatible proxy for freetoken.website.

Hermes -> this proxy -> https://freetoken.website/v1/chat/completions

Native pass-through: the caller's messages + tools are forwarded unchanged
(proper tool calling, SSE streaming). Models are the provider's catalog.

The account currently has no quota (HTTP 403 insufficient quota); the connector
marks these models QUOTA so they auto-recover once the account is funded.

Env:
  FREETOKEN_API_KEY             provider key (in hermes .env)
  FREETOKEN_BRIDGE_PROXY_KEY    bearer key Hermes uses (in hermes .env)
  FREETOKEN_BRIDGE_PROXY_PORT   default 8385
  FREETOKEN_BRIDGE_TIMEOUT      default 60s
"""

import json
import os
import socket
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROXY_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("FREETOKEN_BRIDGE_PROXY_PORT", "8385"))
BIND = os.environ.get("FREETOKEN_BRIDGE_PROXY_BIND", "127.0.0.1")
TIMEOUT = int(os.environ.get("FREETOKEN_BRIDGE_TIMEOUT", "60"))
API = "https://freetoken.website/v1/chat/completions"

MODELS = (
    "claude-5.5-opus", "claude-5.5-sonnet", "claude-5.5-haiku",
    "claude-5-opus", "claude-5-sonnet",
    "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5",
    "DeepSeek-V4.1-Flash", "DeepSeek-V4-Pro", "DeepSeek-V4-Flash",
    "glm-5.3", "glm-5.3-flash", "kimi-k3", "MiniMax-M3.1-Flash",
    "Qwen3.8-Flash-Next", "Qwen3.8-27B", "step-5-preview", "step-3.7-flash",
    "sensenova-6.8-flash-lite", "sensenova-u1.5-lite", "auto",
)
PREFERRED = ("auto", "claude-5.5-haiku", "gpt-5.6-sol", "DeepSeek-V4.1-Flash")


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
        with open(os.path.join(PROXY_DIR, "..", "freetoken-bridge.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def api_key():
    return os.environ.get("FREETOKEN_API_KEY") or ""


def resolve_model(model):
    m = (model or "").strip().lower()
    m = m.removeprefix("freetoken/").removeprefix("freetoken-free/")
    for full in MODELS:
        if full.lower() == m:
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
    return out


def err_obj(msg, status=500, code=None):
    return {"error": {"message": msg, "type": "api_error", "param": None, "code": code}}


class _NoReuseServer(ThreadingHTTPServer):
    # Refuse a second bind on the same port at the OS level (atomic).
    allow_reuse_address = False


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _auth(self):
        want = os.environ.get("FREETOKEN_BRIDGE_PROXY_KEY") or ""
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
            return self._json(200, {"ok": bool(api_key()), "port": PORT,
                                    "models": list(MODELS), "auth": bool(api_key()),
                                    "stats": {**_stats, "uptime_s": int(time.time() - _stats["started"])}})
        if path in ("/v1/models", "/models"):
            if not self._auth():
                return self._json(401, err_obj("unauthorized", 401))
            return self._json(200, {"object": "list", "data": [
                {"id": f"freetoken/{m}", "object": "model", "created": 0, "owned_by": "freetoken-bridge"}
                for m in MODELS]})
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
                f"model '{model_req}' not allowed. Available: {', '.join(MODELS[:8])}...", 400))
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            return self._json(400, err_obj("messages must be non-empty", 400))
        key = api_key()
        if not key:
            return self._json(500, err_obj("FREETOKEN_API_KEY not configured", 500))
        stream = bool(payload.get("stream"))
        last = "no attempt"
        for model in order_models(model_req)[:3]:
            body = dict(payload)
            body["model"] = model
            req = urllib.request.Request(
                API, data=json.dumps(body).encode("utf-8"),
                headers={"content-type": "application/json", "authorization": "Bearer " + key},
                method="POST")
            t0 = time.monotonic()
            try:
                resp = urllib.request.urlopen(req, timeout=TIMEOUT)
            except urllib.error.HTTPError as e:
                raw = e.read().decode("utf-8", errors="replace")
                last = f"HTTP {e.code}: {raw[:160]}"
                log(f"{model} -> {last[:100]}")
                if e.code == 403:
                    break  # quota/balance — shared by all models
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
                return self._json(200, json.loads(raw))
            except Exception:
                return self._json(502, err_obj(f"bad upstream JSON: {raw[:200]}", 502))
        _stats["errors"] += 1
        status = 429 if "403" in last or "quota" in last.lower() else 502
        return self._json(status, err_obj(f"freetoken-bridge: no model responded: {last}", status))

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
    log(f"freetoken-bridge on http://{BIND}:{PORT}/v1 (key check: {'ON' if os.environ.get('FREETOKEN_BRIDGE_PROXY_KEY') else 'OFF'})")
    log(f"models: {len(MODELS)}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
