# Claude Code Multi-Account Manager — Design Spec

**Date**: 2026-04-13  
**Status**: Approved

---

## Overview

A FastAPI web application that manages multiple Claude.ai subscription accounts for Claude Code, enabling automatic credential swapping when usage limits are reached. Uses the same mechanism as the active Claude Code session (macOS Keychain + `~/.claude/.claude.json`) to swap accounts without restarting the binary — only a tmux "nudge" is needed.

---

## Architecture

### Stack

- **Backend**: Python 3.12+, FastAPI, SQLAlchemy (async), aiomysql
- **Database**: MySQL 8
- **Frontend**: Single HTML page, Vanilla JS, dark theme dashboard
- **Credential layer**: macOS `security` CLI (Keychain read/write)
- **Real-time**: WebSocket (FastAPI native)

### Directory Layout

```
claude-code-multi-account/
  backend/
    main.py               # FastAPI app, mounts routers, WebSocket hub
    database.py           # Async SQLAlchemy engine + session factory
    models.py             # SQLAlchemy ORM models
    schemas.py            # Pydantic request/response schemas
    routers/
      accounts.py         # Account CRUD, manual switch, scan Keychain
      tmux.py             # Monitor CRUD, live capture endpoint
      settings.py         # Global settings (threshold, auto-switch toggle)
    services/
      keychain.py         # security CLI wrapper: read/write/list credentials
      anthropic_api.py    # GET oauth/usage, token refresh
      switcher.py         # Core switch logic: next enabled account, ordering
      tmux_service.py     # send-keys, capture-pane, haiku evaluation
    background.py         # APScheduler: usage polling every 60s, auto-switch check
  frontend/
    index.html            # SPA — two tabs: Accounts, tmux Monitor
  docker-compose.yml      # MySQL service
  requirements.txt
  .env.example
```

---

## Database Schema

### `accounts`
| column | type | notes |
|---|---|---|
| id | INT PK AUTO | |
| email | VARCHAR(255) UNIQUE | |
| account_uuid | VARCHAR(36) | from oauthAccount |
| org_uuid | VARCHAR(36) | |
| keychain_suffix | VARCHAR(16) | e.g. `3798613e` — identifies the Keychain entry |
| display_name | VARCHAR(100) | optional label |
| enabled | BOOL | whether eligible for auto-switch |
| priority | INT | switch order (lower = tried first) |
| created_at | DATETIME | |

### `tmux_monitors`
| column | type | notes |
|---|---|---|
| id | INT PK AUTO | |
| name | VARCHAR(100) | human label |
| pattern_type | ENUM('manual','regex') | |
| pattern | VARCHAR(255) | session name or regex |
| enabled | BOOL | |

### `switch_log`
| column | type | notes |
|---|---|---|
| id | INT PK AUTO | |
| from_account_id | INT FK nullable | null if no previous |
| to_account_id | INT FK | |
| reason | ENUM('manual','rate_limit','threshold','api_error') | |
| triggered_at | DATETIME | |

### `settings`
| column | type | notes |
|---|---|---|
| key | VARCHAR(64) PK | |
| value | TEXT | JSON-encoded |

Default settings:
- `auto_switch_enabled`: `true`
- `switch_threshold_percent`: `90` (switch when 5h usage ≥ this)
- `usage_poll_interval_seconds`: `60`

Auto-switch is triggered solely by the background polling task — no external hooks required.

---

## Core Mechanisms

### Reading / Listing Accounts (Keychain scan)

```bash
security find-generic-password -s "Claude Code-credentials-{suffix}" -w
```

Returns JSON: `{"claudeAiOauth": {"accessToken": "...", "refreshToken": "...", "expiresAt": ...}}`

To discover all managed accounts, scan Keychain for entries matching `Claude Code-credentials-*` (via `security dump-keychain` filtered by service prefix). Any entry not yet in the DB is shown as "importable" in the UI.

### Active Account Detection

Read `~/.claude/.claude.json` → `oauthAccount.emailAddress` + `oauthAccount.accountUuid` to identify which account is currently active.

### Switching Accounts

1. Read target account credentials from `Claude Code-credentials-{suffix}` Keychain entry
2. Overwrite `Claude Code-credentials` Keychain entry with target credentials:
   ```bash
   security add-generic-password -U -s "Claude Code-credentials" -a "$USER" -w '{...}'
   ```
3. Surgically update `oauthAccount` field in `~/.claude/.claude.json` (preserve all other fields)
4. Log to `switch_log`
5. Notify all tmux monitors (see below)
6. Broadcast updated state to WebSocket clients

### Usage Data

Poll `https://api.anthropic.com/api/oauth/usage` with each account's `accessToken`:

```
GET https://api.anthropic.com/api/oauth/usage
Authorization: Bearer {accessToken}
```

Returns `five_hour.used_percentage`, `five_hour.resets_at`, `seven_day.used_percentage`, `seven_day.resets_at`. Cache result in memory with 60s TTL. If token is expired, attempt refresh via `https://platform.claude.com/v1/oauth/token` using `refreshToken` before retrying.

### Auto-Switch Logic (background task, runs every 60s)

1. If `auto_switch_enabled` is false → skip
2. Get current active account's usage
3. If `five_hour.used_percentage >= switch_threshold_percent` → call switcher
4. Switcher selects next `enabled=true` account ordered by `priority ASC`, skipping current
5. Performs switch, logs reason=`threshold`

---

## tmux Session Monitoring

### Session Discovery

```bash
tmux list-panes -a -F "#{session_name}:#{window_index}.#{pane_index} #{pane_current_command}"
```

UI shows all discovered sessions. User marks sessions as monitored (manually or via regex on session name).

### Post-Switch Notification

After every account switch, for each enabled tmux monitor:

1. Resolve matching panes (manual: exact match; regex: filter discovered panes)
2. `tmux send-keys -t {target} "continue" Enter`
3. Wait 2s
4. `tmux capture-pane -t {target} -p -S -20` (last 20 lines)
5. Send captured output to `claude -p --model claude-haiku-4-5-20251001` with prompt:
   > "Did the Claude Code session successfully continue after an account switch? Reply with one of: SUCCESS, FAILED, UNCERTAIN. Then one sentence of explanation."
6. Stream result to WebSocket → displayed in tmux Monitor tab as a status badge per session

---

## Frontend (Single HTML Page)

### Layout

Dark theme (`#0a0a0f` background, `#1a1a2e` cards, `#7c3aed` accent).

**Tab: Accounts**
- Header: active account badge, auto-switch toggle, threshold slider (%)
- Account cards (ordered by priority, draggable to reorder):
  - Email + display name
  - 5h usage bar (color: green→yellow→red by %)
  - 7d usage bar
  - Resets-at timestamp
  - Enabled toggle
  - "Switch to" button (disabled if already active)
  - Delete button
- "Scan for new accounts" button → calls `POST /api/accounts/scan`
- Import dialog for newly discovered Keychain entries
- Switch log table (last 20 entries, auto-updates via WebSocket)

**Tab: tmux Monitor**
- Session list with enable toggles and pattern input
- "Refresh sessions" button
- Per-session status panel: last switch result (SUCCESS/FAILED/UNCERTAIN), haiku explanation, raw terminal capture (collapsible)
- Live feed (WebSocket): events stream as switch happens

### Real-time Updates

WebSocket at `ws://localhost:8765/ws` broadcasts:
- `{type: "account_switched", from, to, reason}`
- `{type: "usage_updated", accounts: [...]}`
- `{type: "tmux_result", session, status, explanation}`

---

## Configuration

`.env` file:
```
DATABASE_URL=mysql+aiomysql://user:pass@localhost:3306/claude_multi_account
SERVER_PORT=8765
CLAUDE_CONFIG_DIR=~/.claude
HAIKU_MODEL=claude-haiku-4-5-20251001
```

---

## Error Handling

- Keychain read failure → mark account as `error` state in UI, skip during auto-switch
- Anthropic API unreachable → use last cached usage values, show stale indicator
- tmux session not found → log warning, skip that monitor, surface in UI
- Token expired + refresh failed → disable account, notify via WebSocket
- Switch with no eligible accounts → log error, do not switch, notify user

---

## Out of Scope

- Multi-user / authentication for the web UI itself
- Windows / Linux support (Keychain is macOS-only)
- Claude API key accounts (OAuth subscription only)
- Automatic `/login` OAuth flow from the UI
