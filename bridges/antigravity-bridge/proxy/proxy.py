#!/usr/bin/env python3
"""antigravity-bridge proxy - OpenAI-compatible endpoint over the Antigravity CLI (agy).

Hermes (chat_completions) -> this proxy -> `agy -p "<decision prompt>" --output-format json`
-> Google Antigravity free tier (Gemini 3.8 Flash, Claude Sonnet/Opus 4.6, GPT-OSS 120B).

Tool calling is emulated: the model is driven as a JSON decision engine
(same contract as the cline/opencode bridges) so Hermes sees either
tool_calls or plain content.

Auth: requests must carry `Authorization: Bearer $ANTI_GRAVITY_BRIDGE_PROXY_KEY`
(the same key Hermes uses from hermes/.env). Loopback bind by default.

Env:
  ANTI_GRAVITY_BRIDGE_PROXY_PORT    default 8379
  ANTI_GRAVITY_BRIDGE_PROXY_BIND    default 127.0.0.1
  ANTI_GRAVITY_BRIDGE_PROXY_KEY     required bearer key (validated!)
  ANTI_GRAVITY_BRIDGE_PROXY_TIMEOUT agy subprocess timeout, default 110s
  ANTI_GRAVITY_CLI                  explicit path to agy (default: ~/.gemini/bin/agy.exe)

Zero runtime dependencies (stdlib only).
"""

import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("ANTI_GRAVITY_BRIDGE_PROXY_PORT", "8379"))
BIND = os.environ.get("ANTI_GRAVITY_BRIDGE_PROXY_BIND", "127.0.0.1")
AGY_TIMEOUT = int(os.environ.get("ANTI_GRAVITY_BRIDGE_PROXY_TIMEOUT", "110"))
MAX_PROMPT_CHARS = int(os.environ.get("ANTI_GRAVITY_BRIDGE_MAX_PROMPT_CHARS", "18000"))
NO_WINDOW = 0x08000000 if os.name == "nt" else 0

PROXY_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.normpath(os.path.join(PROXY_DIR, "..", "workspace"))

FALLBACK_MODELS = (
    "gemini-3.8-flash-low",
    "gemini-3.8-flash-medium",
    "gemini-3.8-flash-high",
    "claude-sonnet-4-6",
    "claude-opus-4-6-thinking",
    "gpt-oss-120b-medium",
)
# failover order when the requested model fails with a retryable error
PREFERRED = ("gemini-3.8-flash-medium", "gemini-3.8-flash-low", "gemini-3.8-flash-high")

_models_cache = {"at": 0.0, "items": ()}
_agy_lock = threading.Lock()
_stats = {"requests": 0, "tool_calls": 0, "content": 0, "errors": 0,
          "retries": 0, "failovers": 0, "started": time.time(),
          "last_ms": None, "last_model": None}
_base_cache: list[str] | None = None


LOG_PATH = os.path.join(PROXY_DIR, "..", "antigravity-bridge.log")


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# --------------------------------------------------------------------------- agy cli


def agy_base() -> list[str] | None:
    """Resolve the agy invocation: explicit env path, ~/.gemini/bin/agy.exe, then PATH."""
    global _base_cache
    if _base_cache is not None:
        return _base_cache
    candidates = []
    env_path = os.environ.get("ANTI_GRAVITY_CLI")
    if env_path and os.path.isfile(env_path):
        candidates.append(env_path)
    home_bin = os.path.join(os.path.expanduser("~"), ".gemini", "bin", "agy.exe")
    if os.path.isfile(home_bin):
        candidates.append(home_bin)
    on_path = shutil.which("agy")
    if on_path:
        candidates.append(on_path)
    for c in candidates:
        if os.path.isfile(c):
            _base_cache = [c]
            return _base_cache
    if on_path:
        _base_cache = [on_path]
    return _base_cache


def parse_models_stdout(stdout: str) -> tuple[str, ...]:
    """`agy models` prints `id<TAB>Label` lines (after a stderr status line)."""
    items = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line or "\t" not in line:
            continue
        mid = line.split("\t")[0].strip()
        if re.match(r"^[A-Za-z0-9._-]+$", mid) and mid not in items:
            items.append(mid)
    return tuple(items)


def fetch_models() -> tuple[str, ...]:
    base = agy_base()
    if not base:
        return ()
    try:
        with _agy_lock:
            cp = subprocess.run([*base, "models"], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=45,
                                creationflags=NO_WINDOW)
        items = parse_models_stdout(cp.stdout or "")
        return items
    except Exception as exc:
        log(f"agy models fetch failed: {exc}")
        return ()


def free_models(force: bool = False) -> tuple[str, ...]:
    now = time.monotonic()
    if not force and _models_cache["items"] and now - _models_cache["at"] < 1800:
        return _models_cache["items"]
    items = fetch_models()
    if items:
        _models_cache.update(at=now, items=items)
        return items
    if _models_cache["items"]:
        return _models_cache["items"]
    return FALLBACK_MODELS


def resolve_model(model: str | None) -> str | None:
    m = (model or "").strip().lower()
    if not m:
        return None
    # accept antigravity/<id>, antigravity-free/<id>, or bare id
    m = re.sub(r"^(antigravity|antigravity-free)/", "", m)
    for full in free_models():
        if full.lower() == m:
            return full
    return None


def order_models(requested: str | None = None) -> list[str]:
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
    return ordered or list(FALLBACK_MODELS)


def run_agy(model: str, prompt: str, timeout: int = AGY_TIMEOUT) -> tuple[int, str, str, int]:
    base = agy_base()
    if not base:
        raise RuntimeError("agy CLI not found (set ANTI_GRAVITY_CLI or install Antigravity CLI)")
    os.makedirs(WORKSPACE, exist_ok=True)
    argv = [*base, "-p", prompt, "--output-format", "json",
            "--disable-slash-commands", "--model", model]
    t0 = time.monotonic()
    with _agy_lock:
        cp = subprocess.run(
            argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, cwd=WORKSPACE, creationflags=NO_WINDOW)
    ms = int((time.monotonic() - t0) * 1000)
    return cp.returncode, cp.stdout or "", cp.stderr or "", ms


def parse_run(stdout: str) -> tuple[str, dict, str | None]:
    """agy --output-format json emits one JSON object:
    {conversation_id, status, response, duration_seconds, num_turns, usage{...}}"""
    text, usage, error = "", {}, None
    obj = None
    s = (stdout or "").strip()
    if s:
        try:
            obj = json.loads(s)
        except Exception:
            dec = json.JSONDecoder()
            idx = s.find("{")
            if idx >= 0:
                try:
                    obj, _ = dec.raw_decode(s[idx:])
                except Exception:
                    obj = None
    if isinstance(obj, dict):
        if isinstance(obj.get("response"), str):
            text = obj["response"].strip()
        u = obj.get("usage") if isinstance(obj.get("usage"), dict) else {}
        usage = {
            "input": u.get("input_tokens") or u.get("totalInputTokens"),
            "output": u.get("output_tokens") or u.get("totalOutputTokens"),
            "thinking": u.get("thinking_tokens"),
        }
        st = str(obj.get("status") or "")
        if st and st.upper() not in ("SUCCESS", "OK", "COMPLETED"):
            error = f"agy status:{st}"
            emsg = obj.get("error") or obj.get("message") or obj.get("detail")
            if emsg:
                error = f"{error}: {str(emsg)[:300]}"
    return text, usage, error


# --------------------------------------------------------------------------- prompt


CONTRACT = """You are a JSON decision engine for an EXTERNAL runtime (Hermes). Do NOT use or mention your own tools (terminal, editor, browser, etc.). Reply with EXACTLY ONE JSON object, no markdown/fences/prose.

Output (choose one):
1. {"tool_calls":[{"name":"<tool>","arguments":{...}}]}  (use ONLY tool names listed under TOOLS)
2. {"content":"<final answer to the user>"}

Rules: arguments is a JSON object ({} if none). Multiple calls in one list. Never invent tool output. If no tool needed or task complete -> {"content":"..."}."""


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
    return s if len(s) <= n else s[: n - 1] + "\u2026"


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
               "i can't", "i cannot", "permission", "don't have access", "do not have access",
               "no access to", "not in my", "doesn't have", "does not have", "toolset",
               "not permitted")
    return any(m in c for m in markers)


def retryable_failure(rc: int, err: str | None, detail: str) -> bool:
    if rc == 0 and not detail:
        return False
    low = f"{err or ''} {detail}".lower()
    markers = ("429", "rate", "limit", "quota", "exhaust", "resource", "402", "timeout",
               "timed out", "unavailable", "503", "502", "overloaded", "temporar", "aborted")
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
        "system_fingerprint": "antigravity-bridge",
    }


def openai_error(message: str, etype: str = "api_error", status: int = 500, code: str | None = None) -> dict:
    return {"error": {"message": message, "type": etype, "param": None, "code": code}}


# --------------------------------------------------------------------------- handler


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "antigravity-bridge-proxy/1.0"

    def log_message(self, fmt, *args):
        pass

    def _authorized(self) -> bool:
        want = os.environ.get("ANTI_GRAVITY_BRIDGE_PROXY_KEY") or ""
        if not want:
            return True  # no key configured -> loopback-only, fail open like cline bridge
        h = self.headers.get("Authorization") or ""
        return h == f"Bearer {want}"

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
            base = agy_base()
            self._json({"ok": True, "port": PORT, "agy": base[0] if base else None,
                        "models": list(free_models()), "preferred": list(PREFERRED),
                        "auth": bool(os.environ.get("ANTI_GRAVITY_BRIDGE_PROXY_KEY")),
                        "stats": {**_stats, "uptime_s": int(time.time() - _stats["started"])}})
        elif path in ("/v1/models", "/models"):
            if not self._authorized():
                self._json(openai_error("unauthorized", "authentication_error", 401), 401)
                return
            now = int(time.time())
            self._json({"object": "list", "data": [
                {"id": m, "object": "model", "created": now, "owned_by": "antigravity-bridge"}
                for m in free_models()]})
        else:
            self._json(openai_error(f"unknown path {path}", "invalid_request_error", 404), 404)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path not in ("/v1/chat/completions", "/chat/completions"):
            self._json(openai_error(f"unknown path {path}", "invalid_request_error", 404), 404)
            return
        if not self._authorized():
            _stats["errors"] += 1
            self._json(openai_error("unauthorized: bad or missing bearer key "
                                    "(ANTI_GRAVITY_BRIDGE_PROXY_KEY)",
                                    "authentication_error", 401), 401)
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
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            # client gave up (e.g. probe timeout) — don't log as internal error
            pass
        except Exception as exc:
            _stats["errors"] += 1
            log(f"internal error: {type(exc).__name__}: {exc}")
            try:
                self._json(openai_error(f"{type(exc).__name__}: {exc}"), 500)
            except Exception:
                pass

    def _attempt(self, model: str, prompt: str, tools, tool_choice):
        rc, out, errout, ms = run_agy(model, prompt)
        text, ev_usage, error = parse_run(out)
        detail = ""
        if not text:
            detail = (error or " ".join((errout or "").split())[-300:] or f"exit {rc}")
        parsed = parse_model_json(text) if text else None
        need_retry = bool(text) and (parsed is None or bad_content_when_tools(parsed, tools, tool_choice))
        return {
            "model": model, "rc": rc, "ms": ms, "text": text, "usage": ev_usage,
            "error": error, "detail": detail, "parsed": parsed, "need_retry": need_retry,
        }

    def _chat(self, payload: dict) -> None:
        t_req = time.monotonic()
        model_req = str(payload.get("model") or "")
        if model_req and not resolve_model(model_req):
            avail = ", ".join(free_models())
            self._json(openai_error(
                f"model '{model_req}' is not an allowed Antigravity model. Available: {avail}",
                "invalid_request_error", 400, "model_not_allowed"), 400)
            return
        candidates = order_models(model_req)

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
        for idx, model in enumerate(candidates[:3]):
            tried.append(model)
            try:
                a = self._attempt(model, prompt, tools, tool_choice)
            except subprocess.TimeoutExpired:
                last_detail = f"timeout after {AGY_TIMEOUT}s"
                log(f"{model} -> timeout ({AGY_TIMEOUT}s)")
                break  # no failover: the client-side timeout already dominates
            except RuntimeError as exc:
                last_detail = str(exc)
                break

            parsed = a["parsed"]
            text, ev_usage = a["text"], a["usage"]

            # upstream hard limit: do NOT burn failover attempts on other models —
            # they hit the same account quota. Fail fast with a 429 the connector
            # can classify in seconds instead of minutes.
            low_all = f"{a['error'] or ''} {a['detail'] or ''}".lower()
            if ("quota reached" in low_all or "resource_exhausted" in low_all
                    or (a["rc"] == 3 and ("quota" in low_all or "exhaust" in low_all))):
                _stats["errors"] += 1
                log(f"{model} -> account quota (fast-fail, no failover)")
                self._json(openai_error(
                    f"antigravity-bridge: upstream account quota reached (tried {', '.join(tried)}): "
                    f"{(a['error'] or a['detail'] or 'quota error')[:300]}",
                    "rate_limit_error", 429, "provider_limit"), 429)
                return

            # Skip retry for tool-less chats: plain content is valid; a second
            # agy spawn doubles latency (30-50s+) for nothing.
            if a["need_retry"] and tools:
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
            if retryable_failure(a["rc"], a["error"], last_detail if a["rc"] else "") and idx < len(candidates[:3]) - 1:
                _stats["failovers"] += 1
                log(f"{model} -> failover ({last_detail[:120]})")
                continue
            if idx < len(candidates[:3]) - 1:
                _stats["failovers"] += 1
                log(f"{model} -> trying alternate ({last_detail[:120]})")
                continue
            break

        if not chosen:
            _stats["errors"] += 1
            low = last_detail.lower()
            status, etype, code = 502, "api_error", None
            if any(k in low for k in ("quota", "429", "rate", "limit", "exhaust", "resource", "402")):
                status, etype, code = 429, "rate_limit_error", "provider_limit"
            self._json(openai_error(
                f"antigravity-bridge: all attempts failed (tried {', '.join(tried)}): {last_detail}",
                etype, status, code), status)
            return

        model_used = chosen["model"]
        parsed = chosen["parsed"]
        ev_usage = chosen["usage"]
        sid = f"chatcmpl-agb-{uuid.uuid4().hex[:12]}"

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
    os.makedirs(WORKSPACE, exist_ok=True)
    agy_base()
    ms = free_models(force=True)
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    srv.daemon_threads = True
    authed = bool(os.environ.get("ANTI_GRAVITY_BRIDGE_PROXY_KEY"))
    log(f"antigravity-bridge proxy v1.0 on http://{BIND}:{PORT}/v1  (workspace: {WORKSPACE}, "
        f"key check: {'ON' if authed else 'OFF - no key in env'})")
    log(f"models: {list(ms)}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
