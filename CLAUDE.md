# CCSwitch — Architecture Tour

Short map for future Claude-assisted sessions.  See `README.md` for user-facing
setup instructions.

## What this app does

A local FastAPI dashboard that lets you keep several Claude.ai subscription
accounts, each in its own isolated `CLAUDE_CONFIG_DIR`.  It polls Anthropic's
`/v1/messages` endpoint with a near-empty probe just to read the unified
rate-limit headers, and when the active account approaches its 5-hour window
limit (or gets rate-limited) it auto-switches to the next eligible account
(credentials are copied into `~/.claude/` and the macOS Keychain so a fresh
`claude` run picks the change up immediately).  Optionally, after every
switch a tmux nudge scan sends a configurable message to any pane whose
recent output matches a rate-limit notice, so already-running Claude Code
sessions resume work with the freshly-mirrored credentials.

## Layout

```
backend/
  main.py              FastAPI app + lifespan + /ws endpoint + single poll loop
  background.py        poll_usage_and_switch() — the only polling routine
  cache.py             _UsageCache class + module-level cache singleton
  config.py            Pydantic settings (env prefix: CLAUDE_MULTI_)
  database.py          async SQLAlchemy engine + init_db (Alembic-backed)
  models.py            Account, SwitchLog, Setting (with indexes)
  schemas.py           Pydantic request/response models
  ws.py                Minimal WebSocketManager (broadcast + connection list)
  auth.py              Optional Bearer/WS token middleware
  routers/
    accounts.py            /api/accounts CRUD + login flow
    service.py             /api/service enable/disable + default-account
    settings.py            /api/settings get/patch + shell-setup helper
    credential_targets.py  /api/credential-targets list/rescan/toggle/sync
  services/
    credential_provider.py  CANONICAL: Keychain read/write, _load_json_safe,
                            active_dir_pointer_path, _credential_lock (RLock)
    account_service.py      activate_account_config (6-step), backup/restore,
                            path helpers — imports shared utils from credential_provider
    account_queries.py      DB query helpers (get_by_id, get_by_email, etc.)
    login_session_service.py  isolated add-account login sessions (+RLock)
    credential_targets.py   discover .claude.json targets + JSON settings row
    anthropic_api.py   probe_usage() + refresh_access_token()
    switcher.py        get_next_account() + perform_switch() + maybe_auto_switch
                       (+ _switch_lock asyncio)
    settings_service.py  typed get/set for Setting rows (bool/int/int_or_none/json)
    tmux_service.py    list_panes, send_keys, capture_pane, looks_stalled,
                        wake_stalled_sessions, fire_nudge (post-switch)
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
    ui/
      accounts.js              Account cards, drag-reorder, threshold slider
      service.js               Service toggle, default account
      log.js                   Switch log with pagination
      login.js                 Add-account modal (multi-step login flow)
      credential_targets.js    Settings-page Credential Targets panel
      tmux_nudge.js            Settings-page Wake Tmux Sessions block
alembic/
  versions/            Schema migrations (initial + drop_display_name
                       + drop_tmux_monitors)
tests/
  conftest.py          tmp-dir isolation + make_test_app factory fixture
  test_*.py            router + service + background + schemas + e2e
```

## Key data flow

1. `main.lifespan` runs `init_db()`, syncs `~/.claude-multi/active`, then
   starts exactly **one** background task: `_poll_loop(idle_interval)`.
2. `_poll_loop` calls `bg.poll_usage_and_switch(ws_manager)` immediately (to
   warm caches), then alternates between a tight active cadence
   (`cfg.poll_interval_active`, default 15 s, while any WS client is
   connected) and an idle cadence (DB-configurable, floored at
   `cfg.poll_interval_min`).
3. `poll_usage_and_switch` gates on `service_enabled` before doing any work.
   For each account it reads the access token, skips token refresh for
   already-stale accounts, refreshes otherwise if expiry is within 5 minutes,
   probes `/v1/messages` for rate-limit headers, stores the result in
   `cache` (a `_UsageCache` singleton in `cache.py`), and caches `token_info`
   so `GET /api/accounts` does not fan out Keychain subprocess calls per row.
4. If the active account crosses its `threshold_pct`, it picks the next
   enabled **non-stale** account by priority, calls `switcher.perform_switch`, which
   copies credentials into `~/.claude/`, rewrites both Keychain entries (the
   hashed per-config-dir one and the legacy no-hash one), and writes
   `~/.claude-multi/active`.
5. Every outcome is broadcast over `/ws` so the SPA updates live.

## Account switching = four artefacts

`account_service.activate_account_config()` keeps these in sync so any fresh
`claude` invocation picks up the current account even without
`CLAUDE_CONFIG_DIR` in the environment:

1. Credential files (`.credentials.json`, `credentials.json`, `.claude.json`)
   copied into `~/.claude/`.
2. Keychain entry keyed by `sha256(~/.claude)[:8]` (hashed per-dir service).
3. Legacy `Claude Code-credentials` Keychain entry (no hash).
4. `~/.claude-multi/active` — a plain file the shell integration reads to
   export `CLAUDE_CONFIG_DIR` for new terminals.

## Constraints

- **macOS only** for the credential-switching path (uses the `security` CLI).
  On Linux it silently falls back to the file-based credentials, which may
  or may not work depending on the Claude Code build.
- **Local only**.  The `/ws` endpoint has no authentication — the app is
  intended to run on `127.0.0.1:41924` behind your browser.  Host and port
  are configurable via `CLAUDE_MULTI_SERVER_HOST` / `CLAUDE_MULTI_SERVER_PORT`.
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
