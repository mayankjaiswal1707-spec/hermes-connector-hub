#!/usr/bin/env python3
"""opencode-bridge proxy — OpenAI-compatible endpoint served over `opencode run`.

Hermes points a custom provider at http://127.0.0.1:<port>/v1. Each chat completion
request is flattened into one prompt and sent to a free OpenCode model through the
`opencode run --agent oc-proxy` path. The model reply is parsed into either OpenAI
`content` or emulated `tool_calls`.

v1.1: cached CLI resolution, tighter prompts, per-request timing, refusal hardening,
and automatic failover across available free models on retryable failures.

Stdlib only. Loopback only.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("OCBRIDGE_PROXY_PORT", "8377"))
BIND = os.environ.get("OCBRIDGE_PROXY_BIND", "127.0.0.1")
OC_TIMEOUT = int(os.environ.get("OCBRIDGE_PROXY_TIMEOUT", "180"))
OC_AGENT = "oc-proxy"
MAX_PROMPT_CHARS = int(os.environ.get("OCBRIDGE_MAX_PROMPT_CHARS", "18000"))
RETRY_PARSE_ONCE = True
NO_WINDOW = 0x08000000 if os.name == "nt" else 0

PROXY_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.normpath(os.path.join(PROXY_DIR, "..", "workspace"))

DEFAULT_PROVIDER = "opencode"
FALLBACK_MODELS = (
    "opencode/beam-free",
    "opencode/big-pickle",
    "opencode/deepseek-v4.1-flash-free",
    "opencode/flint-40b-free",
    "opencode/glm-5.3-flash-free",
    "opencode/iceman-free",
    "opencode/longcat-2.5-free",
    "opencode/nemotron-3.5-lightning-free",
    "opencode/nemotron-3.5-super-vl-free",
)
PREFERRED = (
    "opencode/nemotron-3.5-lightning-free",
    "opencode/beam-free",
    "opencode/nemotron-3.5-super-vl-free",
    "opencode/glm-5.3-flash-free",
    "opencode/deepseek-v4.1-flash-free",
)

_models_cache = {"at": 0.0, "items": ()}
_oc_lock = threading.Lock()
_stats = {"requests": 0, "tool_calls": 0, "content": 0, "errors": 0,
          "retries": 0, "failovers": 0, "started": time.time(),
          "last_ms": None, "last_model": None}
_base_cache: list[str] | None = None
_quota_lock = threading.Lock()
_quota_cooldown_until = 0.0
_quota_cooldown_ms_default = 60 * 60 * 1000


LOG_PATH = os.path.join(PROXY_DIR, "..", "opencode-bridge.log")


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# --------------------------------------------------------------------------- opencode


def opencode_base() -> list[str] | None:
    global _base_cache
    if _base_cache is not None:
        return _base_cache
    exe = shutil.which("opencode")
    if not exe:
        return None
    if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
        real = os.path.join(os.path.dirname(os.path.abspath(exe)),
                            "node_modules", "@opencode", "cli", "bin", "opencode.exe")
        if os.path.isfile(real):
            _base_cache = [real]
            return _base_cache
        _base_cache = ["cmd.exe", "/c", exe]
        return _base_cache
    _base_cache = [exe]
    return _base_cache


def _cli_env(cwd: str) -> dict[str, str]:
    """Keep OpenCode's PWD consistent with subprocess cwd on Windows/MSYS."""
    env = os.environ.copy()
    env["PWD"] = os.path.abspath(cwd)
    return env


def free_models(force: bool = False) -> tuple[str, ...]:
    now = time.monotonic()
    if not force and _models_cache["items"] and now - _models_cache["at"] < 3600:
        return _models_cache["items"]
    base = opencode_base()
    items: list[str] = []
    if base:
        try:
            os.makedirs(WORKSPACE, exist_ok=True)
            cp = subprocess.run(
                [*base, "models"], capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=45, cwd=WORKSPACE, env=_cli_env(WORKSPACE),
                creationflags=NO_WINDOW)
            if cp.returncode == 0:
                for line in (cp.stdout or "").splitlines():
                    name = line.strip().split("#", 1)[0].strip()
                    if name and (name.endswith("-free") or name == "opencode/big-pickle"):
                        items.append(name)
        except Exception as exc:
            log(f"model discovery failed: {exc}")
    items = list(dict.fromkeys(items))
    if items:
        _models_cache.update(at=now, items=tuple(items))
        return tuple(items)
    if _models_cache["items"]:
        return _models_cache["items"]
    return FALLBACK_MODELS


def resolve_model(model: str | None) -> str | None:
    m = (model or "").strip()
    if m.startswith(DEFAULT_PROVIDER + "/"):
        m = m.split("/", 1)[1]
    m = m.split("#", 1)[0].strip().lower()
    if not m:
        return None
    for full in free_models():
        fid = full.split("/", 1)[1].split("#", 1)[0].lower()
        if fid == m:
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


def run_opencode(model: str, prompt: str, timeout: int = OC_TIMEOUT) -> tuple[int, str, str, int]:
    base = opencode_base()
    if not base:
        raise RuntimeError("opencode CLI not found on PATH")
    argv = [*base, "run", "--agent", OC_AGENT, "--format", "json", "--model", model, prompt]
    os.makedirs(WORKSPACE, exist_ok=True)
    t0 = time.monotonic()
    with _oc_lock:
        cp = subprocess.run(
            argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, cwd=WORKSPACE, env=_cli_env(WORKSPACE), creationflags=NO_WINDOW)
    ms = int((time.monotonic() - t0) * 1000)
    return cp.returncode, cp.stdout or "", cp.stderr or "", ms


def _record_quota_cooldown(error: dict) -> None:
    """Cache account-wide OpenCode free-tier limits so sibling model probes fail fast."""
    global _quota_cooldown_until
    text = json.dumps(error, ensure_ascii=False).lower()
    if not any(marker in text for marker in ("free-limit", "freetiererror", "free usage limit", "free tier")):
        return
    retry_ms = error.get("retryAfterMs")
    try:
        seconds = max(60.0, float(retry_ms) / 1000.0) if retry_ms is not None else _quota_cooldown_ms_default / 1000.0
    except (TypeError, ValueError):
        seconds = _quota_cooldown_ms_default / 1000.0
    with _quota_lock:
        _quota_cooldown_until = max(_quota_cooldown_until, time.monotonic() + seconds)


def parse_events(stdout: str) -> tuple[str, dict, str | None]:
    parts_by_msg: dict[str, list[str]] = {}
    order: list[str] = []
    seen: set[str] = set()
    usage: dict = {}
    error: str | None = None
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
        kind = ev.get("type")
        if kind == "text":
            pid = str(part.get("id") or "")
            text = part.get("text")
            if not isinstance(text, str) or pid in seen:
                continue
            seen.add(pid)
            mid = str(part.get("messageID") or "")
            if mid not in parts_by_msg:
                parts_by_msg[mid] = []
                order.append(mid)
            parts_by_msg[mid].append(text)
        elif kind == "step_finish":
            usage = {
                "input": (part.get("tokens") or {}).get("input"),
                "output": (part.get("tokens") or {}).get("output"),
                "cost": part.get("cost"),
            }
        elif kind == "error":
            err = part.get("error") if isinstance(part.get("error"), dict) else None
            if err is None:
                # OpenCode CLI emits failures as a top-level `error` object,
                # not consistently inside `part`. Losing it turns useful 429s
                # into generic `exit 1` bridge failures.
                err = ev.get("error") if isinstance(ev.get("error"), dict) else part
            msg = err.get("message") or err.get("name") or err.get("data") or ""
            etype = err.get("type") or err.get("name") or ""
            _record_quota_cooldown(err)
            if msg or etype:
                error = f"{etype}: {msg}".strip(": ")
    text = "\n".join("".join(parts_by_msg[m]) for m in order if "".join(parts_by_msg[m])).strip()
    return text, usage, error


# --------------------------------------------------------------------------- prompt


CONTRACT = """TASK: Decide the agent's NEXT ACTION from the transcript and tools below. Reply with EXACTLY ONE JSON object (no markdown/fences/prose).

Output (choose one):
1. {"tool_calls":[{"name":"<tool>","arguments":{...}}]}  (use ONLY tools listed under TOOLS)
2. {"content":"<final answer to the user>"}

Rules: arguments must be a JSON object ({} if none). Multiple calls in one list. Never invent tool output. If no tool needed or task complete -> {"content":"..."}."""


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


def build_prompt(messages: list, tools: list, tool_choice) -> tuple[str, bool]:
    tool_map: dict = {}
    rendered = render_messages(messages, tool_map)
    use_tools = tool_choice != "none" and bool(tools)
    budget = MAX_PROMPT_CHARS
    header = CONTRACT + "\n"
    if use_tools:
        header += "\nTOOLS:\n" + compact_tools(tools) + "\n"
    elif tool_choice == "none":
        header += '\nTools disabled: answer {"content":"..."}.\n'
    header += "\nTRANSCRIPT:\n"
    budget -= len(header) + 120

    kept: list[str] = []
    dropped = False
    for role, text in reversed(rendered):
        block = {"system": f"[S] {text}", "user": f"[U] {text}", "assistant": f"[A] {text}",
                 "assistant_call": f"[A->tools] {text}", "tool": f"[T] {text}"}[role]
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
    return body, dropped


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
    markers = ("unable to", "not available", "cannot execute", "can't execute", "cannot run",
               "can't run", "i can't", "i cannot", "max_steps", "maximum steps")
    return any(m in c for m in markers)


def account_quota_failure(text: str) -> bool:
    low = (text or "").lower()
    return any(marker in low for marker in (
        "free-limit", "free usage limit", "freetiererror", "free tier usage limit",
        "token plan usage limit reached", "usage limit reached",
    ))


def retryable_failure(rc: int, text: str, err: str | None, detail: str) -> bool:
    if rc == 0 and text:
        return False
    low = f"{err or ''} {detail}".lower()
    markers = ("429", "free-limit", "free usage limit", "rate", "limit", "quota", "timeout",
               "timed out", "unavailable", "503", "502", "overloaded", "temporar")
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
        "system_fingerprint": "opencode-bridge",
    }


def openai_error(message: str, etype: str = "api_error", status: int = 500, code: str | None = None) -> dict:
    return {"error": {"message": message, "type": etype, "param": None, "code": code}}


# --------------------------------------------------------------------------- handler


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "opencode-bridge-proxy/1.1"

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
                {"id": m, "object": "model", "created": now, "owned_by": "opencode-bridge"}
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
        rc, out, errout, ms = run_opencode(model, prompt)
        text, ev_usage, error = parse_events(out)
        detail = ""
        if rc != 0 and not text:
            detail = (error or " ".join(errout.split())[-300:] or f"exit {rc}")
        elif not text and error:
            detail = error
        parsed = parse_model_json(text) if text else None
        need_retry = bool(text) and (parsed is None or bad_content_when_tools(parsed, tools, tool_choice))
        return {"model": model, "rc": rc, "ms": ms, "text": text, "usage": ev_usage,
                "error": error, "detail": detail, "parsed": parsed, "need_retry": need_retry}

    def _chat(self, payload: dict) -> None:
        t_req = time.monotonic()
        model_req = str(payload.get("model") or "")
        if model_req and not resolve_model(model_req):
            avail = ", ".join(free_models())
            self._json(openai_error(
                f"model '{model_req}' is not an allowed OpenCode free model. Available: {avail}",
                "invalid_request_error", 400, "model_not_allowed"), 400)
            return
        candidates = order_models(model_req)
        with _quota_lock:
            quota_remaining = max(0.0, _quota_cooldown_until - time.monotonic())
        if quota_remaining > 0:
            _stats["errors"] += 1
            self._json(openai_error(
                f"OpenCode free usage limit reached; retry after about {int(quota_remaining)}s.",
                "rate_limit_error", 429, "provider_free_limit"), 429)
            return

        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            self._json(openai_error("messages must be a non-empty list", "invalid_request_error", 400), 400)
            return
        tools = payload.get("tools") if isinstance(payload.get("tools"), list) else []
        tool_choice = payload.get("tool_choice")
        stream = bool(payload.get("stream"))
        include_usage = bool((payload.get("stream_options") or {}).get("include_usage"))
        prompt, _ = build_prompt(messages, tools, tool_choice)
        prompt_chars = len(prompt)

        last_detail = "no attempt"
        chosen = None
        tried: list[str] = []
        for idx, model in enumerate(candidates[:3]):
            tried.append(model)
            try:
                a = self._attempt(model, prompt, tools, tool_choice)
            except subprocess.TimeoutExpired:
                last_detail = f"timeout after {OC_TIMEOUT}s"
                log(f"{model} -> timeout")
                if idx < len(candidates[:3]) - 1:
                    _stats["failovers"] += 1
                continue
            except RuntimeError as exc:
                last_detail = str(exc)
                break

            parsed = a["parsed"]
            text, ev_usage = a["text"], a["usage"]
            # Skip retry for tool-less chats: plain content is valid; a second
            # opencode spawn doubles latency for nothing.
            if a["need_retry"] and RETRY_PARSE_ONCE and tools:
                _stats["retries"] += 1
                log(f"{model} -> unusable reply; retrying once")
                try:
                    nudge = '\nIMPORTANT: If a tool from TOOLS applies emit {"tool_calls":[{"name":"<tool>","arguments":{...}}]} else {"content":"..."}. JSON only.'
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
            # Free-tier quota is account-wide. Trying three alternate model IDs
            # cannot restore quota and only multiplies latency and failed calls.
            if account_quota_failure(last_detail):
                log(f"{model} -> free-tier quota exhausted; stopping failover")
                break
            if retryable_failure(a["rc"], text, a["error"], last_detail) and idx < len(candidates[:3]) - 1:
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
            if "free-limit" in low or "free usage limit" in low or "429" in low:
                self._json(openai_error(
                    f"OpenCode free limit/quota (tried {', '.join(tried)}): {last_detail}",
                    "rate_limit_error", 429, "provider_free_limit"), 429)
            else:
                self._json(openai_error(
                    f"opencode-bridge: all attempts failed (tried {', '.join(tried)}): {last_detail}",
                    "api_error", 502), 502)
            return

        model_used = chosen["model"]
        parsed = chosen["parsed"]
        ev_usage = chosen["usage"]
        sid = f"chatcmpl-ocb-{uuid.uuid4().hex[:12]}"

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
    opencode_base()
    ms = free_models(force=True)
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    srv.daemon_threads = True
    log(f"opencode-bridge proxy v1.1 on http://{BIND}:{PORT}/v1  (workspace: {WORKSPACE})")
    log(f"free models: {len(ms)}  preferred: {list(PREFERRED)}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
