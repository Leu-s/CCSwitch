<div align="center">

# CCSwitch

**Stop hitting Claude rate limits. Start using all your subscriptions.**

[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688.svg?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Tests](https://img.shields.io/badge/tests-128%20passing-brightgreen.svg)](#testing)
[![Platform](https://img.shields.io/badge/platform-macOS-lightgrey.svg)](#requirements)
[![Status](https://img.shields.io/badge/status-stable-success.svg)](#project-status)
[![Build](https://img.shields.io/badge/frontend-no%20build%20step-orange.svg)](#architecture)

</div>

If you've ever been deep in a Claude Code session and hit the 5-hour rate limit, you know the pain. **CCSwitch** is a local dashboard that watches usage across every Claude.ai subscription you own and silently swaps credentials *before* you run out — so `claude` keeps working without you noticing the switch.

<p align="center"><img src="preview.png" alt="CCSwitch dashboard" width="800"/></p>

### Who is this for?

- Solo developers running many long-lived Claude Code panes (cmux, tmux, iTerm, VS Code) against a single active account, who want to extend effective rate-limit budget over time by rotating between several paid subscriptions.
- Anyone tired of swapping `CLAUDE_CONFIG_DIR` by hand or restarting tmux windows after every `/login`.
- Users who want a single pane of glass showing five-hour and seven-day utilization across every account they own.

This is **not** a load-balancer across parallel accounts — there are open-source proxies for that (ccflare, claude-balancer, ccNexus), which currently operate in the Anthropic ToS gray zone after the February 2026 policy tightening. CCSwitch keeps the native Claude Code binary, the native macOS Keychain, and the native OAuth flow; it only rotates which credentials sit in the standard Keychain entry at any given moment. No proxy, no token interception, no API redirection.

### How it feels

You're refactoring a service in 20 tmux panes, all running `claude`. Around hour 4, the active account's usage bar turns amber. At 95 % the app atomically swaps your credentials to the next enabled account and — because the **Wake tmux sessions** toggle is on — sends a single keystroke to every pane that's stalled on a rate-limit notice. Every pane wakes up on the new account and continues from wherever it was. A toast tells you what just happened. Your build never stops.

> **macOS only** for the full credential-switching path (Keychain via `security` CLI). Linux falls back to file-only credentials.
> **tmux required** for the Add-Account login flow; the post-switch nudge also uses tmux when enabled.

---

## Table of Contents

- [Features](#features)
- [Requirements](#requirements)
- [Quick Start](#quick-start)
- [Installation](#installation)
- [Configuration](#configuration)
- [Running](#running)
- [Architecture](#architecture)
- [Project Structure](#project-structure)
- [CLI Reference](#cli-reference)
- [API](#api)
- [Testing](#testing)
- [Security](#security)
- [Troubleshooting](#troubleshooting)
- [Project Status](#project-status)
- [License](#license)
- [Acknowledgments](#acknowledgments)

---

## Features

- **Live usage monitoring** — polls `/v1/messages` rate-limit headers every 15 s while the dashboard is open, every 5 min otherwise
- **Auto-switch** — when the active account's five-hour utilization reaches a configurable threshold (default 95 %), or a stale credential is detected, the next enabled account is activated automatically
- **Keychain partition: zero refresh races by design** — CCSwitch stores every inactive account in a private `ccswitch-vault` Keychain namespace that the Claude Code CLI cannot see.  Active account lives in the standard `Claude Code-credentials` entry; the CLI owns its refresh lifecycle, CCSwitch never touches it.  CCSwitch is the sole refresher for vault accounts.  Different Keychain service name → no overlap → no single-use-refresh-token race possible.
- **Atomic credential swap** — when the dashboard flips to a new account, a 5-step orchestrator moves credentials from vault → standard under a single lock, updates `~/.claude/.claude.json`, and rewrites the fallback `.credentials.json`.  Any crash between steps is reconciled on the next startup by the integrity check.
- **Stale-account relogin** — if a refresh token is revoked or rotated by Anthropic, the dashboard marks the account stale and offers a one-click re-login flow (opens a tmux pane with `claude`, verifies new credentials, clears `stale_reason`)
- **Real-time dashboard** — vanilla-JS single-page app; account cards, drag-to-reorder priority, per-account threshold slider, switch log; no build step required
- **Optional tmux nudge** — opt-in toggle on the Settings page: after every account switch, scan every `tmux` pane and send a configurable message (default `continue`) to any pane whose recent output matches a Claude Code rate-limit notice. Off by default.
- **CLI** (`ccswitch`) — list/switch/enable/disable accounts, tail logs, manage the LaunchAgent
- **macOS LaunchAgent** — optional auto-start on login

---

## Requirements

| Dependency | Notes |
|---|---|
| macOS | Full Keychain support. Linux works with file-only credentials. |
| Python 3.12+ | Tested on 3.14 |
| [uv](https://docs.astral.sh/uv/) | Recommended. Plain `pip` also works. |
| tmux | Required for the "Add Account" login flow; also used by the opt-in post-switch nudge |

---

## Quick Start

```bash
# 1. Clone
git clone <repo-url>
cd ccswitch

# 2. Install dependencies
uv sync

# 3. Start the dev server
uv run uvicorn backend.main:app --host 127.0.0.1 --port 41924 --reload

# 4. Open the dashboard
open http://localhost:41924
```

The SQLite database and schema are created automatically on first start — no manual setup required.

---

## Installation

```bash
git clone <repo-url>
cd ccswitch
uv sync

# Optional: copy environment defaults (all have sensible defaults)
cp .env.example .env
```

### Auto-start on login (macOS LaunchAgent)

```bash
# Add scripts/ to your PATH first, or use the full path
export PATH="$PATH:$(pwd)/scripts"

ccswitch service install               # creates ~/Library/LaunchAgents/com.ccswitch.manager.plist
ccswitch service remove                # uninstall
ccswitch service remove --purge-logs   # uninstall + delete logs
```

The LaunchAgent:
- starts the server **immediately** on install (`RunAtLoad: true`)
- **auto-restarts** on crash with a 30 s throttle (`KeepAlive: {SuccessfulExit: false}`)
- **starts automatically** on every login
- writes logs to `~/.local/state/ccswitch/server.log`

```bash
ccswitch log -f                        # tail logs
ccswitch status                        # check if the service is running
launchctl print gui/$(id -u)/com.ccswitch.manager  # raw launchd status
```

---

## Configuration

All settings use the `CCSWITCH_` environment variable prefix. Copy `.env.example` and adjust only what you need — all defaults work for a standard local setup.

| Variable | Default | Description |
|---|---|---|
| `CCSWITCH_SERVER_HOST` | `127.0.0.1` | Bind address (`0.0.0.0` to listen on all interfaces) |
| `CCSWITCH_SERVER_PORT` | `41924` | HTTP server port |
| `CCSWITCH_DATABASE_URL` | `sqlite+aiosqlite:///./ccswitch.db` | SQLite connection string (relative to working dir) |
| `CCSWITCH_POLL_INTERVAL_ACTIVE` | `15` | Poll interval (seconds) while browser tab is open |
| `CCSWITCH_POLL_INTERVAL_IDLE` | `300` | Poll interval (seconds) with no active WebSocket clients |
| `CCSWITCH_POLL_INTERVAL_MIN` | `120` | Minimum floor for the DB-overridable idle interval |
| `CCSWITCH_DEFAULT_ACCOUNT_THRESHOLD_PCT` | `95.0` | Auto-switch threshold for newly added accounts (0–100) |
| `CCSWITCH_HAIKU_MODEL` | `claude-haiku-4-5-20251001` | Cheapest model used for the minimal `/v1/messages` probe (the probe only reads rate-limit headers; no tokens are billed) |
| `CCSWITCH_API_TOKEN` | *(empty)* | Optional Bearer token. **Empty = no auth** (safe for localhost). |
| `CCSWITCH_TMUX_SESSION_NAME` | `ccswitch` | Name of the tmux session used for add-account login panes |
| `CCSWITCH_WS_REPLAY_BUFFER_SIZE` | `100` | Recent `/ws` events buffered for reconnecting clients (see `?since=<seq>`) |
| `CCSWITCH_LOGIN_SESSION_TIMEOUT` | `1800` | Seconds an unused add-account login terminal stays alive before cleanup |
| `CCSWITCH_RATE_LIMIT_BACKOFF_INITIAL` | `120` | Initial backoff (seconds) after an Anthropic 429 on the usage probe |
| `CCSWITCH_RATE_LIMIT_BACKOFF_MAX` | `3600` | Cap (seconds) for the exponential retry delay after repeated 429s |
| `CCSWITCH_ANTHROPIC_MESSAGES_URL` | `https://api.anthropic.com/v1/messages` | Override the Messages endpoint (testing only) |
| `CCSWITCH_ANTHROPIC_REFRESH_URL` | `https://platform.claude.com/v1/oauth/token` | Override the OAuth token-refresh endpoint (testing only) |

No variable is mandatory — all have sensible defaults. Set `CCSWITCH_API_TOKEN` only if you expose the server beyond localhost.

---

## Running

### Development (hot reload)

```bash
uv run uvicorn backend.main:app --host 127.0.0.1 --port 41924 --reload
```

### Production (via launch script)

```bash
bash scripts/launch.sh
```

`launch.sh` checks prerequisites (tmux, `security`), manages a PID file at `~/.local/state/ccswitch/server.pid`, and writes logs to `~/.local/state/ccswitch/server.log`. It starts uvicorn without `--reload`.

### Status and logs

```bash
bash scripts/status.sh    # server health + LaunchAgent status
ccswitch status             # server, shell config, active account
ccswitch log -f             # tail logs (follow mode)
ccswitch log -n 100         # last 100 lines
```

---

## Architecture

```
┌──────────────────┐   WebSocket /ws      ┌──────────────────────┐
│  Browser         │◄────────────────────►│  FastAPI :41924      │
│  (Vanilla JS     │   HTTP /api/*        │                      │
│   ES6 modules)   │──────────────────►   │  background poll     │
└──────────────────┘                      └──────────┬───────────┘
                                                     │
                         ┌───────────────────────────┼───────────────────┐
                         ▼                           ▼                   ▼
                ┌─────────────────┐      ┌─────────────────┐  ┌──────────────────┐
                │ SQLite DB       │      │ Anthropic API   │  │ macOS Keychain   │
                │ (accounts,      │      │ /v1/messages    │  │ + ~/.claude/     │
                │  settings,      │      │ (headers only)  │  │                  │
                │  switch_log)    │      └─────────────────┘  └──────────────────┘
                └─────────────────┘
```

### How credentials are stored

Two Keychain namespaces, disjoint by service name:

| Service                   | Account       | Owner     | Purpose                                     |
|---                        |---            |---        |---                                           |
| `Claude Code-credentials` | `$USER`       | Claude Code CLI | The live credentials the CLI reads on every API call.  CCSwitch writes this only during an account swap. |
| `ccswitch-vault`          | account email | CCSwitch  | Private store for inactive accounts.  Invisible to the CLI (different service name).  CCSwitch is the sole refresher.  |

Invariant: each account's `refresh_token` lives in exactly one Keychain entry at any instant.  A swap physically moves credentials between the two services.  The CLI cannot enumerate Keychain entries by owner — its lookups are targeted at the exact `Claude Code-credentials` service — so vault entries are race-free by construction.

### Data flow

1. **Startup** — `init_db()` runs Alembic migrations (creates the DB on first run, or on upgrade performs the one-shot vault migration from the legacy per-account-dir layout).  Seeds default settings.  Waits for the login keychain to unlock (exponential backoff up to 5 minutes, for the LaunchAgent-at-boot case where FileVault delays unlock).  Runs a startup integrity check to reconcile any crashed-mid-swap state.  Spawns the poll loop and the login-session cleanup loop.

2. **Poll cycle** — Every 15 s with active WebSocket clients, every 5 min when idle.  Per account:
   - *Active account* — reads the access token from the standard Keychain entry, POSTs a near-empty request to `/v1/messages` to read the `anthropic-ratelimit-unified-*` response headers, stores the result.  **Never refreshes.**  The CLI owns the active account's refresh lifecycle.
   - *Vault account* — reads the access token from `ccswitch-vault / email`.  If it's within 20 minutes of expiry, CCSwitch refreshes via Anthropic's `/oauth/token` endpoint and persists the rotated token back into the vault entry.  Probes and stores.  The CLI cannot see this entry, so the refresh is race-free.
   On a probe 401 for the active account, CCSwitch fires a one-shot tmux nudge to wake any sleeping `claude` pane (which will refresh the standard entry on its next API call) and returns the last-known cached usage.  On sleep/wake detection (monotonic gap > 5 min), a 0–30 s random stagger runs before the refresh burst to avoid tripping Anthropic's refresh-endpoint rate limit.  Accounts that return 429 enter per-account exponential backoff (120 s → 3600 s cap).

3. **Auto-switch** — If the active account's five-hour utilization ≥ `threshold_pct` (or it returned 429, or has a `stale_reason`), `perform_switch()` calls `swap_to_account(email)` under a single asyncio lock:
   1. Read `ccswitch-vault / target_email` — the incoming credentials.
   2. Read the standard `Claude Code-credentials` entry immediately before the overwrite, and write those tokens into the vault entry for the *outgoing* email (preserving any last-moment CLI rotation).
   3. Write the incoming credentials to the standard entry.
   4. Atomically rewrite `~/.claude/.claude.json` — replace only `oauthAccount` + `userID`, preserve every other key (projects, MCP state, user prefs).
   5. Atomically rewrite `~/.claude/.credentials.json` as a file-fallback mirror.
   Log `switch_log`, broadcast `account_switched`, fire the tmux nudge so every running `claude` pane wakes up on the new credentials.

4. **Running panes wake up** — Claude Code re-reads the Keychain after each nudge; one `continue` keystroke per pane is enough to pick up the new account and resume from where it was.  Panes that were idle at the moment of the swap just use the fresh credentials on their next API call.

---

## Project Structure

```
├── backend/
│   ├── main.py                        # FastAPI app, lifespan, /ws, static serving
│   ├── config.py                      # Pydantic settings (CCSWITCH_ prefix)
│   ├── models.py                      # ORM: Account, SwitchLog, Setting
│   ├── database.py                    # Async SQLAlchemy engine + Alembic init_db()
│   ├── schemas.py                     # Pydantic request/response models
│   ├── cache.py                       # Thread-safe in-memory usage + token_info cache
│   ├── auth.py                        # Optional Bearer token middleware
│   ├── ws.py                          # WebSocketManager (broadcast, replay_since, seq stamping)
│   ├── background.py                  # Per-account poll + vault refresh + auto-switch
│   ├── routers/
│   │   ├── accounts.py                # /api/accounts CRUD + login flow (add + relogin)
│   │   ├── service.py                 # /api/service enable/disable/default-account
│   │   └── settings.py                # /api/settings tmux nudge + poll interval
│   └── services/
│       ├── account_service.py         # swap_to_account orchestrator + vault account lifecycle
│       ├── account_queries.py         # DB query helpers
│       ├── login_session_service.py   # Scratch-dir login sessions (add + relogin)
│       ├── credential_provider.py     # Vault + standard Keychain helpers, _credential_lock
│       ├── anthropic_api.py           # probe_usage() + refresh_access_token()
│       ├── switcher.py                # get_next_account, perform_switch, maybe_auto_switch
│       ├── settings_service.py        # Typed get/set for Setting DB rows
│       └── tmux_service.py            # tmux pane ops + wake_stalled_sessions + fire_nudge
├── frontend/
│   ├── index.html                     # Accounts page + Settings page + Add-account modal
│   └── src/
│       ├── main.js                    # Entry point: theme, page toggle, keyboard shortcuts
│       ├── api.js                     # fetch wrapper (30 s timeout, error extraction)
│       ├── ws.js                      # WebSocket + exponential backoff + replay
│       ├── state.js                   # Shared mutable state
│       ├── constants.js               # Timing constants
│       ├── utils.js                   # DOM helpers, date/time formatters
│       ├── toast.js                   # Toast notification system
│       ├── style.css                  # Dark/light theme, all components
│       ├── favicon.svg                # Browser tab icon
│       └── ui/
│           ├── accounts.js            # Account cards, drag-reorder, threshold slider, default-account selector
│           ├── service.js             # Master-switch button (service enable/disable)
│           ├── log.js                 # Switch log + pagination
│           ├── login.js               # Add-account + re-login modal (multi-step tmux login)
│           └── tmux_nudge.js          # Settings-page Wake Tmux Sessions block
├── alembic/                           # Database migrations (run on startup)
│   └── versions/                      # Alembic-backed schema migrations
├── scripts/
│   ├── ccswitch / ccswitch.py         # CLI tool
│   ├── launch.sh                      # Production server launcher
│   ├── status.sh                      # Server health check
│   ├── create_system_service.sh       # macOS LaunchAgent installer
│   └── remove_system_service.sh       # LaunchAgent removal
└── tests/                             # router + service + background + schemas + e2e
```

---

## CLI Reference

Add `scripts/` to your `PATH`, or run `python scripts/ccswitch.py <command>` directly.

```bash
ccswitch list                            # List all accounts with active marker and usage
ccswitch switch <email>                  # Switch active account immediately
ccswitch enable <email>                  # Include account in auto-switch rotation
ccswitch disable <email>                 # Exclude account from auto-switch rotation
ccswitch status                          # Server health, active account
ccswitch server start                    # Launch server in a new tmux window
ccswitch server stop                     # Stop server (and unload LaunchAgent if running)
ccswitch log [-f] [-n N]                # View server logs (-f: follow, -n: line count)
ccswitch service install                 # Install macOS LaunchAgent (auto-start on login)
ccswitch service remove [--purge-logs]   # Uninstall LaunchAgent (optionally delete logs)
```

The CLI connects to `http://127.0.0.1:41924` by default. Override with `CCSWITCH_SERVER_HOST`/`CCSWITCH_SERVER_PORT` env vars, or `CCSWITCH_URL` for a full URL.

---

## API

All `/api/*` routes require `Authorization: Bearer <token>` when `CCSWITCH_API_TOKEN` is set. The WebSocket passes the token via `?token=<value>` (browser WebSocket API does not support custom headers). The paths `/`, `/src/*`, and `/health` are always public.

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/accounts` | List accounts with live usage data |
| `POST` | `/api/accounts/start-login` | Open tmux pane for new account login |
| `POST` | `/api/accounts/verify-login` | Verify login completed, save account |
| `DELETE` | `/api/accounts/cancel-login` | Cancel and clean up a login session |
| `GET` | `/api/accounts/login-sessions/{session_id}/capture` | Tail recent terminal output from a login pane (`?lines=10..500`) |
| `POST` | `/api/accounts/login-sessions/{session_id}/send` | Send keystrokes to a login pane (JSON body: `{ "text": "..." }`) |
| `PATCH` | `/api/accounts/{id}` | Update account (`enabled`, `threshold_pct`, `priority`) |
| `DELETE` | `/api/accounts/{id}` | Delete account |
| `POST` | `/api/accounts/{id}/switch` | Manual switch to account |
| `POST` | `/api/accounts/{id}/relogin` | Open tmux pane to re-authenticate a stale account |
| `POST` | `/api/accounts/{id}/relogin/verify` | Verify re-login completed, clear `stale_reason` |
| `GET` | `/api/accounts/log` | Paginated switch log |
| `GET` | `/api/accounts/log/count` | Total switch log entry count |
| `GET` | `/api/service` | Service status (`enabled`, `active_email`, `default_account_id`) |
| `POST` | `/api/service/enable` | Enable auto-switching (preserves existing active account when valid) |
| `POST` | `/api/service/disable` | Disable auto-switching |
| `PATCH` | `/api/service/default-account` | Set starting account for enable |
| `GET` | `/api/settings` | List settings |
| `PATCH` | `/api/settings/{key}` | Update a setting (`usage_poll_interval_seconds`, `tmux_nudge_*`) |
| `GET` | `/health` | Health check (always public) |

**WebSocket:** `ws://localhost:41924/ws?since=<seq>` — streams `usage_updated`, `account_switched`, `account_deleted`, and `error` events. The `since` parameter requests buffered events the client may have missed; the server falls back to a full snapshot if the buffer gap is too large.

---

## Testing

```bash
uv run python -m pytest tests/ -q                  # all 128 tests
uv run python -m pytest tests/ -v --tb=short       # verbose output
uv run pytest tests/test_accounts_router.py        # single file
```

Tests create isolated SQLite databases in a pytest-managed temp directory — no environment setup, no external services, no API keys required. The `asyncio_mode = "auto"` setting in `pyproject.toml` means async test functions are detected automatically without explicit decorators.

---

## Security

**This application is designed for localhost use only.**

- The SQLite database is unencrypted and stores account metadata (emails, config paths)
- By default, `CCSWITCH_API_TOKEN` is empty — **all local requests are accepted without authentication**, which is appropriate when the server binds to `127.0.0.1`
- There is no CORS configuration — the frontend and API share the same origin
- If you set an API token, WebSocket connections pass it as `?token=...` in the URL, which appears in server access logs

**If you need to expose the server beyond localhost:**

1. Set `CCSWITCH_API_TOKEN` to a strong random value: `openssl rand -hex 32`
2. Run behind a TLS-terminating reverse proxy (nginx, Caddy) for HTTPS/WSS
3. Restrict network access via firewall rules

---

## Troubleshooting

**Server won't start**
- Ensure tmux is installed: `brew install tmux`
- Ensure `security` is available (macOS only): `which security`
- Check for a stale PID file: `rm ~/.local/state/ccswitch/server.pid`

**Account switch not picked up in an existing `claude` pane**
- Enable the **Wake tmux sessions** toggle in Settings; it nudges stalled panes automatically after every swap.
- Or press Enter / type any keystroke in the pane — the CLI re-reads the Keychain on its next request and will pick up the new credentials.

**WebSocket indicator stays disconnected**
- Verify the server is running: `curl http://localhost:41924/health`
- Check logs: `ccswitch log -n 50`

**`ccswitch` reports "cannot connect"**
- Start the server first: `ccswitch server start` or `bash scripts/launch.sh`
- If using a non-default port: `export CCSWITCH_SERVER_PORT=PORT` or `export CCSWITCH_URL=http://localhost:PORT`

**Usage card shows "Rate limited"**
- Expected: the app backs off automatically and retries. No action needed.

**LaunchAgent doesn't start after reboot**
- Re-install: `ccswitch service remove && ccswitch service install`
- Check system log: `log show --predicate 'subsystem == "com.apple.launchd"' --last 5m | grep ccswitch`

**Database schema error after upgrade**
- Alembic runs `upgrade head` automatically on every server start — no manual migration step needed.

---

## Project Status

**Stable** — running 24/7 as a personal LaunchAgent on the maintainer's machine. The codebase has been through several rounds of audited refactoring (128 tests covering routers, services, background loop, schemas, and the swap orchestrator). The public API and database schema are considered stable; breaking changes ship with an Alembic migration.

What is intentionally **not** done:
- No multi-tenant support — one user per machine
- No cloud sync — everything lives in local SQLite + macOS Keychain
- No Linux Keychain integration — Linux support would require an analogous vault/standard credential store
- No rollback path for the one-shot vault migration (a one-time JSON backup is written to `~/.ccswitch-backup-2026-04-15.json` on the first upgrade)

---

## Prior art and positioning

The OSS ecosystem around Claude multi-account management has converged on two branches. CCSwitch picks a third.

**Branch 1 — `CLAUDE_CONFIG_DIR` per-profile wrappers** (the majority).
Examples: [diranged/claude-profile](https://github.com/diranged/claude-profile), [burakdede/aisw](https://github.com/burakdede/aisw), [realiti4/claude-swap](https://github.com/realiti4/claude-swap), [kzheart/claude-code-switcher](https://github.com/kzheart/claude-code-switcher), [Second-Victor/cc-account-switcher-zsh](https://github.com/Second-Victor/cc-account-switcher-zsh), [ming86/cc-account-switcher](https://github.com/ming86/cc-account-switcher) (archived Feb 22, 2026 after Anthropic's ToS clarification). Each account lives in its own config directory; Claude Code's own Keychain-hashing (`sha256(config_dir)[:8]`) provides isolation. Switching is usually a shell-level env var flip and a manual `claude` restart. **No auto-switch.** The closest one to auto-switch is [dr5hn/ccm](https://github.com/dr5hn/ccm) (~15 stars, `ccm watch --threshold N --auto`) — a bash script without a dashboard.

**Branch 2 — Local-proxy routers** (intercept `ANTHROPIC_BASE_URL`).
Examples: [snipeship/ccflare](https://github.com/snipeship/ccflare) (~945 stars), [tombii/better-ccflare](https://github.com/tombii/better-ccflare), [snipeship/claude-balancer](https://github.com/snipeship/claude-balancer), [lich0821/ccNexus](https://github.com/lich0821/ccNexus), [codeking-ai/cligate](https://github.com/codeking-ai/cligate), [CaddyGlow/ccproxy-api](https://github.com/CaddyGlow/ccproxy-api), [router-for-me/CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI). These route subscription traffic through a local HTTP server that swaps credentials per request. **Powerful but operating in the Anthropic ToS gray zone** after the February 2026 policy clarification; several (better-ccflare, ccflare itself) have removed round-robin / tier-balancing to stay on the safe side.

**Branch 3 — Native-CLI vault partition + in-place swap** (CCSwitch).
Keep the native `claude` binary, the native macOS Keychain, and the native OAuth flow. Store inactive accounts in a private `ccswitch-vault` Keychain service namespace the CLI does not know about. Swap credentials into the standard `Claude Code-credentials` entry on a rate-limit trigger driven by proactive `/v1/messages` header polling. Nudge running tmux panes so they pick up the new identity without restart. No proxy, no token interception, no API redirection. **ToS-safe by construction.**

No OSS project found with the full combination of (a) private Keychain namespace for inactive accounts, (b) proactive polling of `anthropic-ratelimit-unified-*` response headers, (c) automatic swap before threshold crossing, and (d) a real-time dashboard. The architectural parallels in other ecosystems are [`aws-vault`](https://github.com/99designs/aws-vault) (keychain partition, but `exec` model instead of promote-to-active) and [`gh auth switch`](https://github.com/cli/cli/blob/trunk/docs/multiple-accounts.md) (in-place identity flip via a hosts file, no auto-switch, no keychain namespace).

The upstream feature requests in [anthropics/claude-code#20131](https://github.com/anthropics/claude-code/issues/20131) ("Multi-Account Profile Support", 54+ upvotes) and [#30031](https://github.com/anthropics/claude-code/issues/30031) ("Support like gh auth switch", 22+ upvotes) remain open as of this writing.

---

## License

This project does not currently ship with a license file, which means the source is **All Rights Reserved** by default under copyright law. You may read the code and run it locally for personal use, but redistribution, derivative works, and commercial use are not granted. If you need an explicit license for your use case, open an issue.

---

## Acknowledgments

- Built on [FastAPI](https://fastapi.tiangolo.com/), [SQLAlchemy](https://www.sqlalchemy.org/), and [uv](https://docs.astral.sh/uv/).
- Inspired by the daily reality of hitting Claude Code's five-hour rate window mid-refactor.
- Architecture refined across multiple agent-assisted refactoring passes — see `CLAUDE.md` for the architecture tour aimed at future AI sessions.
