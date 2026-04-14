<div align="center">

# CCSwitch

**Stop hitting Claude rate limits. Start using all your subscriptions.**

[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688.svg?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Tests](https://img.shields.io/badge/tests-218%20passing-brightgreen.svg)](#testing)
[![Platform](https://img.shields.io/badge/platform-macOS-lightgrey.svg)](#requirements)
[![Status](https://img.shields.io/badge/status-stable-success.svg)](#project-status)
[![Build](https://img.shields.io/badge/frontend-no%20build%20step-orange.svg)](#architecture)

</div>

If you've ever been deep in a Claude Code session and hit the 5-hour rate limit, you know the pain. **CCSwitch** is a local dashboard that watches usage across every Claude.ai subscription you own and silently swaps credentials *before* you run out — so `claude` keeps working without you noticing the switch.

<p align="center"><img src="preview.png" alt="CCSwitch dashboard" width="800"/></p>

### Who is this for?

- Power users running long Claude Code sessions who burn through a single subscription's rate window
- Teams or solo developers paying for multiple Claude Pro / Max accounts and tired of swapping `CLAUDE_CONFIG_DIR` by hand
- Anyone who wants a single pane of glass showing five-hour and seven-day utilization across every account they own

### How it feels

You're refactoring a service. The dashboard sits in a tab. Around hour 4, the active account's usage bar turns amber. At 95 % the app silently activates your second account, and — if you enabled the **Wake tmux sessions** toggle in Settings — scans every tmux pane and sends your configured message (default `continue`) to any `claude` session that's stalled on a rate-limit notice. A toast tells you what just happened. Your build never stops.

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
- [Shell Integration](#shell-integration)
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
- **Stale-account relogin** — if a refresh token is revoked or expires, the dashboard marks the account stale and offers a one-click re-login flow (opens a tmux pane with `claude`, verifies new credentials, clears `stale_reason`)
- **Opt-in credential targets** — the dashboard auto-discovers every `.claude.json` location on the machine and lets you tick which ones the switcher should mirror into. Nothing outside the isolated account dirs is touched unless you explicitly opt in.
- **Two switch modes per target**:
  - *Identity-only mirror* — the default for any user-enabled target: only `oauthAccount` and `userID` are mirrored, no credentials leave the account dir
  - *System default* (`~/.claude.json` or `~/.claude/.claude.json`) — additionally writes the legacy `Claude Code-credentials` Keychain entry and copies `.credentials.json` into `~/.claude/`, so a fresh `claude` invocation immediately uses the new account
- **Shell integration** — a one-liner in `.zshrc`/`.bashrc` exports `CLAUDE_CONFIG_DIR` from `~/.ccswitch/active`, so every new terminal picks up the active account without a restart
- **Real-time dashboard** — vanilla-JS single-page app; account cards, drag-to-reorder priority, per-account threshold slider, switch log; no build step required
- **Optional tmux nudge** — opt-in toggle on the Settings page: after every account switch, scan every `tmux` pane and send a configurable message (default `continue`) to any pane whose recent output matches a rate-limit notice (`usage limit reached`, `rate_limit_error`, HTTP 429, …). Off by default.
- **CLI** (`ccswitch`) — list/switch/enable/disable accounts, tail logs, manage the LaunchAgent, set up shell integration
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
| `CCSWITCH_ACTIVE_CLAUDE_DIR` | `~/.claude` | System-wide Claude Code config dir |
| `CCSWITCH_ACCOUNTS_BASE_DIR` | `~/.ccswitch-accounts` | Base dir for isolated per-account config dirs |
| `CCSWITCH_STATE_DIR` | `~/.ccswitch` | Holds the `active` pointer file |
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

## Shell Integration

To make new terminals automatically use the currently-active account, add this one-liner to `~/.zshrc` or `~/.bashrc`:

```bash
_d=$(cat ~/.ccswitch/active 2>/dev/null); [ -n "$_d" ] && export CLAUDE_CONFIG_DIR="$_d"; unset _d
```

At shell startup it reads `~/.ccswitch/active` (a pointer file updated on every account switch) and exports `CLAUDE_CONFIG_DIR` to the active account's isolated directory. Claude Code reads that variable and uses the right credentials.

**Automated setup:**

```bash
ccswitch shell setup    # appends the block above to .zshrc and/or .bashrc
```

After a switch, existing terminals can re-source their rc file (`source ~/.zshrc`) or simply open a new tab.

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
                │ SQLite DB       │      │ Anthropic API   │  │ ~/.claude/       │
                │ (accounts,      │      │ /v1/messages    │  │ ~/.ccswitch/ │
                │  settings,      │      │ (headers only)  │  │ macOS Keychain   │
                │  switch_log)    │      └─────────────────┘  └──────────────────┘
                └─────────────────┘
```

### How credentials are stored

Each account lives in its own isolated config dir under `~/.ccswitch-accounts/account-<uuid>/`. Inside that dir Claude Code keeps `.claude.json` (config + identity), and on macOS it also writes a Keychain entry whose service name is `Claude Code-credentials-<sha256(config_dir)[:8]>` (the *hashed per-dir entry*). Those two files plus the per-dir Keychain entry are the source of truth for an account — the dashboard never overwrites them on a switch.

What a switch *does* touch is determined by the **credential targets** the user has enabled in the dashboard. A target is a canonical path to a `.claude.json` file (e.g. `~/.claude.json`, `~/.claude/.claude.json`, or any other location where Claude Code looks). The dashboard auto-discovers them and shows a checkbox per target.

### Data flow

1. **Startup** — `init_db()` runs Alembic migrations (creates the DB on first run), seeds default settings, syncs `~/.ccswitch/active`, then spawns two background tasks: the poll loop and a login-session cleanup loop (reaps expired add-account sessions every 5 min).

2. **Poll cycle** — Every 15 s with active WebSocket clients, every 5 min when idle. Per account: reads the access token from the isolated config dir, refreshes it if expiring within 5 min, POSTs a near-empty request to `/v1/messages` purely to read the `anthropic-ratelimit-unified-*` response headers (five-hour and seven-day utilization + reset times). Accounts that return 429 enter per-account exponential backoff (120 s → 3600 s cap). Results are cached in memory and broadcast over WebSocket.

3. **Auto-switch** — If the active account's five-hour utilization ≥ `threshold_pct` (or it returned 429, or has a `stale_reason`), `perform_switch()` runs `activate_account_config()` under a credential lock (so a concurrent token refresh cannot interleave). For the chosen account it:
   - **Mirrors `oauthAccount` + `userID`** from the account's `.claude.json` into every user-enabled credential target — identity only, no tokens
   - **If a system-default target is enabled** (`~/.claude.json` or `~/.claude/.claude.json`), additionally writes the legacy `Claude Code-credentials` Keychain entry, cleans stale legacy Keychain entries left by older Claude Code versions, and copies `.credentials.json` into `~/.claude/` as a plaintext fallback
   - **Updates `~/.ccswitch/active`** *after* all credential operations succeed (so the pointer is never advanced to a half-installed state)
   - Logs the event in `switch_log` and broadcasts `account_switched` over WebSocket

4. **Shell pickup** — New terminals sourcing the rc snippet read `~/.ccswitch/active` and export `CLAUDE_CONFIG_DIR`; existing `claude` processes are unaffected until restarted.

> **Note on the hashed per-dir Keychain entry:** the switcher does **not** rewrite it. It is owned by the account and updated only by `save_refreshed_token` when that account's own access token is refreshed. This is intentional — each account keeps its own credentials in its own slot, and a switch only touches the *system-default* entry that fresh `claude` invocations look for.

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
│   ├── background.py                  # Per-account poll, token refresh, rate-limit backoff, auto-switch
│   ├── routers/
│   │   ├── accounts.py                # /api/accounts CRUD + login flow (add + relogin)
│   │   ├── service.py                 # /api/service enable/disable/default-account
│   │   ├── settings.py                # /api/settings + shell snippet
│   │   └── credential_targets.py      # /api/credential-targets list/rescan/toggle/sync
│   └── services/
│       ├── account_service.py         # Account lifecycle, activation, backup/restore
│       ├── account_queries.py         # DB query helpers
│       ├── login_session_service.py   # Add-account + relogin sessions (tmux, auto-expiry)
│       ├── credential_provider.py     # Keychain read/write, token get/save/wipe, _credential_lock
│       ├── credential_targets.py      # Auto-discover .claude.json targets + enable state
│       ├── anthropic_api.py           # probe_usage() + refresh_access_token()
│       ├── switcher.py                # get_next_account, perform_switch, maybe_auto_switch, relogin helpers
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
│           ├── login.js               # Add-account modal (multi-step tmux login)
│           ├── credential_targets.js  # Settings-page Credential Targets panel
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
ccswitch status                          # Server health, shell config, active account
ccswitch shell setup                     # Append CLAUDE_CONFIG_DIR one-liner to rc files
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
| `POST` | `/api/service/enable` | Enable auto-switching, activate default or first account |
| `POST` | `/api/service/disable` | Disable and restore original credentials |
| `PATCH` | `/api/service/default-account` | Set starting account for enable |
| `GET` | `/api/settings` | List settings |
| `PATCH` | `/api/settings/{key}` | Update a setting (`usage_poll_interval_seconds`, `tmux_nudge_*`) |
| `GET` | `/api/settings/shell-status` | Check shell integration and active pointer status |
| `POST` | `/api/settings/setup-shell` | Append shell snippet to `.zshrc` / `.bashrc` |
| `GET` | `/api/credential-targets` | List auto-discovered `.claude.json` targets with enabled state |
| `POST` | `/api/credential-targets/rescan` | Re-run target discovery |
| `PATCH` | `/api/credential-targets` | Toggle the `enabled` flag for one canonical target |
| `POST` | `/api/credential-targets/sync` | Re-mirror active account into all enabled targets |
| `GET` | `/health` | Health check (always public) |

**WebSocket:** `ws://localhost:41924/ws?since=<seq>` — streams `usage_updated`, `account_switched`, `account_deleted`, and `error` events. The `since` parameter requests buffered events the client may have missed; the server falls back to a full snapshot if the buffer gap is too large.

---

## Testing

```bash
uv run pytest tests/ -q                            # all 218 tests
uv run pytest tests/ -v --tb=short                 # verbose output
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

**Account switch not picked up in an existing terminal**
- Open a new terminal tab — the shell snippet runs at startup
- Or re-source your rc file: `source ~/.zshrc`

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

**Stable** — running 24/7 as a personal LaunchAgent on the maintainer's machine. The codebase has been through several rounds of audited refactoring (218 tests covering routers, services, background loop, schemas, and an end-to-end smoke suite). The public API and database schema are considered stable; breaking changes ship with an Alembic migration.

What is intentionally **not** done:
- No multi-tenant support — one user per machine
- No cloud sync — everything lives in local SQLite + `~/.ccswitch/`
- No Linux Keychain integration — file-based fallback only
- No GUI for credential targets beyond the dashboard checkbox list

---

## License

This project does not currently ship with a license file, which means the source is **All Rights Reserved** by default under copyright law. You may read the code and run it locally for personal use, but redistribution, derivative works, and commercial use are not granted. If you need an explicit license for your use case, open an issue.

---

## Acknowledgments

- Built on [FastAPI](https://fastapi.tiangolo.com/), [SQLAlchemy](https://www.sqlalchemy.org/), and [uv](https://docs.astral.sh/uv/).
- Inspired by the daily reality of hitting Claude Code's five-hour rate window mid-refactor.
- Architecture refined across multiple agent-assisted refactoring passes — see `CLAUDE.md` for the architecture tour aimed at future AI sessions.
