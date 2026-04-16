# CCSwitch — Architecture Tour

Short map for future Claude-assisted sessions.  See `README.md` for
user-facing setup instructions and the design spec at
`docs/superpowers/specs/2026-04-15-vault-swap-architecture.md` for the
full architectural rationale.

## What this app does

A local FastAPI dashboard that manages several Claude.ai subscription
accounts under a single `~/.claude/` home directory.  It polls
Anthropic's `/v1/messages` endpoint with a near-empty probe to read the
unified rate-limit headers, and when the active account approaches its
5-hour window limit (or gets rate-limited) it atomically swaps
credentials between CCSwitch's private vault and Claude Code's standard
Keychain entry, then nudges every running `claude` tmux pane so they
wake up on the new account.

The user runs N tmux panes, **all on the same active account at any
given time**.  When a swap happens, every pane wakes up on the
freshly-swapped credentials.  CCSwitch automates what the user was
doing manually — `claude /login` in one pane, then "continue" in all
the others.

### Positioning vs. other OSS Claude-multi-account tooling

Two common branches exist elsewhere; CCSwitch deliberately sits in
neither.

- `CLAUDE_CONFIG_DIR` per-profile wrappers (`diranged/claude-profile`,
  `burakdede/aisw`, `realiti4/claude-swap`, the archived
  `ming86/cc-account-switcher`, etc.): per-account directories, each
  with its own Claude-Code-hashed Keychain entry.  Manual switching,
  no auto-failover on rate-limit, no central observability.
- Local-proxy routers (`ccflare`, `claude-balancer`, `ccNexus`,
  `cligate`, `ccproxy-api`): intercept `ANTHROPIC_BASE_URL` and swap
  credentials per request.  Powerful but post-February 2026 they
  operate in the Anthropic ToS gray zone ("route requests through
  Pro/Max credentials on behalf of users"); several removed
  round-robin / tier-balancing as a response.

CCSwitch's approach is a third path: **keep the native `claude`
binary, the native macOS Keychain, and the native OAuth flow; only
rotate which credentials sit in the standard `Claude Code-credentials`
Keychain entry at any given moment.**  No proxy, no token
interception, no API redirection.  ToS-safe by construction.  The
prior-art trade-offs are discussed in more depth in spec §10.

## Credential storage

Everything lives in exactly two Keychain namespaces.

| Keychain service          | `-a` | Written by | Read by |
|---|---|---|---|
| `Claude Code-credentials` | `$USER` | CCSwitch's swap orchestrator (on every switch); Claude Code CLI (on refresh) | Claude Code CLI (every API call); CCSwitch for probe-only reads |
| `ccswitch-vault`          | account email | CCSwitch: on login, during swap checkpoint, and on background refresh | CCSwitch only |

Invariant: each account's `refresh_token` lives in exactly one
Keychain entry at any instant.  A swap physically moves credentials
between the two services — the token leaves one entry before arriving
at the other.

The CLI cannot see vault entries — different service name, no
enumeration API — so CCSwitch is the sole owner of refresh lifecycles
for vault accounts.  No race possible by construction.  The active
entry is owned by the CLI; CCSwitch never refreshes it.

No per-account config directories.  No `~/.ccswitch-accounts/`.  No
`~/.ccswitch/active` pointer file.  No `CLAUDE_CONFIG_DIR` export
outside the short-lived login scratch directory.

## Layout

```
backend/
  main.py              FastAPI app + lifespan + /ws endpoint + two background
                       tasks (_poll_loop + _cleanup_sessions_loop) + static serving.
                       On startup: waits for Keychain unlock, runs integrity check.
  background.py        poll_usage_and_switch() — per-account probe + reactive vault refresh
                       (probe-401 only, 60s per-email cooldown); active-probe 401 triggers
                       a tmux nudge; post-sleep stagger (monotonic gap > 300 s → random
                       0-30 s delay before refreshes).
  cache.py             _UsageCache — usage and token_info dicts under an asyncio.Lock.
  config.py            Pydantic settings (env prefix: CCSWITCH_). No directory knobs —
                       ~/.claude/ is hardcoded.
  database.py          Async SQLAlchemy engine + init_db (Alembic-backed).
  models.py            Account, SwitchLog, Setting (Account has no config_dir).
  schemas.py           Pydantic request/response models (no waiting_for_cli, no
                       credential-target schemas, no shell-setup schemas).
  ws.py                WebSocketManager — broadcast, replay_since (bounded deque),
                       connection lifecycle, seq stamping.
  auth.py              TokenAuthMiddleware — optional Bearer (HTTP) / query-param (WS) auth.
  routers/
    accounts.py        /api/accounts CRUD + add-login flow + re-login flow.
                       No force-refresh endpoint.
    service.py         /api/service enable/disable (enable preserves existing active
                       account when valid) + default-account.
    settings.py        /api/settings get/patch for tmux nudge + poll interval.
                       No shell-status or setup-shell endpoints.
  services/
    credential_provider.py  Keychain vault/standard read-write helpers, vault refresh
                            persistence, login-scratch hashed-entry helpers, keychain
                            availability probe.  _credential_lock (RLock) shared by swap
                            + vault refresh.
    account_service.py      swap_to_account(email) — the 5-step atomic swap (step 0.5
                            refreshes the incoming vault token via
                            _refresh_incoming_on_promotion; outer acquires
                            refresh_lock(target) then cp._credential_lock — see
                            lock-order invariant).  Also save_new_vault_account,
                            delete_account_everywhere, get_active_email
                            (reads ~/.claude.json), startup_integrity_check,
                            build_ws_snapshot, revalidate_account (on-demand recovery
                            via _refresh_vault_token), and the shared per-email
                            refresh lock (_refresh_locks dict[str, threading.Lock]
                            + get_refresh_lock/with_refresh_lock_async/forget_refresh_lock
                            — serialises swap step 0.5, poll-loop reactive refresh,
                            and Revalidate against the single-use refresh_token).
    account_queries.py      DB query helpers (get_by_id, get_by_email, email→id map,
                            save_verified_account).
    login_session_service.py  Scratch-dir login lifecycle (add + relogin).  Both kinds
                              use $TMPDIR/ccswitch-login/session-<id>/ and extract
                              credentials into the vault on verify.
    anthropic_api.py   probe_usage() + refresh_access_token().
    switcher.py        get_next_account() + perform_switch() + maybe_auto_switch.
                       perform_switch delegates the credential move to
                       ac.swap_to_account(email) under _switch_lock, then writes
                       SwitchLog + broadcasts + fires tmux nudge.
    settings_service.py  Typed get/set for Setting rows (bool/int/int_or_none/json).
    tmux_service.py    list_panes, send_keys, capture_pane, looks_stalled,
                       wake_stalled_sessions, fire_nudge.  Two-tier claude-pane
                       detection: ``@ccswitch-nudge=on`` tmux user option (explicit
                       opt-in) OR a process-ancestry walk from the pane's shell PID
                       to any descendant whose ``comm`` contains ``claude`` (catches
                       bare ``claude`` and native-installer full-path argv[0] alike).
                       ``looks_stalled`` matches only the last 20 lines of the capture
                       after ANSI-escape stripping, so stale scrollback banners do
                       not re-trigger nudges on every swap.
frontend/
  index.html           HTML shell (Accounts page + Settings page + Add-account modal).
                       No shell-warn panel, no credential-targets panel.
  src/
    main.js            Entry point — theme, page toggle, init.
    api.js             Fetch wrapper (30 s timeout, error extraction).
    ws.js              WebSocket client with exponential reconnect + sequence replay.
    state.js           Shared mutable state object.
    constants.js       Timing constants.
    utils.js           DOM helpers.
    toast.js           Toast notifications.
    style.css          Full stylesheet (dark default, light via data-theme).
    ui/
      accounts.js      Account cards, drag-reorder, threshold slider, default selector.
                       No waiting badge, no force-refresh button.
      service.js       Master-switch button.
      log.js           Switch log with pagination.
      login.js         Add-account + re-login modal (same scratch-dir flow for both).
      tmux_nudge.js    Settings-page Wake Tmux Sessions block.
alembic/
  versions/            Schema migrations (auto-run on startup).  The
                       a7e16baf4a03 migration is the one-shot move to the
                       vault-swap architecture — it reads legacy state,
                       writes vault entries, cleans orphan hashed Keychain
                       entries, rmtree's ~/.ccswitch-accounts/, and drops
                       the config_dir column.
tests/
  conftest.py          Tmp-dir isolation + make_test_app factory fixture.
  test_*.py            Router + service + background + schemas + integration tests.
```

## Key data flow

1. `main.lifespan` runs `init_db()` (which applies pending Alembic
   migrations, including the one-shot vault migration for users
   upgrading from the legacy architecture), seeds default settings,
   waits for the login keychain to unlock (exponential backoff up
   to 5 minutes), runs `startup_integrity_check` to reconcile any
   crashed-mid-swap state, then starts **two** background tasks:
   `_poll_loop(idle_interval)` and `_cleanup_sessions_loop()`.
2. `_poll_loop` calls `bg.poll_usage_and_switch(ws_manager)` once at
   startup to warm the caches, then alternates between a tight active
   cadence (`cfg.poll_interval_active`, default 15 s, while any WS
   client is connected) and an idle cadence (DB-configurable, floored
   at `cfg.poll_interval_min`).
3. `poll_usage_and_switch` runs **unconditionally** — usage polling is
   independent of the `service_enabled` master toggle, so the
   dashboard's rate-limit bars stay live even when auto-switching is
   off.  For each account:
   - Active account (email matches `~/.claude.json`'s
     `oauthAccount.emailAddress`): read the access token from the
     standard Keychain entry, probe `/v1/messages`, store the result.
     **Never refresh.**
   - Vault account: read the access token from `ccswitch-vault /
     email`, probe `/v1/messages`, store the result.  **Never refresh
     proactively.**  Refresh only fires reactively — on probe-401
     (see below), on swap-step-0.5 (incoming account on promotion),
     or on an explicit user Revalidate click.
   - Active-probe 401: call `tmux_service.fire_nudge()` (rate-limited
     to at most once per 30 s per account) to wake any sleeping CLI,
     return last-known cached usage, do NOT mark stale.
   - Vault-account probes receive a 401 (access_token invalidated
     server-side) → reactive refresh path in `_process_single_account`:
     acquire `get_refresh_lock(email)`, call `_refresh_vault_token`,
     retry the probe ONCE with the fresh access_token.  Retry success
     → report usage normally, no stale_reason.  Retry still 401 →
     mark `stale_reason = "Anthropic API returned 401 — re-login required"`.
     Refresh terminal (401, or 400 with body `error` in the terminal
     set — RFC 6749 §5.2 + Anthropic `invalid_request_error` /
     `authentication_error`) → stale with the exact refresh-path
     reason.  Refresh transient (network, 5xx, non-terminal 400) →
     no stale_reason this cycle, existing transient-ladder escalation
     after 5 consecutive OR 24 h.  A 60 s cooldown
     (`_last_reactive_refresh_at`) prevents thundering herd across
     concurrent poll cycles; cleared on successful recovery.
   - Vault-account probes are NEVER preceded by a proactive refresh.
     Pre-April-16 CCSwitch refreshed when `expires_at - 20min` window
     opened; this generated ~1 rotation event per idle vault per
     hour, each a broken-chain trigger for Anthropic's server-side
     reuse detection.  Reactive-only refresh aligns with OAuth 2.1
     RTR best practices (Auth0 guidance).  On-demand triggers are:
     probe 401 (above) + swap step 0.5 (below).
   - Swap step 0.5 (between load-incoming and checkpoint-outgoing
     in `_swap_to_account_locked`): attempt one refresh on the
     incoming vault's refresh_token under the shared refresh lock.
     Ensures the newly-active account's access_token is fresh
     BEFORE the CLI sees it (no 401 on first keypress).  Terminal
     → `SwapError`, standard entry not overwritten.  Transient →
     proceed with stored tokens (CLI self-refreshes on first call).
   Per-account 429 backoff (exponential 120 s → 3600 s cap) is
   preserved.  On sleep-wake detection (monotonic gap > 5 min), a
   random 0-30 s stagger runs before the refresh burst to avoid
   tripping Anthropic's refresh-endpoint rate limit.
4. After polling, `switcher.maybe_auto_switch` gates on
   `service_enabled`: if off it returns.  Otherwise, if the active
   account crossed its `threshold_pct` (or came back 429, or is
   stale), it picks the next enabled non-stale account by priority
   and calls `switcher.perform_switch`.
5. Every successful `perform_switch` — auto OR manual — writes a
   `SwitchLog` row, broadcasts `account_switched` over `/ws`, and
   then calls `tmux_service.fire_nudge()` on its tail so every
   running `claude` pane matching a rate-limit stall pattern wakes
   up on the new Keychain credentials.

## Account switching = one swap_to_account call

`account_service.swap_to_account(target_email)` runs the 5-step
atomic sequence under two nested locks — the per-email
``refresh_lock(target_email)`` (threading.Lock, serialises against
concurrent refresh paths on the same email) acquired first, then
``cp._credential_lock`` (threading.RLock, serialises all Keychain
mutations) acquired inside.  Lock-order invariant: refresh_lock is
always OUTSIDE ``cp._credential_lock``.

1. **Load incoming.** Read `ccswitch-vault / target_email`.  Raises
   `SwapError` if missing or has no `refresh_token`.
2. **Checkpoint outgoing.** Read the standard `Claude
   Code-credentials` entry immediately before the overwrite.  Merge
   the freshly-read tokens into the vault entry for the outgoing
   email (preserving the vault's stored `oauthAccount` + `userID`).
   A checkpoint-write failure aborts the swap before step 3, so the
   standard entry is never overwritten on a failed checkpoint.
3. **Promote.** Write the incoming credentials to the standard
   Keychain entry.
4. **Identity file.** Atomically rewrite **`~/.claude.json`** (at HOME
   ROOT — *not* the nested `~/.claude/.claude.json`) — replacing only
   `oauthAccount` and `userID`, preserving every other key (projects,
   MCP state, user prefs).  Creates the file if it does not exist.
   This is the file Claude Code CLI consults on startup for its
   identity when `CLAUDE_CONFIG_DIR` is unset.
5. **File fallback.** Atomically rewrite `~/.claude/.credentials.json`
   at mode 0o600 — a belt-and-braces mirror for Claude Code builds
   that prefer the file over the Keychain.

After the lock is released, `perform_switch` writes a `SwitchLog` row,
broadcasts `account_switched`, and fires `tmux_service.fire_nudge()`
on its tail — regardless of who called it.  Manual switches from the
UI, auto-switches from `maybe_auto_switch`, disable-cascade switches
from `switch_if_active_disabled`, and re-login post-verify swaps all
nudge.

## Startup integrity check

Because step 3 (standard Keychain write) and step 4 (identity file
write) are separate operations, a crash between them leaves the
standard entry holding account B's tokens while `~/.claude.json`
still names account A.  The CLI, if it runs in that window, uses B's
tokens but displays A's email.

`account_service.startup_integrity_check()` reconciles this on every
startup: if the standard entry's `oauthAccount.emailAddress` disagrees
with the identity file's, rewrite the identity file from the standard
entry (the later of the two writes wins).  Logs a prominent warning.

## Login + re-login flow

Both use a transient scratch directory under
`$TMPDIR/ccswitch-login/session-<uuid>/`:

1. `start_login_session()` / `start_relogin_session(expected_email)`
   creates the scratch dir and launches a tmux pane running `claude`
   with `CLAUDE_CONFIG_DIR=<scratch>`.  This is the only place in
   CCSwitch where the env var is ever exported, and it lives for
   seconds.
2. The user completes OAuth in the pane.
3. `verify_login_session(session_id)` reads:
   - `<scratch>/.claude.json` → `oauthAccount` + `userID` + email;
   - the hashed Keychain entry
     `Claude Code-credentials-<sha256(scratch)[:8]>` → the OAuth tokens.
4. The router calls `ac.save_new_vault_account(email, tokens,
   oauth_account, user_id)` to write the canonical vault blob.
5. `cleanup_login_session(session_id)` deletes the scratch hashed
   Keychain entry and `rmtree`s the scratch directory.
6. For add-flow, a DB row is also inserted; the first-ever account
   auto-activates via `swap_to_account`.  For re-login, the DB row
   already exists — `stale_reason` is cleared and, if the account is
   currently active, a fresh `swap_to_account` is run so the standard
   entry and identity file pick up the new tokens.

## Constraints

- **macOS only** for the credential-switching path (uses the `security`
  CLI).  A Linux port would have to provide analogous vault/standard
  credential stores.
- **Local only**.  The `/ws` endpoint has no authentication by default —
  the app is intended to run on `127.0.0.1:41924` behind your browser.
  Host and port are configurable via `CCSWITCH_SERVER_HOST` /
  `CCSWITCH_SERVER_PORT`.  Optional Bearer-token auth via
  `CCSWITCH_API_TOKEN`.
- **tmux required** for the add-account and re-login login flows.
  The tmux nudge is an opt-in feature on the Settings page — off by
  default.
- **Python 3.12+** (the repo's `.venv` runs on 3.14).

## Tests

```bash
uv run python -m pytest tests/ -q
```

`tests/conftest.py` chdirs to a pytest-managed tmp directory for the
session, so hard-coded relative DB URLs (`sqlite+aiosqlite:///./test_*.db`)
end up inside the tmp dir instead of polluting the repo root.

## Concurrency model

- `_credential_lock` (threading.RLock, in credential_provider.py):
  serializes every mutation to the standard Keychain entry, vault
  entries, and the vault refresh path.  Both `swap_to_account` and
  `save_refreshed_vault_token` acquire it.
- `_refresh_locks` (dict[email → threading.Lock], in account_service.py):
  per-email lock serialising the three refresh-token code paths
  (swap step 0.5, poll-loop reactive refresh, user Revalidate) so a
  single-use refresh_token is never presented to Anthropic twice in
  parallel.  Threading (not asyncio) because swap step 0.5 runs on a
  worker thread via asyncio.to_thread + asyncio.run throwaway loop,
  and asyncio.Lock does not serialise across threads/loops.  Async
  callers acquire via `with_refresh_lock_async` (yields the event loop
  via `asyncio.to_thread(lock.acquire)`); swap acquires sync.
  Lock-order invariant: `refresh_lock(email)` is always acquired
  OUTSIDE `cp._credential_lock` — reversing would deadlock under
  contention.
- `_switch_lock` (asyncio.Lock, in switcher.py): serializes concurrent
  `perform_switch` calls so two auto-switches cannot overlap.
- `_sessions_lock` (threading.RLock, in login_session_service.py):
  protects the `_active_login_sessions` dict.  Re-entrant so
  `_cleanup_expired_sessions` can iterate and call
  `cleanup_login_session` (which also acquires it).
- `_UsageCache` (cache.py): `asyncio.Lock` protects in-memory usage
  and token_info dicts.

## Things that are intentionally NOT done

- Alembic is set up for production migrations.  `init_db()` runs
  `alembic upgrade head` on startup, so schema changes require a new
  migration file (`alembic revision --autogenerate`).
- No global exception handler; background task failures are caught
  and logged inside `poll_usage_and_switch` itself.
- No `/ws` auth by default, no CORS config — if you ever expose the
  port remotely you must add both.
- No multi-tenant support (one user per machine).
- No rollback path for the vault migration.  A one-shot JSON backup
  at `~/.ccswitch-backup-2026-04-15.json` is written before the
  migration runs; manual restoration from that file is the only
  way back.  See the design spec for why.
