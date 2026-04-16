# OAuth Refresh `client_id` Fix — Implementation Plan

**Date:** 2026-04-16
**Severity:** CRITICAL — healthy accounts getting marked stale on every swap attempt
**Root cause:** `refresh_access_token` POST missing `client_id` → Anthropic returns 400 `invalid_request_error` → our classifier wrongly marks it terminal → stale_reason persisted to DB.

## Proof

Live test matrix against `platform.claude.com/v1/oauth/token` with a known-healthy refresh_token:

| Variant | Status | Notes |
|---|---|---|
| No headers, no client_id (current) | 400 invalid_request_error | BUG |
| Any header combo, no client_id | 400 invalid_request_error | Headers irrelevant |
| Form-encoded, no client_id | 400 invalid_request_error | Encoding irrelevant |
| JSON + `client_id: "9d1c250a-e61b-44d9-88ed-5944d1962f5e"` | **200** + fresh tokens | FIX |
| Replay consumed rt | 400 invalid_grant | Genuine dead → RFC flat |

Every OSS Claude-multi-account tool surveyed (ccflare, ccNexus, Kaku, Hermes, wanikua, etc.) sends this exact client_id.

## Six ordered steps

Each commits separately → each reversible independently.

### Step 1 — Add `client_id` to refresh body (TDD)

**File:** `backend/services/anthropic_api.py`

1. Write failing test: MockTransport captures outbound body, asserts `client_id == "9d1c250a-e61b-44d9-88ed-5944d1962f5e"`
2. Add constant `_CLAUDE_CODE_CLIENT_ID`
3. Include in `refresh_access_token` POST body
4. Run test → passes

**Commit:** `fix(oauth): include client_id in refresh token POST body`

### Step 2 — Reclassify `invalid_request_error` as transient

**File:** `backend/services/anthropic_api.py`

1. Update 3 existing tests (flip expected outcome):
   - `test_parse_oauth_error_400_anthropic_invalid_request_is_terminal` → rename to `_is_transient`, flip assertion
   - `test_parse_oauth_error_rfc_and_anthropic_both_still_work` → change Anthropic half to `invalid_grant`
   - `test_refresh_vault_token_terminal_400_raises` → update docstring
2. Remove `"invalid_request_error"` from `_TERMINAL_OAUTH_ERROR_CODES` frozenset
3. Rewrite comment block explaining why

**Commit:** `fix(oauth): reclassify invalid_request_error as transient (was false positive)`

### Step 3 — Document classifier trust contract

**Files:** `backend/services/account_service.py`, `backend/services/switcher.py`

Append to docstrings of `revalidate_account` + `perform_switch`: note that persisting stale_reason depends on classifier correctness + reference this fix doc.

**Commit:** `docs(oauth): document classifier trust contract in swap/revalidate`

### Step 4 — One-time DB cleanup script

**File:** `scripts/cleanup_phantom_stale_2026_04_16.py` (new)

For each account with stale_reason matching "rejected|revoked|re-login required":
1. Load vault refresh_token
2. Call fixed `refresh_access_token`
3. On 200 → persist new tokens, clear stale_reason
4. On `invalid_grant` → genuinely dead, leave alone
5. On anything else → skip + log

Support `--dry-run` flag.

**Manual:** `leusnazarii.biz@gmail.com` needs re-login (consumed RT during diagnosis).

**Commit:** `chore(db): one-shot cleanup script for phantom stale accounts`

### Step 5 — Regression tests

**Files:** `tests/test_anthropic_api.py`, `tests/test_switcher.py`

1. `test_parse_oauth_error_invalid_request_error_is_transient` (both shapes)
2. `test_refresh_access_token_includes_client_id_in_body` (already in Step 1)
3. `test_perform_switch_does_not_mark_stale_on_invalid_request_error` (end-to-end)

**Commit:** `test(oauth): regression guards for client_id fix`

### Step 6 — Validation gate

1. `uv run pytest tests/ -q` → all pass
2. Restart server
3. Live swap test on healthy accounts → success
4. Verify no phantom stale on next poll cycle
5. Run cleanup script in dry-run, review, then apply

## Canonical constants

- Claude Code OAuth client_id: `9d1c250a-e61b-44d9-88ed-5944d1962f5e`
- Refresh endpoint: `https://platform.claude.com/v1/oauth/token`

## Post-fix verification (what success looks like)

- `POST /api/accounts/{id}/switch` on a healthy vault account → HTTP 200
- `GET /api/accounts` → previously-stale accounts self-heal after next poll cycle
- No new phantom-stale events logged

## Blast radius reminder

4 DB-writing paths: swap step 0.5, revalidate, poll reactive 401, poll proactive escalation.
Poll paths (3, 4) self-heal. Swap + revalidate do NOT self-heal → need cleanup.
