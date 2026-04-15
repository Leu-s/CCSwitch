# Active-Ownership Refresh Model

**Date:** 2026-04-14
**Status:** **SUPERSEDED** — see `2026-04-15-vault-swap-architecture.md`.

> This design tried to prevent CCSwitch↔CLI refresh races by partitioning
> ownership through the `~/.ccswitch/active` pointer: CCSwitch would refresh
> all inactive accounts, and skip whichever one the CLI was using.
>
> **Why superseded.** The user's real workflow is N parallel cmux panes
> **sharing one active account** — not N parallel CLIs each on their own
> account. The model's "CLI only touches the pointer target" premise was
> built for a workflow that does not exist here. The new Vault-Swap
> architecture eliminates the race *structurally* instead of through
> ownership choreography: active credentials live in the standard
> `Claude Code-credentials` Keychain entry (CLI writes it during refresh;
> CCSwitch never does), inactive credentials live in private
> `ccswitch-vault` entries the CLI cannot see. No partition logic, no
> waiting state, no force-refresh escape hatch, no per-config-dir locks.
>
> The code, tests, docs, and UI described below are removed by the new
> architecture.

---

## Original design (retained for history)

**Date:** 2026-04-14
**Status:** Approved (at time of writing)

## Problem

Anthropic's OAuth refresh tokens are single-use with no grace period. Today,
`_process_single_account` in `backend/background.py` (lines 64–110) calls
`refresh_access_token` whenever any account's cached access token is within
5 minutes of expiry — regardless of whether another process is also consuming
that account's credentials. The Claude Code CLI caches refresh tokens
in-memory and refreshes on its own schedule. When CCSwitch and the CLI
refresh the same account concurrently, one side loses: the server returns
HTTP 404 on the second-use refresh token, the loser's stored copy is
permanently dead, and the account needs manual re-login.

The race is reproducing in production: the user runs multiple Claude Code
sessions 24/7 (tmux panes, VS Code, various projects). Claude Code's own
v2.1.81 mitigation (a `proper-lockfile` lock inside `cli.js`) only serializes
refreshes between Claude Code processes — CCSwitch is a separate Python
process and does not participate in that lock.

## Decision

**Core invariant.** CCSwitch does not initiate token refresh for the account
whose config dir matches `~/.ccswitch/active`. Claude Code owns that
account's refresh lifecycle while it is active. CCSwitch refreshes only the
N-1 inactive accounts — for those, CCSwitch is the sole consumer and no
race is possible.

Two belt-and-braces pieces from the minimal-fix option are kept as
defense-in-depth:

1. **Widen the non-active refresh window from 5 min → 20 min.** Inactive
   accounts have a single consumer, so a wider window simply refreshes
   sooner. Zero collision surface.
2. **Retry probe once after re-reading the Keychain on 401.** If the CLI
   just rotated the active account's token, CCSwitch's cached copy is
   stale; a re-read picks up the fresh token and the probe succeeds on
   retry. Applies to both active and inactive branches.

Active-account 401 that cannot be resolved via re-read becomes a transient
"waiting for Claude Code CLI" soft state — not `stale_reason`. A manual
"Force refresh" affordance lets users who run CCSwitch without an active
CLI push the refresh through themselves.

## Backend

### `backend/background.py`

- `poll_usage_and_switch` snaps `active_config_dir` once per poll cycle via
  `ac.get_active_config_dir_pointer()` (one pointer-file read) and passes
  it into each `_process_single_account` invocation. No per-account
  subprocess calls.
- `_process_single_account` gains an `active_cfg_dir` parameter and
  derives `is_active` by comparing `os.path.abspath(account.config_dir)`
  against the snapped pointer value.
- The refresh block at lines 64–110 is gated on `not account.stale_reason
  and not is_active`. The 5-min window constant in that block becomes
  20 min (`1_200_000` ms). Active accounts are never refreshed from the
  poll loop.
- The probe 401 handler at lines 137–152 gains a single-retry path: re-read
  via `ac.get_access_token_from_config_dir` on a worker thread, and if the
  Keychain holds a different token, refresh `token_info` cache and retry
  `probe_usage` once.
  - Retry success → continue as normal, usage_entry reflects the fresh
    probe result.
  - Retry failure (or same/no token), active path → set transient
    `waiting_for_cli = True` on the usage_entry; do NOT set
    `new_stale_reason`; do NOT persist.
  - Retry failure, inactive path → set `new_stale_reason` exactly as
    today ("Anthropic API returned 401 — re-login required").
- `usage_entry` broadcast shape gains one optional field:
  `waiting_for_cli: bool` (default false). `error` remains the existing
  error-string field.

### `backend/routers/accounts.py`

- New endpoint: `POST /api/accounts/{account_id}/force-refresh`.
  - Intended for the "CCSwitch enabled, Claude Code not running" case
    where the user knows no concurrent refresher exists. The frontend
    only surfaces the button while the card is in the soft "waiting for
    CLI" state.
  - Acquires `_credential_lock` (via an `account_service` helper), reads
    the refresh token, calls `anthropic_api.refresh_access_token`, saves
    the result via `save_refreshed_token`, invalidates the cache entry,
    and broadcasts `usage_updated`.
  - On upstream 400/401 (revoked or rotated-out refresh token): set
    `account.stale_reason = "Refresh token rejected — re-login required"`,
    commit, broadcast, return 409. The card flips from "waiting" to
    the existing stale state so the user can click re-login.
  - On other upstream errors: return 502 without mutating state.
  - On account-not-found: return 404.

### `backend/services/account_service.py`

- No functional changes. `get_active_config_dir_pointer` at line 105 is
  the exact helper the poll loop needs.
- Optionally add a one-liner `is_active_config_dir(config_dir)` helper if
  the comparison is repeated in more than one place; otherwise inline.

### `backend/services/credential_provider.py`

- No changes. `_credential_lock` is still acquired by
  `save_refreshed_token` (for inactive refreshes and force-refresh) and by
  `activate_account_config` (for switches). Its charter is unchanged —
  E2 removes a caller, it does not restructure the lock.

### `backend/services/switcher.py`

- No changes. `perform_switch` still flips the `~/.ccswitch/active`
  pointer inside `_switch_lock`. The next poll cycle picks up the new
  ownership partition automatically. Any brief window between the switch
  and the next poll cycle is harmless — access tokens have multi-minute
  lifetimes, not 15-second ones.

## Frontend

### `frontend/src/ui/accounts.js`

- Card renders a new soft state when the usage entry carries
  `waiting_for_cli === true`:
  - Badge "Waiting for Claude Code" (amber, not red).
  - Sub-text: "Claude Code will refresh this card's token on its next
    request."
  - Inline "Force refresh" button tied to the new endpoint.
- State precedence (first match wins): rate-limited → stale_reason →
  `waiting_for_cli` → healthy. Waiting never clobbers an actual stale
  flag or an active rate-limit banner.

### `frontend/src/api.js`

- Add `forceRefresh(accountId)` wrapping `POST
  /api/accounts/{accountId}/force-refresh`.

### `frontend/src/style.css`

- New `.badge.waiting` variant (amber tint) reusing existing badge layout.

## Tests

New tests in `tests/test_background.py`:

1. `test_active_account_skips_refresh` — near-expiry token on the active
   account is NOT refreshed.
2. `test_inactive_account_refresh_window_20min` — inactive account with a
   token 15 min from expiry IS refreshed.
3. `test_probe_401_on_active_retries_after_keychain_reread_success` —
   first probe 401s, Keychain re-read returns a different token, retry
   succeeds; no stale_reason, no waiting flag.
4. `test_probe_401_on_active_soft_waiting_state` — re-read returns the
   same token; `waiting_for_cli=True`, `stale_reason` stays None.
5. `test_probe_401_on_inactive_still_marks_stale` — inactive path
   unchanged.
6. `test_switch_repartitions_ownership_next_cycle` — after auto-switch
   A→B, next poll cycle refreshes A (now inactive) and does not refresh
   B (now active).

New tests in `tests/test_accounts_router.py`:

7. `test_force_refresh_success` — happy path; broadcasts `usage_updated`.
8. `test_force_refresh_revoked_marks_stale_returns_409` — upstream
   returns 400/401; endpoint sets `stale_reason`, commits, returns 409.
9. `test_force_refresh_upstream_error_returns_502` — upstream returns
   500/503; endpoint does not mutate `stale_reason`, returns 502.

Update the fixture in existing tests that asserted "CCSwitch refreshes
active account" — flip those to assert inactive-account behavior.

## Docs

Add to `CLAUDE.md` under "Key data flow":

> **Refresh ownership.** CCSwitch does not refresh the access token of the
> account `~/.ccswitch/active` points at — Claude Code owns that
> account's refresh lifecycle while it's active. CCSwitch refreshes only
> inactive accounts. On a probe 401 for the active account, CCSwitch
> re-reads the Keychain once and retries; unresolved 401 becomes a soft
> "waiting for CLI" state, never a persisted `stale_reason`. The
> `~/.ccswitch/active` pointer file is the authoritative ownership
> boundary; flipping it during a switch transfers refresh responsibility
> to the other side.

Add to `README.md` (short user-facing note in the "How it works" area):

> CCSwitch shares credentials with the `claude` CLI. To avoid racing the
> CLI on OAuth refresh, CCSwitch does not refresh tokens for the
> currently-active account — the CLI does that on its next API call. If
> the dashboard shows "Waiting for Claude Code" on the active card, run
> any `claude` command and the card recovers on the next poll. If you
> keep CCSwitch open without actively using Claude Code, click **Force
> refresh** to refresh the active account's token manually.

## Rejected alternatives

### E1 alone — widen refresh window only

Reduces race frequency but does not eliminate it. For a 24/7 multi-session
user the estimated residual rate is ~0.2 races/month (one lockout every
5 months). Does not clear the "no recurring problems" bar. E1's
retry-on-401 + wider-window pieces are kept inside E2 as defense-in-depth
for the inactive branch.

### E3 — ccflare-style local HTTP proxy

Dead on three independent grounds:

1. **Anthropic's February 20, 2026 policy update** (at
   `code.claude.com/docs/en/legal-and-compliance`) explicitly prohibits
   routing subscription OAuth traffic through third-party products. Full
   enforcement began April 4, 2026. The Transparency Hub (January 29,
   2026) reports 1.45 M accounts banned in H2 2025 with a 3.3 % appeal
   success rate. ccflare's own maintainers removed round-robin /
   least-requests / weighted strategies from their codebase with the
   verbatim justification "as they could trigger account bans"
   (`github.com/snipeship/ccflare/blob/main/docs/architecture.md`).
2. **Starlette's `BaseHTTPMiddleware` buffers streaming responses.**
   `backend/auth.py:26` subclasses it. SSE pass-through from Anthropic
   through that middleware chain would buffer-then-forward, breaking
   streaming entirely (Starlette Issue #1012, Discussion #1729).
   Rewriting auth as pure ASGI middleware is a prerequisite even to
   attempt E3.
3. **Prompt cache is per-account.** Routing within a conversation across
   accounts destroys cache hits; sticky routing (ccflare's only surviving
   strategy) is incompatible with CCSwitch's primary feature
   (auto-switching before the 5-hour window closes).

### E4 — `claude setup-token` + `CLAUDE_CODE_OAUTH_TOKEN`

Initially attractive (advertised 1-year lifetime, appears refresh-free).
Killed by three independent findings:

1. **Server-side blocked for non-interactive use since February 2026.**
   Multiple confirmed reports on Claude Code Issue #24317 (kokuyouwind
   March 14, ssj5037 April 9) show Anthropic blocks
   `CLAUDE_CODE_OAUTH_TOKEN` when used outside the official Claude Code
   interactive flow. Setup-token falls under the same ToS ban as today's
   refresh tokens.
2. **Not actually refresh-free.** Field reports show the "1 year" is the
   refresh-token lifetime; the underlying bearer still rotates every
   ~8 hours through the same `/v1/oauth/token` endpoint. The race would
   migrate, not disappear.
3. **Issue #19274 (macOS-only).** `claude setup-token` does not persist
   the minted token to Keychain on macOS. CCSwitch is macOS-only. The
   mint flow would require scraping stdout from a tmux pane — strictly
   worse UX than the existing login flow.

## Out of scope

- Inter-CLI races (two Claude Code processes racing each other on the
  same account). Handled by Anthropic's v2.1.81 `cli.js` file lock, not
  by CCSwitch.
- Changes to the add-account and re-login flows. The new invariant is
  orthogonal; login is unchanged.
- Changes to `maybe_auto_switch`. The auto-switch decision is unchanged
  — "waiting for CLI" is intentionally NOT stale, so the stale-fast-path
  does not fire on transient active-account 401s.
- Alembic migration. No schema change.
- Anthropic ToS exposure for CCSwitch itself. The February 2026 policy
  language ("route requests through Free/Pro/Max plan credentials on
  behalf of their users") could be read as covering CCSwitch's own
  `/v1/messages` probe. This concern applies identically to the current
  code, E1, E2, and E4, so it does not change the refresh-race decision.
  Flagged here so the user is aware; addressing it is out of scope for
  this fix.
