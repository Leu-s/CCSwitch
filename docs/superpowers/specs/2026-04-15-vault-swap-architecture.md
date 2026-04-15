# CCSwitch Vault-Swap Architecture

**Date:** 2026-04-15
**Status:** APPROVED — source of truth for implementation.
**Supersedes:** `2026-04-14-active-ownership-refresh-fix-design.md`,
`2026-04-15-multi-account-race-free-architecture-design.md`

---

## 1. Empirical finding (the foundation)

Claude Code CLI **re-reads the Keychain on demand**. A sleeping CLI session
wakes up to the new credentials as soon as any input nudges it back into
the API path — one "continue" keystroke per tmux pane is enough.

The user's real workflow is:

```
N cmux panes ─ all share ONE Claude Code identity
      │
      ▼  (rate-limit hit on active account)
      │
 swap credentials in place (Keychain + ~/.claude/.claude.json)
      │
      ▼  (nudge each pane)
      │
 N cmux panes ─ now all using the NEW identity
```

This is **not** parallel multi-account usage. It is **serialised use of
multiple subscriptions** to extend effective rate-limit budget over time.

Every design choice below follows from that observation.

---

## 2. Architecture

### 2.1 One credential location, period

```
~/.claude/                       # the one and only Claude Code home
  ├── .claude.json               # holds the ACTIVE account's oauthAccount
  ├── .credentials.json          # file-based fallback (also rewritten on swap)
  ├── history.jsonl              # shared across all accounts
  └── projects/, plugins/, …     # shared
```

No `~/.ccswitch-accounts/`. No `~/.claude-accounts/`. No symlink farm.
No `CLAUDE_CONFIG_DIR`. No `~/.ccswitch/active` pointer. No shell profile
snippets.

### 2.2 Keychain partition

Two disjoint namespaces:

| Keychain service | Account (`-a`) | Written by | Read by |
|---|---|---|---|
| `Claude Code-credentials` | `$USER` | CCSwitch on swap; CLI on refresh | CLI on every API call; CCSwitch for probe-only |
| `ccswitch-vault`          | `email` | CCSwitch on login, swap-out, and background refresh | CCSwitch only |

**Invariant.** A given account's `refresh_token` lives in **exactly one**
Keychain entry at any instant. A swap physically moves the credentials
between the two services — the token leaves one entry before arriving at
the other.

### 2.3 Refresh ownership, by construction

- **Active account** (its email matches `oauthAccount.emailAddress` in
  `~/.claude/.claude.json`): CCSwitch **never** refreshes. The CLI owns
  the refresh lifecycle. CLI↔CLI coordination is handled upstream by
  Claude Code 2.1.101+'s intra-CLI file lock.
- **Vault accounts** (every other account): CCSwitch is the **sole**
  refresher. The CLI cannot see `ccswitch-vault` entries — different
  service name — so no race is possible by design.

Because the CLI cannot reach vault entries and CCSwitch does not touch
the active entry's `refresh_token`, the CCSwitch↔CLI refresh race that
motivated the old active-ownership spec is structurally eliminated.

### 2.4 The swap (one operation, atomic per account)

`swap_to(target_email)` runs under a single `asyncio.Lock`:

1. **Checkpoint the outgoing account.** Read the `Claude Code-credentials`
   entry (CLI may have rotated it since the last read) and write it to
   `ccswitch-vault / outgoing_email`. No token is lost.
2. **Load the incoming account.** Read `ccswitch-vault / target_email`.
3. **Promote.** Write the incoming credentials to `Claude Code-credentials`.
4. **Update identity file.** Atomically rewrite `~/.claude/.claude.json`
   — replacing only `oauthAccount` and `userID`, preserving all other
   keys (projects, MCP state, user prefs).
5. **File fallback.** Atomically rewrite `~/.claude/.credentials.json`
   with the new tokens. (Claude Code on non-macOS, and various CLI
   tooling, prefer this file.)
6. **Nudge.** Send a configurable keystroke to every tmux pane whose
   `pane_current_command` looks like `claude` — the tmux nudge logic
   already in place, unchanged.
7. **Broadcast.** Emit `account_switched` over WebSocket.

Steps 1–5 are the data move. Step 6 wakes sleeping CLIs. Step 7 tells
the UI.

### 2.5 The poll loop (unchanged shape, simpler body)

For **every** enabled account on every poll cycle:

- If the account is active: read the access token from
  `Claude Code-credentials`, probe `/v1/messages` for rate-limit
  headers, store the result. Never refresh.
- If the account is in the vault: read the access token from
  `ccswitch-vault / email`. If it's within 20 minutes of expiry,
  refresh (CCSwitch is sole consumer, no skew concerns). Probe. Store.

Per-account 429 backoff (exponential, 120 s → 3600 s cap) is preserved.

After polling, `maybe_auto_switch` runs exactly as today — picks the
next eligible account by priority, calls `swap_to`, fires tmux nudge.
The only change: the threshold compare uses `email`, not pointer files.

### 2.6 The login flow (scratch directory, not permanent)

Adding or re-logging an account:

1. Create a temporary directory in `$TMPDIR/ccswitch-login-{session}/`.
2. Launch `claude /login` in a tmux window with
   `CLAUDE_CONFIG_DIR=$temp_dir`. This is the **only** time CCSwitch
   sets `CLAUDE_CONFIG_DIR`, and it is scoped to the child process.
3. User completes OAuth in that pane.
4. Read the credentials from the temp dir's hashed Keychain entry
   (`Claude Code-credentials-<sha256(temp_dir)[:8]>`).
5. Write those credentials to `ccswitch-vault / email`.
6. Delete the temp hashed Keychain entry. `rmtree` the temp dir.
7. Insert the `Account` DB row.

The per-account temp directory exists for seconds, not forever. The
isolation mechanism Claude Code provides is used only as a transient
bootstrap vehicle to get the OAuth dance done without clobbering the
active account.

### 2.7 First-account activation

The very first account added becomes active immediately: its vault
entry is copied to `Claude Code-credentials`, and `~/.claude/.claude.json`
is updated with its `oauthAccount`. After that, every add is an
inactive-by-default vault entry — the user activates it via manual
switch or auto-switch picks it up.

---

## 3. Data model

### 3.1 `Account` row

```
id             INT           primary key
email          VARCHAR       unique
priority       INT           auto-switch order
threshold_pct  FLOAT         per-account switch threshold
enabled        BOOL          user-visible on/off
stale_reason   VARCHAR NULL  terminal re-login required
created_at, updated_at
```

**Removed:** `config_dir`. Accounts have no filesystem identity; they
are identified by `email`. Vault Keychain entries are derived as
`service = "ccswitch-vault"`, `account = email`.

### 3.2 `SwitchLog` and `Setting` rows: unchanged.

### 3.3 Terminal `stale_reason` strings (the only kinds that remain)

1. `"No access token in vault — re-login required"`
2. `"Refresh token revoked — re-login required"` (HTTP 401 from `/oauth/token`)
3. `"Refresh token rejected (400) — re-login required"` (HTTP 400 from `/oauth/token`)
4. `"Anthropic API returned 401 — re-login required"` (probe 401 after
   successful refresh — rare)

No transient states. No `waiting_for_cli`. No soft stale.

---

## 4. Migration (one-shot, idempotent)

On first startup of the new version, `migrate_to_vault()` runs:

1. **Read legacy state.** For each `Account` DB row that has the (still
   present on-disk) `config_dir` column set:
   - Read its credentials from the hashed Keychain entry
     (`Claude Code-credentials-<sha256(config_dir)[:8]>`) or the
     `.credentials.json` file inside the config_dir.
   - Write those credentials to `ccswitch-vault / email`.
   - Delete the hashed Keychain entry.
2. **Determine active.** Use `~/.ccswitch/active`'s pointer target (if
   present) or the email currently in `~/.claude/.claude.json`'s
   `oauthAccount`. That account's credentials are also copied into the
   legacy `Claude Code-credentials` entry (idempotent — they may
   already be there).
3. **Remove directories.** `rmtree` every `~/.ccswitch-accounts/
   account-*` directory. Remove `~/.ccswitch/active` and its containing
   `~/.ccswitch/` directory.
4. **Remove legacy `~/.claude-accounts/` symlink structure** if it
   exists (some users set this up by hand before CCSwitch existed).
   Never touch `~/.claude` itself.
5. **DB migration.** Alembic migration drops the `config_dir` column.
6. **Mark migrated.** Write a `schema_version = 2` row to `Setting`
   so subsequent startups skip the migration.

Users with a burned (rejected `refresh_token`) account simply re-login
via the UI after migration — CCSwitch does not try to salvage dead
tokens.

The migration is destructive on purpose. There is no "rollback" path
and no compatibility shim. The new code does not read `config_dir`.

---

## 5. What comes out of the codebase

### Backend

- `backend/services/credential_targets.py` — **deleted**.
- `backend/routers/credential_targets.py` — **deleted**.
- `backend/services/account_service.py`
  - Delete: `accounts_base`, `get_active_config_dir_pointer`,
    `clear_active_config_dir`, `write_active_config_dir`,
    `_system_default_canonicals`, `sync_active_to_targets`,
    `_force_refresh_locks`, `_get_force_refresh_lock`,
    `force_refresh_config_dir`.
  - Rewrite: `activate_account_config` →
    `swap_to_account(target_email)`; `_activate_account_config_locked`
    → `_swap_to_account_locked`; `get_active_email` (reads
    `~/.claude/.claude.json` directly); `build_ws_snapshot` (drops
    `waiting_for_cli` and `config_dir`).
- `backend/services/credential_provider.py`
  - Delete: `active_dir_pointer_path`, `_keychain_service_name`,
    `wipe_credentials_for_config_dir`.
  - Add: `VAULT_SERVICE = "ccswitch-vault"`,
    `STANDARD_SERVICE = "Claude Code-credentials"`;
    `read_vault(email)`, `write_vault(email, creds)`,
    `delete_vault(email)`, `read_standard()`, `write_standard(creds)`.
  - Rewrite: `save_refreshed_token` → takes `email`, writes to vault
    only (never touches standard entry — that is owned by CLI).
- `backend/services/switcher.py`
  - Delete: `perform_sync_to_targets`.
  - Rewrite: `perform_switch` calls new `ac.swap_to_account(email)`,
    does not fetch `enabled_targets`.
- `backend/services/login_session_service.py`
  - Rewrite: use `$TMPDIR/ccswitch-login-{session}/` instead of
    `~/.ccswitch-accounts/account-{session}/`. Login result no
    longer carries `config_dir` to the caller.
- `backend/background.py`
  - Delete: `_REFRESH_SKEW_MS_INACTIVE` (rename to `_REFRESH_SKEW_MS`
    since it is now the only one).
  - Delete: active-ownership gate's mid-cycle re-check branch
    (lines 98–114), the 401-retry-after-Keychain-reread block
    (lines 195–221), the active-401 soft-waiting branch
    (lines 223–251), every `waiting_for_cli` field assignment, every
    `cache.set_waiting` / `clear_waiting` call.
  - Rewrite: `active_cfg_dir` is now `active_email`; `is_active` is
    email equality; refresh is gated on `not stale_reason and not
    is_active` (same shape, different derivation).
- `backend/cache.py`
  - Delete: `_waiting` set, `set_waiting`, `clear_waiting`,
    `is_waiting_async`.
- `backend/routers/accounts.py`
  - Delete: `force_refresh_account` endpoint (133 LOC).
  - Delete: `ac._force_refresh_locks.pop` call in `delete_account`.
  - Rewrite: `list_accounts`, `start_login`, `verify_login`,
    `verify_relogin`, `delete_account` to work with the new model.
- `backend/routers/settings.py`
  - Delete: `/api/settings/shell-status` endpoint.
  - Delete: `/api/settings/setup-shell` endpoint.
  - Delete: `_shell_snippet_path` helper.
- `backend/routers/service.py`
  - Rewrite: `enable_service` / `disable_service` — drop
    backup/restore ceremony (no credential mirroring to undo).
- `backend/main.py`
  - Delete: `from .routers import credential_targets` and
    `app.include_router(credential_targets.router)`.
  - Delete: the `~/.ccswitch/active` startup sync block
    (current lines 80–88).
- `backend/models.py`
  - Drop `config_dir` column.
- `backend/schemas.py`
  - Drop `config_dir` from `LoginSessionOut`.
  - Drop `waiting_for_cli` from `AccountWithUsage`.
  - Delete `CredentialTargetOut`, `CredentialTargetUpdate`.
- `backend/config.py`
  - Delete `accounts_base_dir`.

### Frontend

- `frontend/src/ui/credential_targets.js` — **deleted**.
- `frontend/index.html`
  - Delete: `#shell-warn` panel (shell integration warning).
  - Delete: `.ct-panel` section (credential targets panel).
- `frontend/src/main.js`
  - Delete: `credential_targets` imports + calls.
  - Delete: shell-warning logic (the "export CLAUDE_CONFIG_DIR" hint).
- `frontend/src/ws.js`
  - Delete: `waiting_for_cli` reset in the `account_switched` handler.
- `frontend/src/ui/accounts.js`
  - Delete: `isWaiting` derivation; waiting banner; waiting pill;
    force-refresh button; force-refresh click handler.
- `frontend/src/style.css`
  - Delete: `.waiting-pill`, `.waiting-banner`, `.account-card.waiting`,
    `@keyframes waiting-pulse`, all `.ct-*` classes.

### Tests

- Delete: `tests/test_credential_targets.py`,
  `tests/test_credential_targets_router.py`.
- Delete the following targeted tests (validate obsolete behavior):
  - `test_active_account_401_enters_waiting_state`
  - `test_inactive_account_401_marks_stale_not_waiting`
  - `test_successful_probe_clears_waiting_flag`
  - `test_top_level_exception_clears_waiting_flag`
  - `test_broadcast_single_account_gates_waiting_by_is_active`
  - `test_build_ws_snapshot_gates_waiting_by_is_active`
  - `test_build_ws_snapshot_stale_wins_over_waiting`
  - every `test_force_refresh_*` (about 15 of them)
  - `test_delete_account_cleans_up_force_refresh_lock`
  - `test_cache_invalidate_drops_waiting_flag`
  - `test_perform_switch_clears_waiting_for_both_sides`
  - `TestKeychainServiceName` class (`_keychain_service_name` gone)
- Rewrite: `test_activate_account.py` → `test_swap.py` (covers the
  new 6-step swap); `test_credential_provider.py` (vault helpers);
  `test_account_service.py` (swap_to_account, get_active_email);
  `test_switcher.py` (drops enabled_targets fixtures);
  `test_integration_auto_switch.py` (new swap path).
- Add: `test_migration.py` (verifies one-shot migration from legacy
  state to vault).

### Docs

- `CLAUDE.md` — rewrite Overview, Layout, Data flow, delete
  "Active-ownership refresh model" section entirely, rewrite
  "Account switching" section, rewrite Concurrency model.
- `README.md` — rewrite credential-targets features (remove),
  shell integration (remove), "Per-account directories" references.
- This spec is the new source of truth. Mark the two prior specs
  SUPERSEDED with a one-paragraph header explaining why.

---

## 6. Race model

Each of the old failure modes is either eliminated structurally or
absorbed into a small, documented window:

| Failure mode | Old status | New status |
|---|---|---|
| CLI↔CCSwitch refresh race on active account | fatal | **impossible** — CCSwitch never refreshes the active entry |
| CLI↔CCSwitch refresh race on inactive account | latent | **impossible** — inactive tokens live in vault, CLI cannot reach |
| CLI↔CLI refresh race | handled upstream | unchanged (2.1.101+ file lock) |
| Swap races with CLI's ongoing refresh | small window | recoverable — CCSwitch checkpoints active entry just before overwriting, preserving any rotation the CLI landed mid-swap |
| Switch during CLI token expiry | transient 401 blip | CLI re-reads Keychain, sees the new token on retry, recovers |
| Dormant vault account expires | stale cascade | CCSwitch refreshes ahead of expiry (20 min window, sole consumer) |

The only remaining narrow window is step 1→3 of the swap (between
checkpointing the outgoing entry and writing the incoming one). If the
CLI performs an API call in that millisecond, it sees the already-
valid outgoing access token from the in-memory Keychain state the OS
gives it, not the about-to-be-overwritten entry. This has been the
behavior throughout CCSwitch's life and has not produced failures in
the observed workflow.

---

## 7. What stays the same

- The tmux nudge mechanism (`backend/services/tmux_service.py`).
- The usage probe (`backend/services/anthropic_api.py::probe_usage`).
- The WebSocket broadcast shape (modulo the `waiting_for_cli` field
  dropping out of `usage_updated` entries).
- Auto-switch decision logic (threshold + 429 trigger).
- Per-account 429 exponential backoff.
- The Add-Account and Re-Login UX from the user's perspective —
  still a tmux window, still OAuth in the browser, still the same
  confirmation screen. Only the storage target changes.

---

## 8. Non-negotiables (restated)

- No backward-compatibility toggles. No dual-architecture code paths.
  No "legacy mode" settings.
- No per-account directories anywhere in the runtime state.
- No `CLAUDE_CONFIG_DIR` export, ever, except the short-lived child
  process during the add/re-login flow.
- No "waiting for CLI" UI state. No force-refresh button. No soft
  stale reasons.
- No pointer file (`~/.ccswitch/active`). Active account is read from
  `~/.claude/.claude.json`.
- Migration is one-shot and destructive; burned-token accounts are
  re-logged by the user after deploy.
