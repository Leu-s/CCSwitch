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

### 2.4 The swap (ordered for crash safety)

`swap_to(target_email)` runs under a single `asyncio.Lock`. The step
order is deliberate: the write-side of the swap proceeds from
least-visible (vault) to most-visible (identity file) so that a crash
mid-sequence leaves a state that can be detected and reconciled on
restart.

1. **Load the incoming account into memory.** Read
   `ccswitch-vault / target_email`. If the entry does not exist, abort
   the swap with a clear error (the caller is trying to activate an
   account the vault does not know about — a bug, not a recoverable
   state).
2. **Checkpoint the outgoing account — immediately before the
   overwrite, inside the lock, with no intervening work.** Read the
   `Claude Code-credentials` entry. Compare against the in-memory
   outgoing credentials if any exist from a recent poll; if the standard
   entry's `access_token` or `refresh_token` changed since that poll,
   the CLI has rotated while we were holding the lock — accept the
   rotation and write *the freshly-read value* into
   `ccswitch-vault / outgoing_email`. If there is no outgoing account
   (first activation on this machine, see §2.7), skip this step.
3. **Promote.** Write the incoming credentials from step 1 into
   `Claude Code-credentials`.
4. **Update identity file.** Atomically rewrite **`~/.claude.json`**
   (at HOME ROOT, NOT inside `~/.claude/`) — replacing only
   `oauthAccount` and `userID`, preserving all other keys (projects,
   MCP state, user prefs). If the file does not exist, create it with
   mode 0o600 containing only those two keys. This is the file Claude
   Code CLI consults on startup when `CLAUDE_CONFIG_DIR` is unset;
   writing to `~/.claude/.claude.json` instead leaves the CLI's
   `/stats` display stuck on the pre-swap identity.
5. **File fallback.** Atomically rewrite `~/.claude/.credentials.json`
   with the new tokens at mode 0o600. This is a belt-and-braces mirror
   against any Claude Code build that falls back to the file (Linux,
   older macOS builds, future versions).
6. **Release lock, then nudge.** Send a configurable keystroke to
   every tmux pane whose `pane_current_command` looks like `claude` —
   the `tmux_service.fire_nudge()` logic already in place, unchanged.
7. **Broadcast.** Emit `account_switched` over WebSocket.

Steps 1–5 are the data move, ordered so that at every boundary either
the swap is visibly complete (all five steps landed) or the state is
identifiable as mid-swap (see §9.1 for the startup reconciliation). In
particular, the standard Keychain entry is written **after** the vault
checkpoint and **before** the identity file, so the invariant
"`~/.claude/.claude.json`'s email matches the standard entry's owner"
is broken only during steps 3–4 — a window measured in tens of
milliseconds — and is observable on restart.

### 2.5 The poll loop (unchanged shape, simpler body)

For **every** enabled account on every poll cycle:

- If the account is active: read the access token from
  `Claude Code-credentials`, probe `/v1/messages` for rate-limit
  headers, store the result. **Never refresh.** A 401 on the active
  probe is treated as a transient blip — the CLI has either not yet
  rotated a freshly-minted token into the Keychain, or the stored
  access token is expired and the CLI has not been invoked recently.
  CCSwitch does **not** mark the account stale on a probe 401; it
  calls `tmux_service.fire_nudge()` once to wake any sleeping CLI pane
  (which will trigger the CLI's own refresh on its next API call) and
  returns the last-known usage data from the cache. If the pane wakes
  up, the next poll cycle sees the fresh token and recovers. If there
  is no claude pane open anywhere, the UI shows a small
  "access token stale — type in any claude terminal to refresh" note
  on the active card; no red banner, no re-login prompt.
- If the account is in the vault: read the access token from
  `ccswitch-vault / email`. If it's within 20 minutes of expiry,
  refresh (CCSwitch is sole consumer, no skew concerns). Probe. Store.

Per-account 429 backoff (exponential, 120 s → 3600 s cap) is preserved.

**Stagger after sleep.** If the poll cycle detects `time.monotonic()`
jumping by more than 5 minutes between iterations — a strong signal
that the Mac was asleep — it sleeps a random interval of 0–30 seconds
before dispatching refresh calls. With N accounts all expiring during
sleep, a burst of N concurrent `/oauth/token` POSTs in one second
looks like bot traffic to Anthropic; a bounded stagger removes that
signal without meaningfully delaying recovery.

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

**Bootstrap edge cases.** The swap in §2.4 must handle all three of:

- **No `~/.claude/` directory.** Create it with `os.makedirs(path,
  mode=0o700, exist_ok=True)` before step 4.
- **No `~/.claude/.claude.json` file.** Step 4 writes a minimal file
  containing only `oauthAccount` and `userID` — the `_load_json_safe`
  helper already returns `{}` on a missing file, and the atomic
  rewrite then creates the target.
- **No standard Keychain entry.** Step 2 of §2.4 is skipped outright
  when the read returns empty — there is nothing to checkpoint. This
  also covers the case where the standard entry holds credentials for
  an email that CCSwitch has never tracked (e.g., the user ran
  `claude login` before installing CCSwitch). In that case CCSwitch
  writes those stray credentials into
  `ccswitch-vault / __orphan_<email>__` rather than a normal vault
  entry, logs a warning, and surfaces the orphan in the Settings page
  with a one-click "delete" button. The user can ignore, delete, or
  manually add the orphan's email as a real account.

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
4. `"Anthropic API returned 401 — re-login required"` (vault-account
   probe 401 after successful refresh — the account is genuinely
   unreachable)

The DB column `Account.stale_reason` is only written for these four
cases — all of them require the user to re-login via the UI.
Transient probe failures (network timeout, DNS failure, Anthropic
5xx, 429) are cached as `{"error": ..., "rate_limited": ...}` entries
in memory for the next poll cycle to re-evaluate; they never persist
to `stale_reason`. An **active-account probe 401** is explicitly not
one of the four — it means "the stored access token is expired and
CCSwitch refuses to refresh," which is a CLI-wake-up problem, not a
re-login problem (see §2.5 active-probe handling).

---

## 4. Migration (one-shot, idempotent per step)

The migration runs inside the Alembic upgrade function for the new
revision (`<rev>_migrate_to_vault_and_drop_config_dir`). Running it
from Alembic gives us transactional DB changes alongside the Keychain
moves, and means the migration is uniquely gated by the Alembic
revision table — re-runs after a crash are a no-op by construction
*at the DB-change grain*. The Keychain moves inside the function are
additionally made idempotent **at the per-account grain** so a crash
mid-run replays cleanly.

Before any mutation, `dump_legacy_state_to_backup()` writes a JSON
backup to `~/.ccswitch-backup-2026-04-15.json` containing:

- every `Account` DB row (all columns including `config_dir`);
- the contents of every hashed `Claude Code-credentials-<hash>`
  Keychain entry the migration will touch, base64-encoded;
- the contents of `~/.ccswitch/active`, if present;
- the contents of `~/.claude/.claude.json`, if present.

The backup is a one-shot safety net — the user can manually restore
credentials from it if something catastrophic happens. It is not
consumed by any code path; the spec does not promise a rollback tool.

After the backup is written:

1. **Per-account move.** For each `Account` row (iterated in a
   deterministic order):
   - Check `read_vault(email)`. If it already returns credentials
     that include a `refresh_token`, skip this account — a prior run
     migrated it. Do not overwrite.
   - Otherwise read credentials from the hashed Keychain entry
     `Claude Code-credentials-<sha256(config_dir)[:8]>`, falling back
     to `.credentials.json` inside the config_dir.
   - **Validate before writing.** If the credentials contain no
     `refresh_token`, set `account.stale_reason = "No access token in
     vault — re-login required"` and skip the vault write. The user
     will re-login via the UI after migration.
   - Write credentials to `ccswitch-vault / email`.
   - After the vault write succeeds, delete the hashed Keychain entry.
     A failure at this step leaves a harmless duplicate that a later
     cleanup sweep removes.
2. **Determine active.** Read `~/.ccswitch/active` if present to get
   the pointer target; map it back to an email via the DB. If the
   pointer is absent or unmappable, fall back to
   `~/.claude/.claude.json`'s `oauthAccount.emailAddress`. If both are
   absent, leave the active state empty — the user picks one on first
   open.
3. **Promote active.** Copy the active account's vault entry into
   `Claude Code-credentials` (idempotent — the vault write is the
   source of truth). Update `~/.claude/.claude.json` with the active
   account's `oauthAccount` + `userID`, creating the file if it does
   not exist. Atomically rewrite `~/.claude/.credentials.json` with
   the active tokens.
4. **Orphan Keychain sweep.** Enumerate all keychain entries whose
   service name matches `Claude Code-credentials-*` (hashed pattern).
   For every such entry whose hash does not correspond to any Account
   row's config_dir (stale from earlier deletions, broken migrations,
   etc.), delete it. The goal is that after migration completes, the
   only remaining `Claude Code-credentials*` entries are the standard
   one and the vault ones.
5. **Remove directories.** `rmtree` every `~/.ccswitch-accounts/`
   subdirectory. Remove `~/.ccswitch/active` and its containing
   `~/.ccswitch/` directory. Leave `~/.claude/` and its contents
   intact. Optionally remove `~/.claude-accounts/` if it exists and
   contains only symlinks or is empty — never delete if it contains
   non-symlink data.
6. **Drop column.** `op.drop_column('accounts', 'config_dir')`.

Alembic's own revision table records that the migration ran. Re-runs
are impossible without `alembic downgrade`, which is explicitly not
supported for this migration (downgrade raises `NotImplementedError`).

Users with a burned (already-rejected) `refresh_token` at migration
time end up with a valid vault entry carrying stale credentials plus
a `stale_reason` (set by the poll loop on first probe, or by the
validation step in #1). They re-login via the UI to recover. The
spec does not attempt to salvage dead tokens.

The migration is destructive on purpose. There is no rollback path
and no dual-runtime toggle. The new code does not read `config_dir`.
Downgrading to a pre-migration binary is explicitly unsupported —
if the user needs to revert, they restore the JSON backup from
step 0 by hand.

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
  - Delete `accounts_base_dir`, `state_dir`, `active_claude_dir`.
    All three were helpers for a layout that no longer exists.
    `~/.claude/` is hardcoded where needed — the override was
    always suspect and untested.
- `backend/routers/service.py`
  - Rewrite `enable_service`: if the current active email (read from
    `~/.claude/.claude.json`) is already present in the DB as an
    enabled account, set `service_enabled = true` and return — do NOT
    swap. Only if no valid active account exists is the default (or
    first) enabled account activated. The current code's backup /
    force-swap / disable-restores dance is deleted entirely.
  - Rewrite `disable_service`: set `service_enabled = false`. Done.
    No credential restore.

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

---

## 9. Robustness and edge cases

This section addresses concerns raised during spec review (both a
code-reviewer pass and an adversary pass) that are not structural to
the architecture but must be handled correctly by the implementation.

### 9.1 Startup integrity check

Because steps 3 and 4 of the swap are separate Keychain + filesystem
writes, a crash between them leaves a visible inconsistency: the
standard Keychain entry holds account B's tokens, but
`~/.claude/.claude.json` still names account A as the active
`oauthAccount`. The CLI, if it runs in that window, reads the standard
entry (getting B's tokens) but displays A's identity.

On every startup, before any background task runs, CCSwitch performs
a single integrity check:

1. Read the active email from `~/.claude/.claude.json` — call it
   `identity_email`.
2. Read the standard `Claude Code-credentials` entry — extract the
   `oauthAccount` subfield if present, compare its
   `emailAddress` to `identity_email`.
3. If they disagree, rewrite `~/.claude/.claude.json` with the
   standard entry's `oauthAccount` (the later of the two writes in a
   crashed swap). Log a warning with enough detail to investigate.

If the standard entry has no `oauthAccount` subfield (older format),
skip the check. The worst case — silent disagreement — is recoverable
on the next swap.

### 9.2 Keychain-locked degraded mode

On a fresh Mac boot, the login keychain may not yet be unlocked when
the CCSwitch LaunchAgent starts (FileVault + Touch ID, or a long
password, can delay unlock until the user interacts with the
screen). A `security find-generic-password` against a locked keychain
returns error -25308 (`errSecInteractionNotAllowed`) or times out.

CCSwitch treats Keychain-unavailable as **a distinct state from
stale_reason**:

- On startup, probe with a dummy `security find-generic-password -s
  Claude Code-credentials`. If it returns -25308 or -25307 (user
  canceled, interaction not allowed, keychain locked), enter a
  "Keychain locked" mode: show an amber banner on the UI, skip all
  probe + refresh cycles, and retry the dummy probe with exponential
  backoff (5 s, 10 s, 30 s, 60 s, 300 s, …).
- When the dummy probe eventually succeeds, resume normal operation.
- Under no circumstance does Keychain-unavailable write to
  `stale_reason`. A locked keychain is a transient operational state
  that the user can resolve by logging in.

### 9.3 Active-probe 401 handling

Spelled out in §2.5. Restated here for emphasis:

- Active-account probe 401 does **not** set `stale_reason`.
- It triggers a one-shot `tmux_service.fire_nudge()` call (rate-
  limited per account to at most once every 30 seconds).
- The UI shows a small informational note on the active card
  ("access token stale — open any claude terminal to refresh"), but
  not the red stale banner.
- The next poll cycle picks up the CLI's refresh automatically.
- If no claude pane exists and the user cannot easily open one (VS
  Code Claude extension, Claude Desktop), they can still interact
  with those tools; any API call from any `claude`-consuming process
  on the machine refreshes the Keychain entry.

This narrowly preserves the failure-mode of "no CLI running anywhere"
as the single edge case where recovery requires user action, which
matches the user's workflow assumption: they always have at least
one claude pane open.

### 9.4 CLI mid-refresh during swap

The swap's step 2 reads the standard entry *immediately before* step
3 overwrites it. If the CLI rotated the outgoing account between the
previous poll and this swap, step 2 sees the rotated value and writes
it into the vault. If the CLI rotates between steps 2 and 3 (tens of
milliseconds), the rotated value is lost — CCSwitch wrote the
pre-rotation snapshot. The mitigation:

- On the **next** swap back to that account, step 2 reads the then-
  standard entry again; if anything has touched it since (CLI during
  the interim period), the re-read captures it.
- If CCSwitch's vault entry for account A was briefly stale and A is
  promoted before a poll cycle refreshes the vault copy from the CLI,
  the first refresh attempt via `/oauth/token` returns 400 (the
  server already rotated). CCSwitch marks the account stale with
  `"Refresh token rejected (400) — re-login required"`. Rare; the
  user re-logs in.

The spec accepts a narrow residual race for the benefit of not
serializing with the CLI. This is a deliberate tradeoff — elimination
would require the CLI to cooperate, which it does not.

### 9.5 Concurrent-startup safety

If the user starts two CCSwitch processes at once (e.g., a dev
instance + the LaunchAgent), the Alembic migration must not run
twice. Alembic's built-in revision table serializes this — whichever
process reaches the migration first holds an exclusive lock; the
second waits and then sees the migration as already applied.

For the runtime loops: each process has its own in-memory
`_switch_lock` and cache, so two processes can race on the standard
Keychain entry. This is a user error; CCSwitch logs a prominent
warning if it detects another CCSwitch process running on the same
DB file (via a file-lock sentinel in `settings.state_dir` — but
that's gone, so use `~/.claude/ccswitch.lock`).

### 9.6 `looks_stalled` false positives

The tmux nudge heuristic matches rate-limit UI strings in the last
200 lines of a pane. A user composing a message that contains a
rate-limit word ("rate limited my experiment") can trigger a nudge.
The implementation adds two guards:

- Only nudge panes whose last-output timestamp is more than 60 s
  old (i.e., pane is idle).
- The match pattern is anchored on the specific Claude Code UI
  string (`"You've hit your … limit"`), not a generic keyword.

### 9.7 The empirical claim and how we verify it

The architecture hangs on: "Claude Code CLI picks up new credentials
when nudged, with no restart." The user has confirmed this in their
own 20-pane workflow. Before merging the implementation, the
implementer runs the following manual verification:

1. Open a tmux pane running `claude`.
2. From another terminal, overwrite the `Claude Code-credentials`
   Keychain entry with a different account's tokens (use the current
   CCSwitch to do the swap manually).
3. In the `claude` pane, press Enter or type a word. Observe that
   the next API call uses the new account (check the bill / model
   trace / session email in the pane's next response).
4. If the observation matches, the empirical claim holds for that
   Claude Code build.
5. If it does NOT match (e.g., the CLI uses the old in-memory token),
   the implementer falls back to: nudge the pane by sending a
   SIGTERM to the `claude` process, letting cmux/tmux restart it.
   Document this fallback in the implementation, gated on a setting.

The verification result is recorded in the PR description that
introduces the new architecture. If the fallback path is needed, the
spec is amended to describe the kill-and-restart mechanism before
merge.

### 9.8 Keychain entry naming and user-visibility

Vault Keychain entries appear in Keychain Access.app under the
service name `ccswitch-vault` with the account field set to the
email. To discourage accidental deletion, the implementation sets
the Keychain entry's **comment field** to:

```
CCSwitch subscription vault — do not delete. Managed by the
CCSwitch dashboard at http://127.0.0.1:41924.
```

This does not prevent deletion but gives the user the information
they need if they poke around.

### 9.9 Rollback is unsupported by design

Downgrading the CCSwitch binary past this spec's revision is
explicitly unsupported. The architecture's simplifications are not
compatible with old code paths that read `config_dir` from DB rows
or hashed Keychain entries. A user who needs to downgrade restores
the `~/.ccswitch-backup-2026-04-15.json` backup file manually, which
contains enough information (base64'd hashed-entry contents + DB
row dump + original `.claude.json`) to reconstruct the legacy state
outside CCSwitch.

The user instruction for this work is a hard cutover; the spec
codifies that as "no rollback."
