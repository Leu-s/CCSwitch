# CCSwitch — Architecture Tour

Short map for future Claude-assisted sessions.  See `README.md` for user-facing
setup instructions.

## What this app does

A local FastAPI dashboard that lets you keep several Claude.ai subscription
accounts, each in its own isolated `CLAUDE_CONFIG_DIR`.  It polls Anthropic's
`/v1/messages` endpoint with a near-empty probe just to read the unified
rate-limit headers, and when the active account approaches its 5-hour window
limit (or gets rate-limited) it auto-switches to the next eligible account.
On every switch the dashboard mirrors the active account's identity into any
user-opted-in `.claude.json` files ("credential targets") and — when a
system-default target is enabled — also rewrites the legacy
`Claude Code-credentials` Keychain entry and `~/.claude/.credentials.json`
so a fresh `claude` run picks up the change without `CLAUDE_CONFIG_DIR` set.
Optionally, after every switch a tmux nudge scan sends a configurable
message to any **claude** pane whose recent output matches a rate-limit
notice, so already-running Claude Code sessions resume work with the
freshly-mirrored credentials.

## Layout

```
backend/
  main.py              FastAPI app + lifespan + /ws endpoint + two background
                       tasks (_poll_loop + _cleanup_sessions_loop) + static serving
  background.py        poll_usage_and_switch() — per-account probe, token refresh,
                       per-account rate-limit backoff (exponential, 120 s → 3600 s cap)
  cache.py             _UsageCache class + module-level cache singleton
  config.py            Pydantic settings (env prefix: CCSWITCH_)
  database.py          async SQLAlchemy engine + init_db (Alembic-backed)
  models.py            Account, SwitchLog, Setting (with indexes)
  schemas.py           Pydantic request/response models (17 schemas for accounts,
                       login flow, service, settings, credential targets, switch log)
  ws.py                WebSocketManager — broadcast, replay_since (bounded deque),
                       connection lifecycle, seq stamping
  auth.py              TokenAuthMiddleware — optional Bearer (HTTP) / query-param (WS)
                       auth; exempt paths: /, /src/*, /health
  routers/
    accounts.py            /api/accounts CRUD + login flow (add + relogin)
    service.py             /api/service enable/disable + default-account
    settings.py            /api/settings get/patch + shell-setup helper
    credential_targets.py  /api/credential-targets list/rescan/toggle/sync
  services/
    credential_provider.py  Keychain read/write (get/save token, wipe credentials),
                            _load_json_safe, active_dir_pointer_path,
                            get_token_info, LEGACY_KEYCHAIN_SERVICE constant,
                            _credential_lock (RLock) — shared by account_service
    account_service.py      activate_account_config (credentials→mirror→pointer),
                            sync_active_to_targets, build_ws_snapshot,
                            backup/restore, path helpers
    account_queries.py      DB query helpers (get_by_id, get_by_email, etc.)
    login_session_service.py  add-account + relogin sessions (+RLock for
                              _active_login_sessions dict, 10 s subprocess
                              timeouts, 30-min auto-expiry)
    credential_targets.py   discover .claude.json targets, JSON settings row,
                            mirror_oauth_into_targets — validated opt-in list
    anthropic_api.py   probe_usage() + refresh_access_token()
    switcher.py        get_next_account() + perform_switch() + maybe_auto_switch
                       + switch_if_active_disabled + perform_sync_to_targets
                       (+ _switch_lock asyncio); calls fire_nudge() after switch
    settings_service.py  typed get/set for Setting rows (bool/int/int_or_none/json)
    tmux_service.py    list_panes, send_keys, capture_pane, looks_stalled,
                        wake_stalled_sessions, fire_nudge — nudges only panes
                        whose pane_current_command looks like `claude`
frontend/
  index.html           HTML shell (Accounts page + Settings page + Add-account modal)
  src/
    main.js            App entry point — theme, shell status, page toggle, init
    api.js             Fetch wrapper (30s timeout, error extraction)
    ws.js              WebSocket with exponential reconnect + sequence replay
    state.js           Shared mutable state object
    constants.js       Timing constants
    utils.js           DOM helpers (qs, qsa, escapeHtml, fmtTime, etc.)
    toast.js           Toast notification system
    style.css          Full stylesheet (dark default, light via data-theme)
    favicon.svg        Browser tab icon (served via /src/ static mount)
    ui/
      accounts.js              Account cards, drag-reorder, threshold slider,
                               default-account selector (sets service.default_account_id)
      service.js               Master-switch button (loads /api/service, toggles enable/disable)
      log.js                   Switch log with pagination
      login.js                 Add-account modal (multi-step login flow)
      credential_targets.js    Settings-page Credential Targets panel
      tmux_nudge.js            Settings-page Wake Tmux Sessions block
alembic/
  versions/            Schema migrations (Alembic-backed, auto-run on startup)
tests/
  conftest.py          tmp-dir isolation + make_test_app factory fixture
  test_*.py            router + service + background + schemas + e2e
```

## Key data flow

1. `main.lifespan` runs `init_db()`, seeds default settings via
   `ensure_defaults`, syncs `~/.ccswitch/active`, then starts **two**
   background tasks: `_poll_loop(idle_interval)` and
   `_cleanup_sessions_loop()` (the latter reaps expired add-account login
   sessions every 5 min via `ls._cleanup_expired_sessions`).
2. `_poll_loop` calls `bg.poll_usage_and_switch(ws_manager)` immediately (to
   warm caches), then alternates between a tight active cadence
   (`cfg.poll_interval_active`, default 15 s, while any WS client is
   connected) and an idle cadence (DB-configurable, floored at
   `cfg.poll_interval_min`).
3. `poll_usage_and_switch` runs **unconditionally** — usage polling is
   independent of the `service_enabled` master toggle so the dashboard's
   rate-limit bars stay live even when auto-switching is off.  For each
   account it reads the access token, skips token refresh for already-stale
   accounts, refreshes otherwise if expiry is within 5 minutes, probes
   `/v1/messages` for rate-limit headers, stores the result in `cache`
   (a `_UsageCache` singleton in `cache.py`), and caches `token_info` so
   `GET /api/accounts` does not fan out Keychain subprocess calls per row.
   Accounts that return 429 enter per-account exponential backoff
   (120 s initial, doubling up to 3600 s cap; cleared on next success).
4. After polling, it delegates to `switcher.maybe_auto_switch`, which
   **does** gate on `service_enabled`: if the master toggle is off it
   returns immediately.  Otherwise, if the active account crosses its
   `threshold_pct` (or came back 429, or has a `stale_reason`), it picks
   the next enabled **non-stale** account by priority, calls
   `switcher.perform_switch`, and then calls `tmux_service.fire_nudge()`
   to kick any stalled claude panes.
5. Every outcome is broadcast over `/ws` so the SPA updates live.

## Account switching = four artefacts (ordered!)

`account_service.activate_account_config()` writes four pieces of state.
The **step order matters** — the failure-prone step runs FIRST so a mid-switch
exception cannot leave the system in a split-brain state:

1. **Atomic `.credentials.json` copy** into `~/.claude/` (tmp-in-same-dir +
   `os.replace`). Runs only when a system-default credential target is
   enabled. Most likely step to fail (disk full, permission denied), so it
   runs before anything else is touched.
2. **Legacy `Claude Code-credentials` Keychain entry** rewritten, and any
   stale `claude-code`/`claude-code-user`/`root` leftover entries deleted.
   Runs only when a system-default credential target is enabled.
3. **Mirror `oauthAccount` + `userID`** from the new account's `.claude.json`
   into every user-opted-in credential target file (atomic write, 0o600).
   Other keys in each target file (projects, MCP state, etc.) are preserved.
4. **`~/.ccswitch/active` pointer** — the last write. The shell
   integration reads this to export `CLAUDE_CONFIG_DIR` for new terminals.
   Writes are atomic and now re-raise on failure instead of silently
   swallowing — callers treat the switch as failed if this step raises.

The per-config-dir hashed Keychain entry (`sha256(config_dir)[:8]`) is
written once at account creation, not per switch, and is read by
`credential_provider` on every token refresh.

## Constraints

- **macOS only** for the credential-switching path (uses the `security` CLI).
  On Linux it silently falls back to the file-based credentials, which may
  or may not work depending on the Claude Code build.
- **Local only**.  The `/ws` endpoint has no authentication — the app is
  intended to run on `127.0.0.1:41924` behind your browser.  Host and port
  are configurable via `CCSWITCH_SERVER_HOST` / `CCSWITCH_SERVER_PORT`.
- **tmux required** for the Add-Account login flow.  The tmux nudge
  (`wake_stalled_sessions`) is an opt-in feature on the Settings page — off by default.
- **Python 3.12+** (the repo's `.venv` runs on 3.14).

## Tests

```bash
uv run pytest tests/ -q
```

`tests/conftest.py` chdirs to a pytest-managed tmp directory for the
session, so hard-coded relative DB URLs (`sqlite+aiosqlite:///./test_*.db`)
end up inside the tmp dir instead of polluting the repo root.

## Concurrency model

- `_credential_lock` (threading.RLock, in credential_provider.py): serializes ALL
  mutations to Keychain entries, credential files, and the active-dir pointer.
  Both `activate_account_config` and `save_refreshed_token` acquire it.
- `_switch_lock` (asyncio.Lock, in switcher.py): serializes concurrent
  `perform_switch` calls so two auto-switches can't overlap.
- `_sessions_lock` (threading.RLock, in login_session_service.py): protects the
  `_active_login_sessions` dict.  Reentrant so `_cleanup_expired_sessions` can
  iterate the dict and then call `cleanup_login_session` (which also acquires it).
- `_UsageCache` (cache.py): asyncio.Lock protects in-memory usage + token_info
  dicts.  All reads from outside the cache use `_async` variants.

## Things that are intentionally NOT done

- Alembic is set up for production migrations. `init_db()` runs `alembic upgrade head`
  on startup, so schema changes require a new migration file (`alembic revision --autogenerate`).
- No global exception handler; background task failures are caught and
  logged inside `poll_usage_and_switch` itself.
- No `/ws` auth, no CORS config — if you ever expose the port remotely you
  must add both.
- No multi-tenant support (one user per machine).
