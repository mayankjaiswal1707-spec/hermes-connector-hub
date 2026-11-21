# Hermes Connector Hub

A local, self-hosted connector layer that lets [Hermes Agent](https://github.com) talk to free AI model tiers through OpenAI-compatible bridges — with a live status dashboard, automatic quota-aware fallback, and zero runtime dependencies (Python stdlib only).

> Educational / personal-use tool. All providers are third-party free tiers; you must comply with their terms. No keys or credentials are included in this repository.

## What it does

```
Hermes Agent
   │
   ├── cline-free        → http://127.0.0.1:8378/v1   (Cline CLI)
   ├── opencode-free     → http://127.0.0.1:8377/v1   (OpenCode CLI)
   ├── antigravity-free  → http://127.0.0.1:8379/v1   (Antigravity `agy` CLI)
   ├── gemini-free       → http://127.0.0.1:8380/v1   (Google Gemini API key, free tier)
   ├── zhipu-free        → http://127.0.0.1:8381/v1   (Zhipu/Z.ai GLM free Flash models)
   ├── nvidia-free       → http://127.0.0.1:8382/v1   (NVIDIA NIM key, working models)
   ├── openrouter-free   → http://127.0.0.1:8383/v1   (OpenRouter :free models, native pass-through)
   ├── nous-free         → http://127.0.0.1:8384/v1   (Hermes Nous Portal free models via your login)
   ├── freetoken-free    → http://127.0.0.1:8385/v1   (freetoken.website models - account needs credit)
   └── auto-free         → http://127.0.0.1:8399/auto/v1  (smart combo router)
                                  │
                    Connector Hub dashboard/API :8399
```

- **3 bridges** wrap local CLIs as OpenAI-compatible chat endpoints, plus a **Gemini bridge** that calls Google's free-tier API directly with your own `GOOGLE_API_KEY`.
- **Connector Hub** registers providers, probes every free model, writes working models into Hermes config, and serves the dashboard at `http://127.0.0.1:8399/`.
- **Auto Combo** is a virtual provider that chains the three bridges, skips quota/failing models, and forwards to the first healthy one. One request, automatic fallback.
- **Routing brain ("think before acting"):** adaptive scoring by success rate, EWMA latency, and failure streak; circuit-breaker cooldown after 3 consecutive failures; selectable strategies `adaptive` / `fastest` / `priority` / `round_robin` via the dashboard or `POST /api/routing`. TCP preflight skips dead bridges instantly; cached routing state avoids a disk read per request.
- **Bridge watchdog:** checks each bridge port every 60s and auto-restarts dead ones via the existing autostart wrappers (with 180s restart-storm protection).
- **Status truth:** `working` / `quota` / `failing` badges reflect real upstream responses. Quota-limited models are kept as "Retry Later" and never auto-deleted. The global `model:` default is never touched.

## Features

- One-click **Test all models** (parallel probing, account-limit fast-skip).
- Live dashboard with totals, per-model errors, combo chain visualization, and catalog refresh timestamp.
- Automatic background reconcile every 30 min (`CONNECTOR_AUTO_REFRESH_MIN`) — adds new upstream models, retires vanished ones.
- Bearer-key enforcement on every bridge; keys stored in Hermes `.env`, never in config or repo.
- OAuth (RFC 8628 device flow) and token-store providers supported.
- Logon autostart via HKCU Run + hidden launcher (no admin required).

## Requirements

- Windows (tested), Python 3.10+ (3.14 recommended).
- Hermes Agent with a `config.yaml` and `.env`.
- Free-tier CLIs installed and authenticated: `cline`, `opencode`, and/or `agy` (Antigravity CLI at `~/.gemini/bin/agy.exe`).

## Install

1. Copy `connector/` and `bridges/` into your Hermes home:
   ```
   %LOCALAPPDATA%\hermes\connector\connector.py
   %LOCALAPPDATA%\hermes\cline-bridge\proxy\proxy.py
   %LOCALAPPDATA%\hermes\opencode-bridge\proxy\proxy.py
   %LOCALAPPDATA%\hermes\antigravity-bridge\proxy\proxy.py
   ```
2. Generate bearer keys into Hermes `.env`:
   ```
   CLINE_BRIDGE_PROXY_KEY=<random>
   OPENCODE_BRIDGE_PROXY_KEY=<random>
   ANTI_GRAVITY_BRIDGE_PROXY_KEY=<random>
   GEMINI_BRIDGE_PROXY_KEY=<random>
   GOOGLE_API_KEY=<your Google AI Studio key>   # only for gemini-free
   ```
3. Register providers and probe:
   ```
   hermes-connectors.cmd seed
   hermes-connectors.cmd refresh-models
   hermes-connectors.cmd test --all
   ```
4. Start the hub: `hermes-connectors.cmd serve`, open `http://127.0.0.1:8399/`.
5. Optional autostart: `hermes-connectors.cmd autostart --on`.

The bundled `install.ps1` automates steps 1–5 (run it from your Hermes home).

## Usage

```powershell
# dashboard
http://127.0.0.1:8399/

# CLI
hermes-connectors.cmd status
hermes-connectors.cmd test cline-free --all
hermes-connectors.cmd retry opencode/beam-free
hermes-connectors.cmd refresh-models
hermes-connectors.cmd autostart --status

# routing brain
#   dashboard: "Routing brain" panel -> click a strategy
curl -X POST http://127.0.0.1:8399/api/routing -H "content-type: application/json" -d "{\"strategy\":\"fastest\"}"
curl http://127.0.0.1:8399/api/routing

# use a specific provider in Hermes (does not change the global default)
hermes -z "hello" --provider cline-free -m cline-free/solar-mini4

# use the auto combo
hermes -z "hello" --provider auto-free -m auto
```

## Configuration knobs (Hermes `.env`)

| Variable | Default | Meaning |
|---|---|---|
| `CONNECTOR_PROBE_CONCURRENCY` | `4` | Parallel probes per API provider (CLI bridges capped at 1) |
| `CONNECTOR_PROBE_TIMEOUT` | `120` | Client probe timeout seconds (keep > bridge timeout) |
| `CONNECTOR_PROBE_COOLDOWN_H` | `6` | Hours before a sentinel re-probe |
| `CONNECTOR_AUTO_REFRESH_MIN` | `30` | Background catalog reconcile interval |
| `CONNECTOR_WATCHDOG_SEC` | `60` | Health-check interval for auto-restarting dead bridges |
| `COMBO_ATTEMPT_TIMEOUT` | `35` | Per-model attempt timeout inside the combo router |
| `CLINE_BRIDGE_PROXY_TIMEOUT` | `90` | Cline CLI subprocess timeout |
| `ANTI_GRAVITY_BRIDGE_PROXY_TIMEOUT` | `110` | agy subprocess timeout |

## Project layout

```
connector/connector.py                hub core: state, probes, sync, auto-combo, dashboard, API
bridges/cline-bridge/proxy/proxy.py   Cline CLI bridge (8378)
bridges/opencode-bridge/proxy/proxy.py  OpenCode CLI bridge (8377)
bridges/antigravity-bridge/proxy/proxy.py  agy CLI bridge (8379)
bridges/gemini-bridge/proxy/proxy.py       Google Gemini API bridge (8380)
bridges/zhipu-bridge/proxy/proxy.py        Zhipu GLM free Flash bridge (8381)
bridges/nvidia-bridge/proxy/proxy.py       NVIDIA NIM bridge (8382)
bridges/openrouter-bridge/proxy/proxy.py   OpenRouter free-model bridge (8383)
bridges/nous-bridge/proxy/proxy.py         Nous Portal free bridge (8384)
bridges/freetoken-bridge/proxy/proxy.py    freetoken.website bridge (8385)
scripts/run-hidden.vbs                hidden launcher for autostart
tests/smoke_test.ps1                  syntax + connectivity verification
install.ps1                           installer helper
```

## Tests

```powershell
powershell -ExecutionPolicy Bypass -File tests\smoke_test.ps1
```

The smoke test compiles every Python source file and checks each local endpoint's `/health` (if services are running). It never contacts upstream model providers.

## License

MIT — see [LICENSE](LICENSE).
