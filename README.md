# Claude Code Multi-Account Manager

**Seamless auto-switching between Claude.ai accounts when you hit rate limits.**

![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)
![Tests](https://img.shields.io/badge/tests-147%20passing-brightgreen)
![Platform](https://img.shields.io/badge/platform-macOS-lightgrey)
![No build step](https://img.shields.io/badge/frontend-no%20build%20step-orange)

A local dashboard that monitors usage across multiple Claude.ai subscription accounts and automatically switches credentials before you hit the rate-limit ceiling. Each account lives in its own isolated `CLAUDE_CONFIG_DIR`; the switch is transparent — `claude` picks up the new account without any manual intervention.

<!-- To add a screenshot: place it in docs/screenshot.png and uncomment below -->
<!-- ![Dashboard](docs/screenshot.png) -->

> **macOS only** for the full credential-switching path (Keychain via `security` CLI). Linux falls back to file-only credentials.
> **tmux required** for the login flow and monitor features.

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

---

## Features

- **Live usage monitoring** — polls `/v1/messages` rate-limit headers every 15 s while the dashboard is open, every 5 min otherwise
- **Auto-switch** — when the active account's five-hour utilization reaches a configurable threshold (default 95 %), the next enabled account is activated automatically
- **Keychain-safe credential switching** — copies credential files into `~/.claude/`, rewrites both the hashed per-config-dir and legacy Keychain entries, and updates `~/.claude-multi/active`
- **Shell integration** — a one-liner in `.zshrc`/`.bashrc` exports `CLAUDE_CONFIG_DIR` for every new terminal; no restart needed
- **Real-time dashboard** — vanilla-JS single-page app; account cards, drag-to-reorder priority, per-account threshold slider, switch log; no build step required
- **tmux monitors** — watch specific panes, auto-continue paused `claude` sessions after a switch
- **CLI** (`cc-acc`) — list/switch/enable/disable accounts, tail logs, manage the LaunchAgent, set up shell integration
- **macOS LaunchAgent** — optional auto-start on login

---

## Requirements

| Dependency | Notes |
|---|---|
| macOS | Full Keychain support. Linux works with file-only credentials. |
| Python 3.12+ | Tested on 3.14 |
| [uv](https://docs.astral.sh/uv/) | Recommended. Plain `pip` also works. |
| tmux | Required for the "Add Account" login flow and monitor features |

---

## Quick Start

```bash
# 1. Clone
git clone <repo-url>
cd claude-code-multi-account

# 2. Install dependencies
uv sync

# 3. Start the dev server
uv run uvicorn backend.main:app --host 127.0.0.1 --port 8765 --reload

# 4. Open the dashboard
open http://localhost:8765
```

The SQLite database and schema are created automatically on first start — no manual setup required.

---

## Installation

```bash
git clone <repo-url>
cd claude-code-multi-account
uv sync

# Optional: copy environment defaults (all have sensible defaults)
cp .env.example .env
```

### Auto-start on login (macOS LaunchAgent)

```bash
# Add scripts/ to your PATH first, or use the full path
export PATH="$PATH:$(pwd)/scripts"

cc-acc service install               # creates ~/Library/LaunchAgents/com.claudemulti.manager.plist
cc-acc service remove                # uninstall
cc-acc service remove --purge-logs   # uninstall + delete logs
```

The LaunchAgent:
- starts the server **immediately** on install (`RunAtLoad: true`)
- **auto-restarts** on crash with a 30 s throttle (`KeepAlive: {SuccessfulExit: false}`)
- **starts automatically** on every login
- writes logs to `~/.local/state/claude-multi/server.log`

```bash
cc-acc log -f                        # tail logs
cc-acc status                        # check if the service is running
launchctl print gui/$(id -u)/com.claudemulti.manager  # raw launchd status
```

---

## Configuration

All settings use the `CLAUDE_MULTI_` environment variable prefix. Copy `.env.example` and adjust only what you need — all defaults work for a standard local setup.

| Variable | Default | Description |
|---|---|---|
| `CLAUDE_MULTI_SERVER_PORT` | `8765` | HTTP server port |
| `CLAUDE_MULTI_DATABASE_URL` | `sqlite+aiosqlite:///./claude_multi_account.db` | SQLite connection string (relative to working dir) |
| `CLAUDE_MULTI_ACTIVE_CLAUDE_DIR` | `~/.claude` | System-wide Claude Code config dir |
| `CLAUDE_MULTI_ACCOUNTS_BASE_DIR` | `~/.claude-multi-accounts` | Base dir for isolated per-account config dirs |
| `CLAUDE_MULTI_STATE_DIR` | `~/.claude-multi` | Holds the `active` pointer file |
| `CLAUDE_MULTI_POLL_INTERVAL_ACTIVE` | `15` | Poll interval (seconds) while browser tab is open |
| `CLAUDE_MULTI_POLL_INTERVAL_IDLE` | `300` | Poll interval (seconds) with no active WebSocket clients |
| `CLAUDE_MULTI_POLL_INTERVAL_MIN` | `120` | Minimum floor for the DB-overridable idle interval |
| `CLAUDE_MULTI_DEFAULT_ACCOUNT_THRESHOLD_PCT` | `95.0` | Auto-switch threshold for newly added accounts (0–100) |
| `CLAUDE_MULTI_HAIKU_MODEL` | `claude-haiku-4-5-20251001` | Model used for tmux monitor evaluation |
| `CLAUDE_MULTI_API_TOKEN` | *(empty)* | Optional Bearer token. **Empty = no auth** (safe for localhost). |

No variable is mandatory — all have sensible defaults. Set `CLAUDE_MULTI_API_TOKEN` only if you expose the server beyond localhost.

---

## Running

### Development (hot reload)

```bash
uv run uvicorn backend.main:app --host 127.0.0.1 --port 8765 --reload
```

### Production (via launch script)

```bash
bash scripts/launch.sh
```

`launch.sh` checks prerequisites (tmux, `security`), manages a PID file at `~/.local/state/claude-multi/server.pid`, and writes logs to `~/.local/state/claude-multi/server.log`. It starts uvicorn without `--reload`.

### Status and logs

```bash
bash scripts/status.sh    # server health + LaunchAgent status
cc-acc status             # server, shell config, active account
cc-acc log -f             # tail logs (follow mode)
cc-acc log -n 100         # last 100 lines
```

---

## Shell Integration

To make new terminals automatically use the currently-active account, add this one-liner to `~/.zshrc` or `~/.bashrc`:

```bash
_d=$(cat ~/.claude-multi/active 2>/dev/null); [ -n "$_d" ] && export CLAUDE_CONFIG_DIR="$_d"; unset _d
```

At shell startup it reads `~/.claude-multi/active` (a pointer file updated on every account switch) and exports `CLAUDE_CONFIG_DIR` to the active account's isolated directory. Claude Code reads that variable and uses the right credentials.

**Automated setup:**

```bash
cc-acc shell setup    # appends the block above to .zshrc and/or .bashrc
```

After a switch, existing terminals can re-source their rc file (`source ~/.zshrc`) or simply open a new tab.

---

## Architecture

```
┌──────────────────┐   WebSocket /ws      ┌──────────────────────┐
│  Browser         │◄────────────────────►│  FastAPI  :8765      │
│  (Vanilla JS     │   HTTP /api/*        │                      │
│   ES6 modules)   │──────────────────►   │  background poll     │
└──────────────────┘                      └──────────┬───────────┘
                                                     │
                         ┌───────────────────────────┼───────────────────┐
                         ▼                           ▼                   ▼
                ┌─────────────────┐      ┌─────────────────┐  ┌──────────────────┐
                │ SQLite DB       │      │ Anthropic API   │  │ ~/.claude/       │
                │ (accounts,      │      │ /v1/messages    │  │ ~/.claude-multi/ │
                │  settings,      │      │ (headers only)  │  │ macOS Keychain   │
                │  switch_log,    │      └─────────────────┘  └──────────────────┘
                │  tmux_monitors) │
                └─────────────────┘
```

### Data flow

1. **Startup** — `init_db()` runs Alembic migrations (creates the DB on first run), syncs `~/.claude-multi/active`, then spawns two background tasks: the poll loop and a login-session cleanup loop.

2. **Poll cycle** — Every 15 s with active clients, every 5 min when idle. Per account: reads the access token from the isolated config dir, refreshes it if expiring within 5 min, POSTs a near-empty request to `/v1/messages` purely to read the `anthropic-ratelimit-unified-*` response headers (five-hour and seven-day utilization + reset times). Results are cached in memory and broadcast over WebSocket.

3. **Auto-switch** — If the active account's five-hour utilization ≥ `threshold_pct`, `perform_switch()` atomically:
   - Copies credential files into `~/.claude/`
   - Rewrites the hashed per-config-dir Keychain entry and the legacy `Claude Code-credentials` entry
   - Writes `~/.claude-multi/active`
   - Logs the event in `switch_log`
   - Broadcasts `account_switched` over WebSocket

4. **Shell pickup** — New terminals sourcing the rc snippet read `~/.claude-multi/active` and export `CLAUDE_CONFIG_DIR`; existing `claude` processes are unaffected until restarted.

---

## Project Structure

```
├── backend/
│   ├── main.py                        # FastAPI app, lifespan, /ws, static serving
│   ├── config.py                      # Pydantic settings (CLAUDE_MULTI_ prefix)
│   ├── models.py                      # ORM: Account, TmuxMonitor, SwitchLog, Setting
│   ├── database.py                    # Async SQLAlchemy engine + Alembic init_db()
│   ├── schemas.py                     # Pydantic request/response models
│   ├── cache.py                       # Thread-safe in-memory usage + token_info cache
│   ├── auth.py                        # Optional Bearer token middleware
│   ├── ws.py                          # WebSocketManager (broadcast + replay buffer)
│   ├── background.py                  # Per-account poll, token refresh, auto-switch
│   ├── routers/
│   │   ├── accounts.py                # /api/accounts CRUD + login flow
│   │   ├── service.py                 # /api/service enable/disable/default-account
│   │   ├── settings.py                # /api/settings + shell snippet
│   │   └── tmux.py                    # /api/tmux sessions, monitors, capture, send-keys
│   └── services/
│       ├── account_service.py         # Account lifecycle, activation, backup/restore
│       ├── account_queries.py         # DB query helpers
│       ├── login_session_service.py   # tmux-based login session start/verify/cleanup
│       ├── credential_provider.py     # Keychain read/write + credential file I/O
│       ├── anthropic_api.py           # probe_usage() + refresh_access_token()
│       ├── switcher.py                # get_next_account() + perform_switch()
│       ├── settings_service.py        # Typed get/set for Setting DB rows
│       └── tmux_service.py            # tmux pane ops + Claude Haiku evaluation
├── frontend/
│   ├── index.html                     # HTML shell (259 lines)
│   └── src/
│       ├── main.js                    # Entry point: tabs, theme, keyboard shortcuts
│       ├── api.js                     # fetch wrapper (30 s timeout, error extraction)
│       ├── ws.js                      # WebSocket + exponential backoff + replay
│       ├── state.js                   # Shared mutable state
│       ├── constants.js               # Timing constants
│       ├── utils.js                   # DOM helpers, date/time formatters
│       ├── toast.js                   # Toast notification system
│       ├── style.css                  # Dark/light theme, all components
│       └── ui/
│           ├── accounts.js            # Account cards, drag-reorder, threshold slider
│           ├── service.js             # Service toggle, default account, auto-switch
│           ├── log.js                 # Switch log + pagination
│           ├── login.js               # Add-account modal (multi-step tmux login)
│           ├── tmux.js                # tmux terminal pane, monitors, event feed
│           └── events.js              # Custom DOM event bus
├── alembic/                           # Database migrations
│   └── versions/                      # initial schema + drop_display_name
├── scripts/
│   ├── cc-acc / cc-acc.py             # CLI tool
│   ├── launch.sh                      # Production server launcher
│   ├── status.sh                      # Server health check
│   ├── create_system_service.sh       # macOS LaunchAgent installer
│   └── remove_system_service.sh       # LaunchAgent removal
└── tests/                             # 16 test files, 147 tests
```

---

## CLI Reference

Add `scripts/` to your `PATH`, or run `python scripts/cc-acc.py <command>` directly.

```bash
cc-acc list                            # List all accounts with active marker and usage
cc-acc switch <email>                  # Switch active account immediately
cc-acc enable <email>                  # Include account in auto-switch rotation
cc-acc disable <email>                 # Exclude account from auto-switch rotation
cc-acc status                          # Server health, shell config, active account
cc-acc shell setup                     # Append CLAUDE_CONFIG_DIR one-liner to rc files
cc-acc server start                    # Launch server in a new tmux window
cc-acc server stop                     # Stop server (and unload LaunchAgent if running)
cc-acc log [-f] [-n N]                # View server logs (-f: follow, -n: line count)
cc-acc service install                 # Install macOS LaunchAgent (auto-start on login)
cc-acc service remove [--purge-logs]   # Uninstall LaunchAgent (optionally delete logs)
```

The CLI connects to `http://localhost:8765` by default. Override with the `CLAUDE_MULTI_URL` env var.

---

## API

All `/api/*` routes require `Authorization: Bearer <token>` when `CLAUDE_MULTI_API_TOKEN` is set. The WebSocket passes the token via `?token=<value>` (browser WebSocket API does not support custom headers). The paths `/`, `/src/*`, and `/health` are always public.

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/accounts` | List accounts with live usage data |
| `POST` | `/api/accounts/start-login` | Open tmux pane for new account login |
| `POST` | `/api/accounts/verify-login` | Verify login completed, save account |
| `DELETE` | `/api/accounts/cancel-login` | Cancel and clean up a login session |
| `PATCH` | `/api/accounts/{id}` | Update account (`enabled`, `threshold_pct`, `priority`) |
| `DELETE` | `/api/accounts/{id}` | Delete account |
| `POST` | `/api/accounts/{id}/switch` | Manual switch to account |
| `GET` | `/api/accounts/log` | Paginated switch log |
| `GET` | `/api/accounts/log/count` | Total switch log entry count |
| `GET` | `/api/service` | Service status (`enabled`, `active_email`, `default_account_id`) |
| `POST` | `/api/service/enable` | Enable auto-switching, activate default or first account |
| `POST` | `/api/service/disable` | Disable and restore original credentials |
| `PATCH` | `/api/service/default-account` | Set starting account for enable |
| `GET` | `/api/settings` | List settings |
| `PATCH` | `/api/settings/{key}` | Update a setting (`auto_switch_enabled`, `usage_poll_interval_seconds`) |
| `GET` | `/api/settings/shell-status` | Check shell integration and active pointer status |
| `POST` | `/api/settings/setup-shell` | Append shell snippet to `.zshrc` / `.bashrc` |
| `GET` | `/api/tmux/sessions` | Discover tmux panes |
| `GET` | `/api/tmux/capture` | Capture output from a pane |
| `POST` | `/api/tmux/send` | Send keys to a pane |
| `GET` | `/api/tmux/monitors` | List monitors |
| `POST` | `/api/tmux/monitors` | Create monitor |
| `PATCH` | `/api/tmux/monitors/{id}` | Update monitor |
| `DELETE` | `/api/tmux/monitors/{id}` | Delete monitor |
| `GET` | `/health` | Health check (always public) |

**WebSocket:** `ws://localhost:8765/ws?since=<seq>` — streams `usage_updated`, `account_switched`, `account_deleted`, `tmux_result`, and `error` events. The `since` parameter requests buffered events the client may have missed; the server falls back to a full snapshot if the buffer gap is too large.

---

## Testing

```bash
uv run pytest tests/ -q                            # all 147 tests
uv run pytest tests/ -v --tb=short                 # verbose output
uv run pytest tests/test_accounts_router.py        # single file
```

Tests create isolated SQLite databases in a pytest-managed temp directory — no environment setup, no external services, no API keys required. The `asyncio_mode = "auto"` setting in `pyproject.toml` means async test functions are detected automatically without explicit decorators.

---

## Security

**This application is designed for localhost use only.**

- The SQLite database is unencrypted and stores account metadata (emails, config paths)
- By default, `CLAUDE_MULTI_API_TOKEN` is empty — **all local requests are accepted without authentication**, which is appropriate when the server binds to `127.0.0.1`
- There is no CORS configuration — the frontend and API share the same origin
- If you set an API token, WebSocket connections pass it as `?token=...` in the URL, which appears in server access logs

**If you need to expose the server beyond localhost:**

1. Set `CLAUDE_MULTI_API_TOKEN` to a strong random value: `openssl rand -hex 32`
2. Run behind a TLS-terminating reverse proxy (nginx, Caddy) for HTTPS/WSS
3. Restrict network access via firewall rules

---

## Troubleshooting

**Server won't start**
- Ensure tmux is installed: `brew install tmux`
- Ensure `security` is available (macOS only): `which security`
- Check for a stale PID file: `rm ~/.local/state/claude-multi/server.pid`

**Account switch not picked up in an existing terminal**
- Open a new terminal tab — the shell snippet runs at startup
- Or re-source your rc file: `source ~/.zshrc`

**WebSocket indicator stays disconnected**
- Verify the server is running: `curl http://localhost:8765/health`
- Check logs: `cc-acc log -n 50`

**`cc-acc` reports "cannot connect"**
- Start the server first: `cc-acc server start` or `bash scripts/launch.sh`
- If using a non-default port: `export CLAUDE_MULTI_URL=http://localhost:PORT`

**Usage card shows "Rate limited"**
- Expected: the app backs off automatically and retries. No action needed.

**LaunchAgent doesn't start after reboot**
- Re-install: `cc-acc service remove && cc-acc service install`
- Check system log: `log show --predicate 'subsystem == "com.apple.launchd"' --last 5m | grep claudemulti`

**Database schema error after upgrade**
- Alembic runs `upgrade head` automatically on every server start — no manual migration step needed.
