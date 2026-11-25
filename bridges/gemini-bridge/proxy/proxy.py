#!/usr/bin/env python3
"""gemini-bridge - OpenAI-compatible endpoint over the Google Gemini API (free tier).

Hermes -> this proxy -> generativelanguage.googleapis.com/v1beta/models/<m>:generateContent

Uses the JSON decision-engine contract (same as the cline/opencode/antigravity
bridges) so Hermes sees either tool_calls or plain content. Bearer-key enforced.

Env:
  GOOGLE_API_KEY              Google AI Studio key (stored in hermes .env)
  GEMINI_BRIDGE_PROXY_KEY     bearer key Hermes uses (stored in hermes .env)
  GEMINI_BRIDGE_PROXY_PORT    default 8380
  GEMINI_BRIDGE_TIMEOUT       per-request timeout, default 30s

Zero runtime dependencies (stdlib only).
"""

import json
import os
import socket
import re
import threading
import time
import urllib.request
import urllib.error
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROXY_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("GEMINI_BRIDGE_PROXY_PORT", "8380"))
BIND = os.environ.get("GEMINI_BRIDGE_PROXY_BIND", "127.0.0.1")
TIMEOUT = int(os.environ.get("GEMINI_BRIDGE_TIMEOUT", "30"))
MAX_PROMPT_CHARS = int(os.environ.get("GEMINI_BRIDGE_MAX_PROMPT_CHARS", "18000"))
API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# Free-tier models verified working with the account (overloaded ones are kept
# lower in the failover order).
FREE_MODELS = (
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-flash-lite-latest",
    "gemini-2.5-flash",
    "gemini-3-flash-preview",
    "gemini-flash-latest",
    "gemini-3.5-flash",
)
PREFERRED = ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite",
             "gemini-2.5-flash", "gemini-3-flash-preview")


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

_stats = {"requests": 0, "tool_calls": 0, "content": 0, "errors": 0,
          "failovers": 0, "started": time.time(), "last_ms": None, "last_model": None}
_lock = threading.Lock()

CONTRACT = """You are a JSON decision engine for an EXTERNAL runtime (Hermes). Do NOT use or mention your own tools. Reply with EXACTLY ONE JSON object, no markdown/fences/prose.

Output (choose one):
1. {"tool_calls":[{"name":"<tool>","arguments":{...}}]}  (use ONLY tool names listed under TOOLS)
2. {"content":"<final answer to the user>"}

Rules: arguments is a JSON object ({} if none). Multiple calls in one list. Never invent tool output. If no tool needed or task complete -> {"content":"..."}."""


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(os.path.join(PROXY_DIR, "..", "gemini-bridge.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def api_key() -> str | None:
    return os.environ.get("GOOGLE_API_KEY") or ""


def _load_env_keys() -> None:
    """Load Google keys from hermes/.env if not present in the environment."""
    try:
        import os as _os
        env = _os.path.join(_os.environ.get("LOCALAPPDATA", ""), "hermes", ".env")
        with open(env, "r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                if "=" in raw and not raw.strip().startswith("#"):
                    k, v = raw.split("=", 1)
                    k, v = k.strip(), v.strip().strip('"').strip("'")
                    if k in ("GOOGLE_API_KEY", "GOOGLE_API_KEY_2", "GEMINI_BRIDGE_PROXY_KEY") and not _os.environ.get(k):
                        _os.environ[k] = v
    except Exception:
        pass
_load_env_keys()


def api_keys() -> list[str]:
    """All configured Google keys (AIza... and newer AQ... auth keys)."""
    return [k.strip() for k in (os.environ.get("GOOGLE_API_KEY", ""),
                                os.environ.get("GOOGLE_API_KEY_2", "")) if k.strip()]


def resolve_model(model: str | None) -> str | None:
    m = (model or "").strip().lower()
    m = re.sub(r"^(gemini|gemini-free)/", "", m)
    if not m:
        return None
    for full in FREE_MODELS:
        if full == m:
            return full
    return None


def order_models(requested: str | None) -> list[str]:
    ordered = []
    req = resolve_model(requested) if requested else None
    if req:
        ordered.append(req)
    for m in PREFERRED:
        if m not in ordered:
            ordered.append(m)
    for m in FREE_MODELS:
        if m not in ordered:
            ordered.append(m)
    return ordered


def compact_tools(tools: list) -> str:
    lines = []
    for t in (tools or [])[:40]:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        desc = " ".join(str(fn.get("description") or "").split())[:120]
        params = fn.get("parameters") if isinstance(fn.get("parameters"), dict) else {}
        props = params.get("properties") if isinstance(params.get("properties"), dict) else {}
        required = set(params.get("required") or [])
        args = []
        for k, v in list(props.items())[:10]:
            typ = v.get("type") if isinstance(v, dict) else "any"
            args.append(f"{k}{'*' if k in required else ''}:{typ or 'any'}")
        lines.append(f"- {name}({{{','.join(args)}}}): {desc}")
    return "\n".join(lines)


def _clip(s, n):
    s = str(s or "")
    return s if len(s) <= n else s[:n - 1] + "…"


def build_prompt(messages: list, tools: list, tool_choice) -> str:
    tool_map: dict = {}
    rendered = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "").lower()
        content = msg.get("content")
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
        content = _clip(content if isinstance(content, str) else ("" if content is None else json.dumps(content)), 2200)
        if role in ("system", "developer"):
            rendered.append("[S] " + _clip(content, 900))
        elif role == "user":
            rendered.append("[U] " + content)
        elif role == "assistant":
            calls = msg.get("tool_calls")
            if isinstance(calls, list) and calls:
                norm = []
                for c in calls[:12]:
                    fn = c.get("function") if isinstance(c.get("function"), dict) else {}
                    raw = fn.get("arguments")
                    try:
                        raw = json.loads(raw) if isinstance(raw, str) else (raw or {})
                    except Exception:
                        raw = {"_raw": str(raw)[:120]}
                    norm.append({"name": fn.get("name"), "arguments": raw})
                    tool_map[str(c.get("id") or "")] = fn.get("name")
                rendered.append("[A->tools] " + json.dumps({"tool_calls": norm}))
            elif content:
                rendered.append("[A] " + content)
        elif role == "tool":
            tid = str(msg.get("tool_call_id") or "")
            name = tool_map.get(tid) or str(msg.get("name") or "tool")
            rendered.append("[T] " + f"[{name}] {_clip(content, 1600)}")

    use_tools = tool_choice != "none" and bool(tools)
    header = "Decide NEXT ACTION. Output ONLY one JSON object.\n"
    if use_tools:
        header += "TOOLS:\n" + compact_tools(tools) + "\n"
    budget = MAX_PROMPT_CHARS - len(header) - 120
    kept, dropped = [], False
    for block in reversed(rendered):
        if budget - len(block) < 0:
            dropped = True
            break
        budget -= len(block) + 1
        kept.append(block)
    kept.reverse()
    body = header + "TRANSCRIPT:\n" + "\n".join(kept) + "\n"
    if dropped:
        body += "[earlier trimmed]\n"
    return CONTRACT + "\n\n" + body + "Reply with JSON only."


def call_gemini(model: str, prompt: str) -> tuple[int, str, int]:
    keys = api_keys()
    if not keys:
        raise RuntimeError("GOOGLE_API_KEY not configured")
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 2048, "temperature": 0},
    }
    t0 = time.monotonic()
    last_status, last_raw = 0, ""
    for key in keys:
        req = urllib.request.Request(
            f"{API_BASE}/{model}:generateContent",
            data=json.dumps(body).encode("utf-8"),
            headers={"content-type": "application/json", "x-goog-api-key": key},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            return 200, raw, int((time.monotonic() - t0) * 1000)
        except urllib.error.HTTPError as e:
            last_status = e.code
            last_raw = e.read().decode("utf-8", errors="replace")
            # Google rejects invalid keys with 400 (not 401); retry the next key
            # only when the error is specifically an auth problem.
            auth_error = (e.code in (401, 403) or
                          (e.code == 400 and "api key not valid" in last_raw.lower()))
            if auth_error:
                continue
            break
    return last_status, last_raw, int((time.monotonic() - t0) * 1000)


def extract_text(raw: str) -> tuple[str, str]:
    """Return (text, error)."""
    try:
        obj = json.loads(raw)
    except Exception:
        return "", f"bad JSON from Google: {raw[:200]}"
    if isinstance(obj.get("error"), dict):
        e = obj["error"]
        return "", f"google {e.get('code')}: {str(e.get('message'))[:250]}"
    try:
        parts = obj["candidates"][0]["content"].get("parts", [])
    except Exception:
        return "", f"unexpected Gemini response: {raw[:200]}"
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and p.get("text"))
    return text.strip(), ""


def parse_model_json(text: str):
    if not text:
        return None
    t = re.sub(r"^```(?:json)?\s*", "", text.strip())
    t = re.sub(r"\s*```$", "", t).strip()
    dec = json.JSONDecoder()
    for idx in range(len(t)):
        if t[idx] != "{":
            continue
        try:
            obj, _ = dec.raw_decode(t[idx:])
        except Exception:
            continue
        if isinstance(obj.get("tool_calls"), list) and obj["tool_calls"]:
            calls = []
            for c in obj["tool_calls"]:
                name = str(c.get("name") or "").strip()
                if not name:
                    continue
                raw = c.get("arguments", {})
                if isinstance(raw, str):
                    try:
                        raw = json.loads(raw)
                    except Exception:
                        raw = {"_raw": raw[:300]}
                if not isinstance(raw, dict):
                    raw = {}
                calls.append({"name": name, "arguments": raw})
            if calls:
                return {"tool_calls": calls}
        if isinstance(obj.get("content"), str) and obj["content"].strip():
            return {"content": obj["content"]}
    return None


def classify(status: int, err: str) -> str:
    low = err.lower()
    if status == 429 or "quota" in low or "rate" in low or "resource_exhausted" in low:
        return "quota"
    if status == 503 or "high demand" in low or "unavailable" in low:
        return "busy"
    return "fail"


def openai_error(message, etype="api_error", status=500, code=None):
    return {"error": {"message": message, "type": etype, "param": None, "code": code}}


def usage(prompt_chars: int, text: str) -> dict:
    pt = max(1, prompt_chars // 4)
    ct = max(1, len(text) // 4)
    return {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct}


def resp(model, msg, finish, prompt_chars, text, sid):
    return {"id": sid, "object": "chat.completion", "created": int(time.time()),
            "model": model, "choices": [{"index": 0, "message": msg,
            "finish_reason": finish, "logprobs": None}],
            "usage": usage(prompt_chars, text), "system_fingerprint": "gemini-bridge"}


class _NoReuseServer(ThreadingHTTPServer):
    # Refuse a second bind on the same port at the OS level (atomic).
    allow_reuse_address = False


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _authorized(self) -> bool:
        want = os.environ.get("GEMINI_BRIDGE_PROXY_KEY") or ""
        if not want:
            return True
        return self.headers.get("Authorization") == f"Bearer {want}"

    def _send(self, code, obj, ctype="application/json"):
        body = (json.dumps(obj) if ctype == "application/json" else obj).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", f"{ctype}; charset=utf-8")
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
            return self._send(200, {"ok": True, "port": PORT, "models": list(FREE_MODELS),
                                    "key": bool(api_key()),
                                    "stats": {**_stats, "uptime_s": int(time.time() - _stats["started"])}})
        if path in ("/v1/models", "/models"):
            if not self._authorized():
                return self._send(401, openai_error("unauthorized", "authentication_error", 401))
            return self._send(200, {"object": "list", "data": [
                {"id": m, "object": "model", "created": 0, "owned_by": "gemini-bridge"}
                for m in FREE_MODELS]})
        return self._send(404, openai_error(f"unknown path {path}", "invalid_request_error", 404))

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path not in ("/v1/chat/completions", "/chat/completions"):
            return self._send(404, openai_error(f"unknown path {path}", "invalid_request_error", 404))
        if not self._authorized():
            return self._send(401, openai_error("unauthorized: bad bearer key", "authentication_error", 401))
        try:
            n = int(self.headers.get("content-length") or 0)
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            return self._send(400, openai_error(f"bad request: {e}", "invalid_request_error", 400))
        _stats["requests"] += 1
        try:
            self._chat(payload)
        except Exception as e:
            _stats["errors"] += 1
            log(f"internal error: {type(e).__name__}: {e}")
            try:
                self._send(500, openai_error(f"{type(e).__name__}: {e}"))
            except Exception:
                pass

    def _chat(self, payload):
        model_req = str(payload.get("model") or "")
        if model_req and not resolve_model(model_req):
            return self._send(400, openai_error(
                f"model '{model_req}' is not an allowed Gemini model. Available: {', '.join(FREE_MODELS)}",
                "invalid_request_error", 400, "model_not_allowed"))
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            return self._send(400, openai_error("messages must be a non-empty list", "invalid_request_error", 400))
        tools = payload.get("tools") if isinstance(payload.get("tools"), list) else []
        tool_choice = payload.get("tool_choice")
        stream = bool(payload.get("stream"))
        prompt = build_prompt(messages, tools, tool_choice)
        prompt_chars = len(prompt)

        last = "no attempt"
        tried = []
        for model in order_models(model_req)[:4]:
            tried.append(model)
            status, raw, ms = call_gemini(model, prompt)
            text, err = extract_text(raw)
            if status == 200 and text:
                parsed = parse_model_json(text) or {"content": text}
                _stats["last_ms"] = ms
                _stats["last_model"] = model
                if "tool_calls" in parsed:
                    calls = [{"id": f"call_{uuid.uuid4().hex[:10]}", "type": "function",
                              "function": {"name": c["name"],
                                           "arguments": json.dumps(c["arguments"])}}
                             for c in parsed["tool_calls"]]
                    msg = {"role": "assistant", "content": None, "tool_calls": calls, "refusal": None}
                    finish = "tool_calls"
                    _stats["tool_calls"] += len(calls)
                else:
                    msg = {"role": "assistant", "content": parsed["content"], "refusal": None}
                    finish = "stop"
                    _stats["content"] += 1
                sid = f"chatcmpl-gmb-{uuid.uuid4().hex[:12]}"
                log(f"{model} -> {finish} in {ms}ms")
                out = resp(model, msg, finish, prompt_chars, text, sid)
                if stream:
                    return self._sse(out, msg, finish, sid, model)
                return self._send(200, out)
            last = err or f"HTTP {status}"
            bucket = classify(status, err)
            log(f"{model} -> {bucket} ({last[:100]})")
            if bucket == "quota":
                break  # account-wide, other models share it
            _stats["failovers"] += 1

        _stats["errors"] += 1
        status = 429 if classify(0, last) == "quota" else 502
        return self._send(status, openai_error(
            f"gemini-bridge: no model responded (tried {', '.join(tried)}): {last}",
            "rate_limit_error" if status == 429 else "api_error", status,
            "provider_limit" if status == 429 else None))

    def _sse(self, out, msg, finish, sid, model):
        chunks = []
        base = {"id": sid, "object": "chat.completion.chunk", "created": int(time.time()), "model": model}
        first = dict(base)
        first["choices"] = [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]
        chunks.append(first)
        if msg.get("tool_calls"):
            for i, c in enumerate(msg["tool_calls"]):
                ch = dict(base)
                ch["choices"] = [{"index": 0, "delta": {"tool_calls": [{
                    "index": i, "id": c["id"], "type": "function", "function": c["function"]}]},
                    "finish_reason": None}]
                chunks.append(ch)
        else:
            content = msg.get("content") or ""
            for i in range(0, len(content), 200):
                ch = dict(base)
                ch["choices"] = [{"index": 0, "delta": {"content": content[i:i + 200]}, "finish_reason": None}]
                chunks.append(ch)
        last = dict(base)
        last["choices"] = [{"index": 0, "delta": {}, "finish_reason": finish}]
        chunks.append(last)
        u = dict(base)
        u["choices"] = []
        u["usage"] = out["usage"]
        chunks.append(u)
        body = "".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n"
        return self._send(200, body, ctype="text/event-stream")


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
    log(f"gemini-bridge on http://{BIND}:{PORT}/v1 (key check: {'ON' if os.environ.get('GEMINI_BRIDGE_PROXY_KEY') else 'OFF'})")
    log(f"models: {list(FREE_MODELS)}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
