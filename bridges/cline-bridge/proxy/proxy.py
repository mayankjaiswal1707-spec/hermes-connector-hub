#!/usr/bin/env python3
"""cline-bridge proxy — OpenAI-compatible endpoint served over `cline` CLI.

Hermes points a custom provider at http://127.0.0.1:<port>/v1. Each chat completion
request is flattened into a prompt and sent to a Cline *free* model through the
`cline --json` headless path (the only free-tier entry point; free usage is not
supported through the Cline API). The model's reply is parsed back into either an
OpenAI `content` message or emulated `tool_calls`.

Optimized for lower latency and higher reliability: cached CLI resolution, tighter
prompts, per-request timing, refusal hardening, and automatic failover across
currently-available free models on retryable failures.

Stdlib only. Loopback only.
"""

from __future__ import annotations

import json
import os
import socket
import re
import shutil
import subprocess
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("CLINE_BRIDGE_PROXY_PORT", "8378"))
BIND = os.environ.get("CLINE_BRIDGE_PROXY_BIND", "127.0.0.1")
CLINE_TIMEOUT = int(os.environ.get("CLINE_BRIDGE_PROXY_TIMEOUT", "90"))
MAX_PROMPT_CHARS = int(os.environ.get("CLINE_BRIDGE_MAX_PROMPT_CHARS", "18000"))
RETRY_PARSE_ONCE = False
NO_WINDOW = 0x08000000 if os.name == "nt" else 0

PROXY_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.normpath(os.path.join(PROXY_DIR, "..", "workspace"))
CATALOG_URL = "https://api.cline.bot/api/v1/ai/cline/recommended-models"

FALLBACK_FREE = (
    "cline-free/nemotron-3.5-super-vl",
    "cline-free/solar-mini4",
    "nvidia/nemotron-3.5-lightning-free",
)
# Preferred order when we must choose/failover (generally faster/more reliable first).
PREFERRED = (
    "nvidia/nemotron-3.5-lightning-free",
    "cline-free/nemotron-3.5-super-vl",
    "cline-free/solar-mini4",
)

_models_cache = {"at": 0.0, "items": ()}
_cline_lock = threading.Lock()

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
          "retries": 0, "failovers": 0, "started": time.time(),
          "last_ms": None, "last_model": None}
_base_cache: list[str] | None = None


LOG_PATH = os.path.join(PROXY_DIR, "..", "cline-bridge.log")


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# --------------------------------------------------------------------------- cline cli


def cline_base() -> list[str] | None:
    """Resolve a stable cline invocation (prefer node + bin to avoid .cmd newline issues)."""
    global _base_cache
    if _base_cache is not None:
        return _base_cache
    node = shutil.which("node")
    cli = shutil.which("cline")
    if node and cli:
        binpath = os.path.join(os.path.dirname(os.path.abspath(cli)),
                               "node_modules", "cline", "bin", "cline")
        if os.path.isfile(binpath):
            _base_cache = [node, binpath]
            return _base_cache
    if cli:
        if os.name == "nt" and cli.lower().endswith((".cmd", ".bat")):
            _base_cache = ["cmd.exe", "/c", cli]
        else:
            _base_cache = [cli]
        return _base_cache
    return None


def fetch_free_models() -> tuple[str, ...]:
    try:
        with urllib.request.urlopen(CATALOG_URL, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
        items = []
        for m in (data.get("free") or []):
            if isinstance(m, dict) and m.get("id"):
                items.append(str(m["id"]).strip())
        seen: set[str] = set()
        out = []
        for i in items:
            if i and i not in seen:
                seen.add(i)
                out.append(i)
        return tuple(out)
    except Exception as exc:
        log(f"model catalog fetch failed: {exc}")
        return ()


def free_models(force: bool = False) -> tuple[str, ...]:
    now = time.monotonic()
    if not force and _models_cache["items"] and now - _models_cache["at"] < 1800:
        return _models_cache["items"]
    items = fetch_free_models()
    if items:
        _models_cache.update(at=now, items=items)
        return items
    if _models_cache["items"]:
        return _models_cache["items"]
    return FALLBACK_FREE


def order_models(requested: str | None = None) -> list[str]:
    """Requested model first (if known), then preferred/available fallbacks."""
    avail = list(free_models())
    ordered: list[str] = []
    req = resolve_model(requested) if requested else None
    if req:
        ordered.append(req)
    for m in PREFERRED:
        if m in avail and m not in ordered:
            ordered.append(m)
    for m in avail:
        if m not in ordered:
            ordered.append(m)
    return ordered or list(FALLBACK_FREE)


def resolve_model(model: str | None) -> str | None:
    m = (model or "").strip().lower()
    if not m:
        return None
    for full in free_models():
        fl = full.lower()
        if fl == m or fl.endswith("/" + m) or fl.split("/")[-1] == m:
            return full
    return None


def run_cline(model: str, system: str, prompt: str, timeout: int = CLINE_TIMEOUT) -> tuple[int, str, str, int]:
    base = cline_base()
    if not base:
        raise RuntimeError("cline CLI not found on PATH")
    os.makedirs(WORKSPACE, exist_ok=True)
    argv = [*base, "--json", "-c", WORKSPACE, "-P", "cline", "-m", model,
            "--auto-approve", "false", "-s", system, prompt]
    t0 = time.monotonic()
    with _cline_lock:
        cp = subprocess.run(
            argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, cwd=WORKSPACE, creationflags=NO_WINDOW)
    ms = int((time.monotonic() - t0) * 1000)
    return cp.returncode, cp.stdout or "", cp.stderr or "", ms


def parse_run(stdout: str) -> tuple[str, dict, str | None]:
    text = ""
    usage: dict = {}
    error: str | None = None
    run_seen = False
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        et = ev.get("type")
        if et == "run_result":
            run_seen = True
            t = ev.get("text")
            if isinstance(t, str):
                text = t.strip()
            u = ev.get("usage") or ev.get("aggregateUsage") or {}
            if isinstance(u, dict):
                usage = {
                    "input": u.get("inputTokens") or u.get("totalInputTokens"),
                    "output": u.get("outputTokens") or u.get("totalOutputTokens"),
                    "cost": u.get("totalCost", u.get("cost")),
                }
            fr = str(ev.get("finishReason") or "")
            if fr and fr not in ("completed", "stop", "end_turn"):
                error = error or f"finishReason:{fr}"
        elif et == "agent_event":
            inner = ev.get("event") if isinstance(ev.get("event"), dict) else {}
            if inner.get("type") == "error":
                msg = inner.get("error") or inner.get("message") or ""
                if msg:
                    error = str(msg)[:400]
    if not text and not run_seen:
        parts = []
        for line in (stdout or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if ev.get("type") == "agent_event":
                inner = ev.get("event") if isinstance(ev.get("event"), dict) else {}
                if inner.get("type") == "content_end" and inner.get("contentType") == "text":
                    t = inner.get("text")
                    if isinstance(t, str):
                        parts.append(t)
        if parts:
            text = "".join(parts).strip()
    return text, usage, error


# --------------------------------------------------------------------------- prompt


CONTRACT = """You are a JSON decision engine for an EXTERNAL runtime (Hermes). Do NOT use or mention your own tools (run_commands, editor, read_files, etc.). Reply with EXACTLY ONE JSON object, no markdown/fences/prose.

Output (choose one):
1. {"tool_calls":[{"name":"<tool>","arguments":{...}}]}  (use ONLY tool names listed under TOOLS)
2. {"content":"<final answer to the user>"}

Rules: arguments is a JSON object ({} if none). Multiple calls in one list. Never invent tool output. If no tool needed or task complete -> {"content":"..."}.
CRITICAL: You are ONLY a JSON formatter. The "TOOLS" below are virtual labels you MUST emit as JSON — they are NOT functions you can execute yourself. Never say a tool is unavailable, don't attempt to actually call/run anything. Just output the JSON object with the tool name and arguments. For "use the X tool to Y" requests, ALWAYS emit {"tool_calls":[{"name":"X","arguments":{...}}]}. NEVER answer the question yourself with facts/data/results — you have NO ability to look things up; if the user wants info a tool provides, emit the tool_calls JSON instead of a direct answer. Only output {"content":"..."} for greetings, general conversation, or final answers after tool results. Use the EXACT tool names from TOOLS. NEVER call your own native tools (read_files, run_commands, etc.) — they are DISABLED."""


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
        req = set(params.get("required") or [])
        args = []
        for k, v in list(props.items())[:10]:
            typ = v.get("type") if isinstance(v, dict) else None
            if not typ and isinstance(v, dict) and v.get("enum"):
                typ = "enum"
            typ = typ or "any"
            star = "*" if k in req else ""
            args.append(f"{k}{star}:{typ}")
        argstr = ("{" + ",".join(args) + "}") if args else "{}"
        lines.append(f"- {name}({argstr}): {desc}")
    if len(tools or []) > 40:
        lines.append(f"- ... (+{len(tools)-40} more)")
    return "\n".join(lines)


def _clip(s: str, n: int) -> str:
    s = str(s or "")
    return s if len(s) <= n else s[: n - 1] + "…"


def render_messages(messages: list, tool_map: dict) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "").lower()
        content = msg.get("content")
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
        content = _clip(content if isinstance(content, str) else ("" if content is None else json.dumps(content, ensure_ascii=False)), 2200)
        if role in ("system", "developer"):
            out.append(("system", _clip(content, 900)))
        elif role == "user":
            out.append(("user", content))
        elif role == "assistant":
            calls = msg.get("tool_calls")
            if isinstance(calls, list) and calls:
                norm = []
                for c in calls[:12]:
                    if not isinstance(c, dict):
                        continue
                    fn = c.get("function") if isinstance(c.get("function"), dict) else {}
                    name = fn.get("name") or ""
                    raw = fn.get("arguments")
                    try:
                        args_obj = json.loads(raw) if isinstance(raw, str) else (raw or {})
                    except Exception:
                        args_obj = {"_raw": str(raw)[:120]}
                    norm.append({"name": name, "arguments": args_obj})
                    tool_map[str(c.get("id") or "")] = name
                out.append(("assistant_call", json.dumps({"tool_calls": norm}, ensure_ascii=False)))
            elif content:
                out.append(("assistant", content))
        elif role == "tool":
            tid = str(msg.get("tool_call_id") or "")
            name = tool_map.get(tid) or str(msg.get("name") or "tool")
            out.append(("tool", f"[{name}] {_clip(content, 1600)}"))
    return out


def build_prompt(messages: list, tools: list, tool_choice) -> str:
    tool_map: dict = {}
    rendered = render_messages(messages, tool_map)
    use_tools = tool_choice != "none" and bool(tools)
    budget = MAX_PROMPT_CHARS
    header = "Decide NEXT ACTION. Output ONLY one JSON object.\n"
    if use_tools:
        header += "TOOLS:\n" + compact_tools(tools) + "\n"
    elif tool_choice == "none":
        header += 'Tools disabled: answer {"content":"..."}.\n'
    header += "TRANSCRIPT:\n"
    budget -= len(header) + 120

    kept: list[str] = []
    dropped = False
    for role, text in reversed(rendered):
        block = {
            "system": f"[S] {text}",
            "user": f"[U] {text}",
            "assistant": f"[A] {text}",
            "assistant_call": f"[A->tools] {text}",
            "tool": f"[T] {text}",
        }[role]
        if budget - len(block) < 0:
            dropped = True
            break
        budget -= len(block) + 1
        kept.append(block)
    kept.reverse()
    body = header + "\n".join(kept) + "\n"
    if dropped:
        body += "[earlier trimmed]\n"
    body += "Reply with JSON only."
    return body


# --------------------------------------------------------------------------- parsing / shaping


def parse_model_json(text: str):
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t).strip()
    dec = json.JSONDecoder()
    for idx in range(len(t)):
        if t[idx] != "{":
            continue
        try:
            obj, _ = dec.raw_decode(t[idx:])
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        if isinstance(obj.get("tool_calls"), list) and obj["tool_calls"]:
            calls = []
            for c in obj["tool_calls"]:
                if not isinstance(c, dict):
                    continue
                name = str(c.get("name") or (c.get("function") or {}).get("name") or "").strip()
                if not name:
                    continue
                raw = c.get("arguments", (c.get("function") or {}).get("arguments"))
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
        if obj.get("name") and ("arguments" in obj):
            raw = obj.get("arguments")
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except Exception:
                    raw = {"_raw": raw[:300]}
            if isinstance(raw, dict):
                return {"tool_calls": [{"name": str(obj["name"]), "arguments": raw}]}
    return None


def bad_content_when_tools(p, tools, tool_choice) -> bool:
    if not p or "tool_calls" in p or tool_choice == "none" or not tools:
        return False
    c = (p.get("content") or "").lower()
    markers = ("unable to", "not available", "currently available", "non-interactive", "session restriction",
               "cannot execute", "can't execute", "cannot run", "can't run", "please run",
               "i can't", "i cannot", "don't have access", "do not have access", "no access to",
               "not in my", "doesn't have", "does not have", "toolset", "not permitted")
    return any(m in c for m in markers)


def retryable_failure(rc: int, text: str, err: str | None, detail: str) -> bool:
    if rc == 0 and text:
        return False
    low = f"{err or ''} {detail}".lower()
    markers = ("429", "rate", "limit", "quota", "insufficient", "402", "timeout", "timed out",
               "aborted", "finishreason:aborted", "unavailable", "503", "502", "overloaded",
               "temporar")
    return any(m in low for m in markers) or rc != 0


def _usage(prompt_chars: int, ev_usage: dict) -> dict:
    pin = ev_usage.get("input")
    pout = ev_usage.get("output")
    pt = int(pin) if isinstance(pin, (int, float)) else max(1, prompt_chars // 4)
    ct = int(pout) if isinstance(pout, (int, float)) else 0
    return {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct}


def _resp(model: str, choice_msg: dict, finish: str, ev_usage: dict, prompt_chars: int, sid: str) -> dict:
    return {
        "id": sid, "object": "chat.completion", "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "message": choice_msg, "finish_reason": finish, "logprobs": None}],
        "usage": _usage(prompt_chars, ev_usage),
        "system_fingerprint": "cline-bridge",
    }


def openai_error(message: str, etype: str = "api_error", status: int = 500, code: str | None = None) -> dict:
    return {"error": {"message": message, "type": etype, "param": None, "code": code}}


# --------------------------------------------------------------------------- handler


class _NoReuseServer(ThreadingHTTPServer):
    # Refuse a second bind on the same port at the OS level (atomic).
    allow_reuse_address = False


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "cline-bridge-proxy/1.1"

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass
        self.close_connection = True

    def _sse_begin(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

    def _sse(self, chunk: dict) -> None:
        self.wfile.write(b"data: " + json.dumps(chunk).encode("utf-8") + b"\n\n")
        self.wfile.flush()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/health", "/healthz"):
            self._json({"ok": True, "port": PORT, "workspace": WORKSPACE,
                        "models": free_models(), "preferred": list(PREFERRED),
                        "stats": {**_stats, "uptime_s": int(time.time() - _stats["started"])}})
        elif path in ("/v1/models", "/models"):
            now = int(time.time())
            self._json({"object": "list", "data": [
                {"id": m, "object": "model", "created": now, "owned_by": "cline-bridge"}
                for m in free_models()]})
        else:
            self._json(openai_error(f"unknown path {path}", "invalid_request_error", 404), 404)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path not in ("/v1/chat/completions", "/chat/completions"):
            self._json(openai_error(f"unknown path {path}", "invalid_request_error", 404), 404)
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception as exc:
            self._json(openai_error(f"bad request body: {exc}", "invalid_request_error", 400), 400)
            return
        _stats["requests"] += 1
        try:
            self._chat(payload)
        except BrokenPipeError:
            pass
        except Exception as exc:
            _stats["errors"] += 1
            log(f"internal error: {type(exc).__name__}: {exc}")
            try:
                self._json(openai_error(f"{type(exc).__name__}: {exc}"), 500)
            except Exception:
                pass

    def _attempt(self, model: str, prompt: str, tools, tool_choice):
        rc, out, errout, ms = run_cline(model, CONTRACT, prompt)
        text, ev_usage, error = parse_run(out)
        detail = ""
        if rc != 0 and not text:
            detail = (error or " ".join(errout.split())[-300:] or f"exit {rc}")
        elif not text and error:
            detail = error
        parsed = parse_model_json(text) if text else None
        need_retry = bool(text) and (parsed is None or bad_content_when_tools(parsed, tools, tool_choice))
        return {
            "model": model, "rc": rc, "ms": ms, "text": text, "usage": ev_usage,
            "error": error, "detail": detail, "parsed": parsed, "need_retry": need_retry,
        }

    def _chat(self, payload: dict) -> None:
        t_req = time.monotonic()
        model_req = str(payload.get("model") or "")
        candidates = order_models(model_req)
        if model_req and not resolve_model(model_req):
            avail = ", ".join(free_models())
            self._json(openai_error(
                f"model '{model_req}' is not an allowed Cline free model. Available: {avail}",
                "invalid_request_error", 400, "model_not_allowed"), 400)
            return

        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            self._json(openai_error("messages must be a non-empty list", "invalid_request_error", 400), 400)
            return
        tools = payload.get("tools") if isinstance(payload.get("tools"), list) else []
        tool_choice = payload.get("tool_choice")
        stream = bool(payload.get("stream"))
        include_usage = bool((payload.get("stream_options") or {}).get("include_usage"))
        prompt = build_prompt(messages, tools, tool_choice)
        prompt_chars = len(CONTRACT) + len(prompt)

        last_detail = "no attempt"
        chosen = None
        tried: list[str] = []
        for idx, model in enumerate(candidates[:2]):
            tried.append(model)
            try:
                a = self._attempt(model, prompt, tools, tool_choice)
            except subprocess.TimeoutExpired:
                last_detail = f"timeout after {CLINE_TIMEOUT}s"
                log(f"{model} -> timeout ({CLINE_TIMEOUT}s)")
                if idx < len(candidates[:2]) - 1:
                    _stats["failovers"] += 1
                continue
            except RuntimeError as exc:
                last_detail = str(exc)
                break

            # parse/format retry on same model (cheap relative to failover choice)
            # Skip the retry entirely for tool-less chats: a plain chat answer is
            # already valid content; a second CLI spawn doubles latency for nothing.
            parsed = a["parsed"]
            text, ev_usage = a["text"], a["usage"]
            if a["need_retry"] and RETRY_PARSE_ONCE and tools:
                _stats["retries"] += 1
                log(f"{model} -> unusable reply; retrying once")
                try:
                    nudge = ("\nIMPORTANT: If a tool from TOOLS applies emit "
                             '{"tool_calls":[{"name":"<tool>","arguments":{...}}]} else {"content":"..."}. JSON only.')
                    a2 = self._attempt(model, prompt + nudge, tools, tool_choice)
                    if a2["text"]:
                        p2 = a2["parsed"]
                        if p2 is not None and not bad_content_when_tools(p2, tools, tool_choice):
                            parsed, text, ev_usage, a = p2, a2["text"], a2["usage"], a2
                        elif parsed is None:
                            parsed, text, ev_usage = p2, a2["text"], a2["usage"]
                except Exception:
                    pass

            if text and parsed is None:
                parsed = {"content": text}

            ok = bool(parsed) and not bad_content_when_tools(parsed, tools, tool_choice)
            if ok:
                chosen = {"model": model, "parsed": parsed, "text": text, "usage": ev_usage, "ms": a["ms"]}
                break

            last_detail = a["detail"] or a["error"] or (f"rc={a['rc']}" if a["rc"] else "empty/unusable reply")
            if retryable_failure(a["rc"], text, a["error"], last_detail) and idx < len(candidates[:2]) - 1:
                _stats["failovers"] += 1
                log(f"{model} -> failover ({last_detail[:120]})")
                continue
            # non-retryable but unusable (e.g. persistent bad format) - try next anyway once
            if idx < len(candidates[:2]) - 1:
                _stats["failovers"] += 1
                log(f"{model} -> trying alternate ({last_detail[:120]})")
                continue
            break

        if not chosen:
            _stats["errors"] += 1
            low = last_detail.lower()
            status, etype, code = 502, "api_error", None
            if any(k in low for k in ("quota", "429", "rate", "limit", "insufficient", "402")):
                status, etype, code = 429, "rate_limit_error", "provider_limit"
            self._json(openai_error(
                f"cline-bridge: all attempts failed (tried {', '.join(tried)}): {last_detail}",
                etype, status, code), status)
            return

        model_used = chosen["model"]
        parsed = chosen["parsed"]
        ev_usage = chosen["usage"]
        sid = f"chatcmpl-clb-{uuid.uuid4().hex[:12]}"

        if "tool_calls" in parsed:
            calls = parsed["tool_calls"]
            tc = [{"id": f"call_{uuid.uuid4().hex[:10]}", "type": "function",
                   "function": {"name": c["name"], "arguments": json.dumps(c["arguments"], ensure_ascii=False)}}
                  for c in calls]
            msg = {"role": "assistant", "content": None, "tool_calls": tc, "refusal": None}
            finish = "tool_calls"
            _stats["tool_calls"] += len(tc)
        else:
            msg = {"role": "assistant", "content": parsed["content"], "refusal": None}
            finish = "stop"
            _stats["content"] += 1

        total_ms = int((time.monotonic() - t_req) * 1000)
        _stats["last_ms"] = total_ms
        _stats["last_model"] = model_used
        log(f"{model_used} -> {finish} in {chosen['ms']}ms (total {total_ms}ms, tried {len(tried)})")

        if stream:
            self._stream_response(sid, model_used, msg, finish, ev_usage, prompt_chars, include_usage)
        else:
            self._json(_resp(model_used, msg, finish, ev_usage, prompt_chars, sid))

    def _stream_response(self, sid, model, msg, finish, ev_usage, prompt_chars, include_usage) -> None:
        try:
            self._sse_begin()
            base = {"id": sid, "object": "chat.completion.chunk", "created": int(time.time()), "model": model}
            first = dict(base)
            first["choices"] = [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]
            self._sse(first)
            if msg.get("tool_calls"):
                for i, c in enumerate(msg["tool_calls"]):
                    ch = dict(base)
                    ch["choices"] = [{"index": 0, "delta": {"tool_calls": [{
                        "index": i, "id": c["id"], "type": "function",
                        "function": {"name": c["function"]["name"], "arguments": c["function"]["arguments"]},
                    }]}, "finish_reason": None}]
                    self._sse(ch)
            else:
                content = msg.get("content") or ""
                for i in range(0, len(content), 240):
                    ch = dict(base)
                    ch["choices"] = [{"index": 0, "delta": {"content": content[i:i + 240]}, "finish_reason": None}]
                    self._sse(ch)
            last = dict(base)
            last["choices"] = [{"index": 0, "delta": {}, "finish_reason": finish}]
            self._sse(last)
            if include_usage:
                u = dict(base)
                u["choices"] = []
                u["usage"] = _usage(prompt_chars, ev_usage)
                self._sse(u)
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionAbortedError):
            pass
        finally:
            self.close_connection = True


def main() -> None:
    if _already_running() or not _claim_lock():
        print("another instance is already running; exiting", flush=True)
        return

    os.makedirs(WORKSPACE, exist_ok=True)
    cline_base()
    ms = free_models(force=True)
    try:
        srv = _NoReuseServer((BIND, PORT), Handler)
    except OSError as e:
        print(f'port {PORT} already bound; exiting: {e}', flush=True)
        return
    srv.daemon_threads = True
    log(f"cline-bridge proxy v1.1 on http://{BIND}:{PORT}/v1  (workspace: {WORKSPACE})")
    log(f"free models: {list(ms)}  preferred: {list(PREFERRED)}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
