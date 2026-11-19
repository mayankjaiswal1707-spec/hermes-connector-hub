#!/usr/bin/env python3
"""nvidia-bridge - OpenAI-compatible endpoint over NVIDIA NIM free models.

Hermes -> this proxy -> integrate.api.nvidia.com/v1/chat/completions

Only models verified working with the provided key are exposed.
Bearer-key enforced. Zero runtime dependencies (stdlib only).

Env:
  NVIDIA_API_KEY             NVIDIA NIM key (in hermes .env)
  NVIDIA_BRIDGE_PROXY_KEY    bearer key Hermes uses (in hermes .env)
  NVIDIA_BRIDGE_PROXY_PORT   default 8382
  NVIDIA_BRIDGE_TIMEOUT      default 60s
"""

import json
import os
import re
import threading
import time
import urllib.request
import urllib.error
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROXY_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("NVIDIA_BRIDGE_PROXY_PORT", "8382"))
BIND = os.environ.get("NVIDIA_BRIDGE_PROXY_BIND", "127.0.0.1")
TIMEOUT = int(os.environ.get("NVIDIA_BRIDGE_TIMEOUT", "60"))
API = "https://integrate.api.nvidia.com/v1/chat/completions"

# Only models verified working with the provided NVIDIA NIM key.
FREE_MODELS = ("nvidia/nemotron-3.5-lightning-30b-a3b", "nvidia/nemotron-3.5-bolt-e4b", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning", "nvidia/nemotron-3.5-super-vl-120b-a12b", "nvidia/nemotron-3-ultra-550b-a55b", "minimaxai/minimax-m3.1-flash", "meta/muse-glimmer-30b", "poolside/laguna-xs-2.1", "stepfun-ai/step-5-preview", "mistralai/mistral-large-4-1t-a52b")
PREFERRED = ("minimaxai/minimax-m3.1-flash", "nvidia/nemotron-3.5-lightning-30b-a3b", "nvidia/nemotron-3.5-bolt-e4b", "meta/muse-glimmer-30b", "poolside/laguna-xs-2.1", "stepfun-ai/step-5-preview", "nvidia/nemotron-3-ultra-550b-a55b")

_stats = {"requests": 0, "tool_calls": 0, "content": 0, "errors": 0,
          "failovers": 0, "started": time.time(), "last_ms": None, "last_model": None}

CONTRACT = """You are an assistant responding inside an agent runtime. Answer the user directly and concisely.\n\nCRITICAL RULES:\n- For greetings, simple questions, tests, "reply with only X", or anything answerable with text: output ONLY a plain text answer. NEVER call a tool.\n- Only call a tool when the user explicitly asks for a real external action (run a command, read/write a file, fetch live data).\n- Always finish with a plain text answer; never end on a tool call.\n\nIf a tool is genuinely required, output exactly one JSON object: {"tool_calls":[{"name":"<tool>","arguments":{...}}]} using only the listed TOOLS.\nOtherwise output exactly: {"content":"<your answer>"}"""


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(os.path.join(PROXY_DIR, "..", "nvidia-bridge.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def api_key():
    return os.environ.get("NVIDIA_API_KEY") or ""


def resolve_model(model):
    m = (model or "").strip().lower()
    if m in FREE_MODELS:
        return m
    short = re.sub(r"^(nvidia|nvidia-free)/", "", m)
    for full in FREE_MODELS:
        if full == short or full.endswith("/" + short):
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


def compact_tools(tools):
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
        args = [f"{k}{'*' if k in req else ''}:{(v.get('type') if isinstance(v, dict) else None) or 'any'}"
                for k, v in list(props.items())[:10]]
        lines.append(f"- {name}({{{','.join(args)}}}): {desc}")
    return "\n".join(lines)


def build_prompt(messages, tools, tool_choice):
    rendered = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "").lower()
        c = msg.get("content")
        if isinstance(c, list):
            c = " ".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
        c = str(c or "")[:2200]
        if role in ("system", "developer"):
            rendered.append("[S] " + c[:900])
        elif role == "user":
            rendered.append("[U] " + c)
        elif role == "assistant":
            rendered.append("[A] " + c)
        elif role == "tool":
            rendered.append("[T] " + c[:1600])
    use_tools = tool_choice != "none" and bool(tools)
    header = "Decide NEXT ACTION. Output ONLY one JSON object.\n"
    if use_tools:
        header += "TOOLS:\n" + compact_tools(tools) + "\n"
    return CONTRACT + "\n\n" + header + "TRANSCRIPT:\n" + "\n".join(rendered) + "\nReply with JSON only."


def call_nvidia(model, prompt):
    key = api_key()
    if not key:
        raise RuntimeError("NVIDIA_API_KEY not configured")
    body = {"model": model, "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 2048, "temperature": 0}
    req = urllib.request.Request(
        API, data=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json", "authorization": f"Bearer {key}"},
        method="POST")
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        status = 200
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        status = e.code
    return status, raw, int((time.monotonic() - t0) * 1000)


def extract_text(raw):
    try:
        obj = json.loads(raw)
    except Exception:
        return "", f"bad JSON: {raw[:160]}"
    if isinstance(obj.get("error"), dict):
        e = obj["error"]
        return "", f"nvidia {e.get('code')}: {str(e.get('message'))[:200]}"
    try:
        return (obj["choices"][0]["message"].get("content") or "").strip(), ""
    except Exception:
        return "", f"unexpected response: {raw[:160]}"


def parse_model_json(text):
    if not text:
        return None
    t = re.sub(r"^```(?:json)?\s*", "", text.strip())
    t = re.sub(r"\s*```$", "", t).strip()
    dec = json.JSONDecoder()
    for i in range(len(t)):
        if t[i] != "{":
            continue
        try:
            obj, _ = dec.raw_decode(t[i:])
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


def classify(status, err):
    low = err.lower()
    if status == 429 or "rate" in low or "频繁" in low or "限流" in low:
        return "busy"
    if status == 403 or "无权" in low or "quota" in low:
        return "quota"
    return "fail"


def err_obj(msg, status=500, code=None):
    return {"error": {"message": msg, "type": "api_error", "param": None, "code": code}}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _auth(self):
        want = os.environ.get("NVIDIA_BRIDGE_PROXY_KEY") or ""
        return (not want) or self.headers.get("Authorization") == f"Bearer {want}"

    def _send(self, code, obj):
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
            return self._send(200, {"ok": True, "port": PORT, "key": bool(api_key()),
                                    "models": list(FREE_MODELS),
                                    "stats": {**_stats, "uptime_s": int(time.time() - _stats["started"])}})
        if path in ("/v1/models", "/models"):
            if not self._auth():
                return self._send(401, err_obj("unauthorized", 401))
            return self._send(200, {"object": "list", "data": [
                {"id": m, "object": "model", "created": 0, "owned_by": "nvidia-bridge"} for m in FREE_MODELS]})
        return self._send(404, err_obj(f"unknown path {path}", 404))

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path not in ("/v1/chat/completions", "/chat/completions"):
            return self._send(404, err_obj(f"unknown path {path}", 404))
        if not self._auth():
            return self._send(401, err_obj("unauthorized: bad bearer key", 401))
        try:
            n = int(self.headers.get("content-length") or 0)
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            return self._send(400, err_obj(f"bad request: {e}", 400))
        _stats["requests"] += 1
        try:
            self._chat(payload)
        except Exception as e:
            _stats["errors"] += 1
            log(f"internal error: {type(e).__name__}: {e}")
            try:
                self._send(500, err_obj(f"{type(e).__name__}: {e}"))
            except Exception:
                pass

    def _chat(self, payload):
        model_req = str(payload.get("model") or "")
        if model_req and not resolve_model(model_req):
            return self._send(400, err_obj(
                f"model '{model_req}' not allowed. Available: {', '.join(FREE_MODELS)}", 400))
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            return self._send(400, err_obj("messages must be non-empty", 400))
        tools = payload.get("tools") if isinstance(payload.get("tools"), list) else []
        prompt = build_prompt(messages, tools, payload.get("tool_choice"))
        last = "no attempt"
        for model in order_models(model_req):
            status, raw, ms = call_nvidia(model, prompt)
            text, error = extract_text(raw)
            if status == 200 and text:
                parsed = parse_model_json(text) or {"content": text}
                # GLM sometimes emits tool_calls JSON even when no tools were
                # supplied — treat that as a plain chat answer instead.
                if "tool_calls" in parsed and not tools:
                    clean = re.sub(r"^```(?:json)?\s*", "", text.strip())
                    clean = re.sub(r"\s*```$", "", clean).strip()
                    parsed = {"content": clean or text}
                if "tool_calls" in parsed:
                    calls = [{"id": f"call_{uuid.uuid4().hex[:10]}", "type": "function",
                              "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
                             for c in parsed["tool_calls"]]
                    msg = {"role": "assistant", "content": None, "tool_calls": calls, "refusal": None}
                    finish = "tool_calls"
                    _stats["tool_calls"] += len(calls)
                else:
                    msg = {"role": "assistant", "content": parsed["content"], "refusal": None}
                    finish = "stop"
                    _stats["content"] += 1
                _stats["last_ms"] = ms
                _stats["last_model"] = model
                log(f"{model} -> {finish} in {ms}ms")
                pt = max(1, len(prompt) // 4)
                ct = max(1, len(text) // 4)
                return self._send(200, {
                    "id": f"chatcmpl-zmb-{uuid.uuid4().hex[:12]}", "object": "chat.completion",
                    "created": int(time.time()), "model": model,
                    "choices": [{"index": 0, "message": msg, "finish_reason": finish, "logprobs": None}],
                    "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct},
                    "system_fingerprint": "nvidia-bridge"})
            last = error or f"HTTP {status}"
            bucket = classify(status, last)
            log(f"{model} -> {bucket} ({last[:90]})")
            # Rate limits are account-wide/short-lived: don't fall back to a
            # different (possibly worse) model — surface a clean 429 instead.
            if bucket in ("quota", "busy"):
                break
            _stats["failovers"] += 1
        _stats["errors"] += 1
        return self._send(429 if classify(0, last) == "quota" else 502,
                          err_obj(f"nvidia-bridge: no model responded: {last}"))


def main():
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    srv.daemon_threads = True
    log(f"nvidia-bridge on http://{BIND}:{PORT}/v1 (key check: {'ON' if os.environ.get('NVIDIA_BRIDGE_PROXY_KEY') else 'OFF'})")
    log(f"free models: {list(FREE_MODELS)}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()




