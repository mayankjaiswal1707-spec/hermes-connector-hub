#!/usr/bin/env python3
"""nous-bridge - OpenAI-compatible proxy for Hermes CLI's own Nous Portal free models.

Hermes (and the Hermes Desktop app) -> this proxy -> https://inference-api.nousresearch.com/v1

Auth: reads the Nous OAuth agent_key from Hermes auth.json on every request,
so it always uses the token Hermes keeps fresh via its own keepalive. No key
is duplicated or stored by this bridge.

Only the genuinely-free (:free, zero-priced) Nous models are exposed.
Native pass-through: the caller's messages + tools are forwarded unchanged.

Env:
  NOUS_BRIDGE_PROXY_KEY      bearer key Hermes uses (in hermes .env)
  NOUS_BRIDGE_PROXY_PORT     default 8384
  NOUS_BRIDGE_TIMEOUT        default 90s
"""

import json
import os
import socket
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROXY_DIR = os.path.dirname(os.path.abspath(__file__))
HERMES_HOME = os.environ.get("HERMES_HOME") or os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "hermes")
AUTH_PATH = os.path.join(HERMES_HOME, "auth.json")
PORT = int(os.environ.get("NOUS_BRIDGE_PROXY_PORT", "8384"))
BIND = os.environ.get("NOUS_BRIDGE_PROXY_BIND", "127.0.0.1")
TIMEOUT = int(os.environ.get("NOUS_BRIDGE_TIMEOUT", "90"))

# Verified free (:free, pricing 0) Nous models; paid models excluded.
FREE_MODELS = (
    "upstage/solar-mini4:free",
    "tencent/hy4-flash:free",
    "stepfun/step-3.7-flash:free",
    "qwen/qwen4-max:free",
    "meituan/longcat-2.5:free",
)
PREFERRED = ("tencent/hy4-flash:free", "upstage/solar-mini4:free",
             "stepfun/step-3.7-flash:free", "qwen/qwen4-max:free")


# ---- single-instance guard: only ONE process may bind this bridge's port ----
def _already_running() -> bool:
    lock = os.path.join(PROXY_DIR, "..", ".bridge.lock")
    # A lock is only valid if the port is ACTUALLY bound. A stale lock from a
    # killed process (or a reused PID) must never block a fresh start.
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", PORT))
        s.close()
        return True
    except Exception:
        try:
            s.close()
        except Exception:
            pass
    # port is free -> clear stale lock so _claim_lock can create a fresh one
    try:
        if os.path.exists(lock):
            os.remove(lock)
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
        return _already_running()
    except Exception:
        return True  # fail open rather than block the bridge

_stats = {"requests": 0, "errors": 0, "failovers": 0, "started": time.time(),
          "last_ms": None, "last_model": None}


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(os.path.join(PROXY_DIR, "..", "nous-bridge.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def nous_creds():
    """Read the fresh Nous token/base URL from Hermes auth.json."""
    try:
        with open(AUTH_PATH, "r", encoding="utf-8", errors="replace") as f:
            d = json.load(f)
        n = (d.get("providers") or {}).get("nous") or {}
        token = n.get("agent_key") or n.get("access_token") or ""
        base = n.get("inference_base_url") or "https://inference-api.nousresearch.com/v1"
        return base.rstrip("/"), token
    except Exception as e:
        log(f"auth read failed: {e}")
        return "", ""


def resolve_model(model):
    m = (model or "").strip().lower()
    m = m.removeprefix("nous/").removeprefix("nous-free/")
    if m in FREE_MODELS:
        return m
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


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _auth(self):
        want = os.environ.get("NOUS_BRIDGE_PROXY_KEY") or ""
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
            base, token = nous_creds()
            return self._json(200, {"ok": bool(token), "port": PORT,
                                    "models": list(FREE_MODELS), "auth": bool(token),
                                    "stats": {**_stats, "uptime_s": int(time.time() - _stats["started"])}})
        if path in ("/v1/models", "/models"):
            if not self._auth():
                return self._json(401, err_obj("unauthorized", 401))
            return self._json(200, {"object": "list", "data": [
                {"id": f"nous/{m}", "object": "model", "created": 0, "owned_by": "nous-bridge"}
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
        base, token = nous_creds()
        if not base or not token:
            return self._json(503, err_obj("Nous auth not available in Hermes auth.json", 503))
        stream = bool(payload.get("stream"))
        last = "no attempt"
        for model in order_models(model_req):
            body = dict(payload)
            body["model"] = model
            req = urllib.request.Request(
                base + "/chat/completions", data=json.dumps(body).encode("utf-8"),
                headers={"content-type": "application/json", "authorization": "Bearer " + token},
                method="POST")
            t0 = time.monotonic()
            try:
                resp = urllib.request.urlopen(req, timeout=TIMEOUT)
            except urllib.error.HTTPError as e:
                raw = e.read().decode("utf-8", errors="replace")
                last = f"HTTP {e.code}: {raw[:160]}"
                log(f"{model} -> {last[:100]}")
                if e.code in (401, 403, 404):
                    break  # auth/credit problem — other models share it
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
        return self._json(502, err_obj(f"nous-bridge: no free model responded: {last}", 502))

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

    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    srv.daemon_threads = True
    log(f"nous-bridge on http://{BIND}:{PORT}/v1 (key check: {'ON' if os.environ.get('NOUS_BRIDGE_PROXY_KEY') else 'OFF'})")
    log(f"free models: {list(FREE_MODELS)}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
