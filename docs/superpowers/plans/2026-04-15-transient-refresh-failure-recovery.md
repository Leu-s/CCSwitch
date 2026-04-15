# Transient Refresh Failure Recovery — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop marking CCSwitch vault accounts as "re-login required" for transient OAuth 400 responses from Anthropic's refresh endpoint, and add a one-click "Revalidate" recovery path for accounts already stuck in phantom-stale state.

**Architecture:** Three-layer change.  (1) Parse Anthropic's 400 response body and keep only OAuth2 `error` codes from RFC 6749 §5.2 that indicate a token or client problem (`invalid_grant`, `invalid_client`, `unauthorized_client`, `unsupported_grant_type`, `invalid_scope`) as terminal; every other code (including unparseable body) becomes transient.  401 is classified terminal only when the body also carries one of those codes; bare 401 is transient (could be WAF / edge-proxy challenge).  (2) Per-account exponential refresh-backoff (120 s → 3600 s, parallel to the existing 429 mechanism) with escalation to terminal after either (a) N=5 consecutive transient failures OR (b) ≥ 24 h since the first transient failure — whichever comes first.  (3) New `POST /api/accounts/{id}/revalidate` endpoint + UI button for on-demand recovery of phantom-stale **vault** accounts only.  Active-account revalidate is refused with HTTP 409 — CLI owns the active refresh lifecycle; the user switches to another account first, then revalidates.  Concurrent revalidate calls are serialised per-email via an in-memory async Lock.  Revalidate failure path returns HTTP 409 (not 200+success=false) with the updated `stale_reason` in the body so standard HTTP-error middleware works.  No DB migration: retry state lives in module-level in-memory dicts like the existing 429 backoff.

**Tech Stack:** Python 3.14 + FastAPI + SQLAlchemy async + httpx + vanilla JS frontend + pytest + pytest-asyncio.

---

## Files touched

| Role | File | Change |
|---|---|---|
| New helper | `backend/services/anthropic_api.py` | `parse_oauth_error()` + refactor `refresh_access_token` to surface parsed error on `HTTPStatusError` |
| Core logic | `backend/background.py` | Branch on error code; transient 400 path + refresh-backoff dicts + escalation |
| Orchestrator | `backend/services/account_service.py` | `revalidate_account(email)` on-demand recovery helper |
| Router | `backend/routers/accounts.py` | `POST /{id}/revalidate` endpoint |
| Schemas | `backend/schemas.py` | `RevalidateResult` response schema |
| Frontend state | `frontend/src/ui/accounts.js` | Conditional Revalidate button + event wiring |
| Frontend API | `frontend/src/api.js` | `revalidateAccount(id)` fetch wrapper |
| Frontend main | `frontend/src/main.js` | Listener for `app:revalidate-account` event |
| Tests | `tests/test_anthropic_api.py` | `parse_oauth_error` unit tests |
| Tests | `tests/test_background.py` | Transient 400 path + retry escalation |
| Tests | `tests/test_accounts_router.py` | Revalidate endpoint integration tests |
| Docs | `CLAUDE.md` | Update Key data flow step 3 bullet |
| Docs | `docs/superpowers/specs/2026-04-15-vault-swap-architecture.md` | New §9.10 on transient vs terminal refresh failures |
| Docs | `README.md` | Troubleshooting entry for Revalidate button |

---

## Milestone 1 — OAuth error parsing, 400 vs 401 disambiguation

**Files:**
- Modify: `backend/services/anthropic_api.py:89-108`
- Test: `tests/test_anthropic_api.py`

### Task 1.1 — Add OAuth error helper with failing test

- [ ] **Step 1: Write failing test at `tests/test_anthropic_api.py`**

Append to the existing file (do NOT replace):

```python
# ── OAuth error parser ────────────────────────────────────────────────

from unittest.mock import MagicMock

from backend.services.anthropic_api import parse_oauth_error, OAuthErrorKind


def _make_http_status_error(status: int, json_body=None, text_body=""):
    """Build an httpx.HTTPStatusError with a realistic response object."""
    import httpx
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    if json_body is not None:
        resp.json = MagicMock(return_value=json_body)
    else:
        resp.json = MagicMock(side_effect=ValueError("no json"))
    resp.text = text_body
    req = httpx.Request("POST", "https://example.test/oauth2/token")
    return httpx.HTTPStatusError("status", request=req, response=resp)


def test_parse_oauth_error_401_with_invalid_grant_is_terminal():
    err = _make_http_status_error(401, {"error": "invalid_grant"})
    assert parse_oauth_error(err) == OAuthErrorKind.TERMINAL_REVOKED


def test_parse_oauth_error_401_with_invalid_client_is_terminal():
    err = _make_http_status_error(401, {"error": "invalid_client"})
    assert parse_oauth_error(err) == OAuthErrorKind.TERMINAL_REVOKED


def test_parse_oauth_error_bare_401_without_body_is_transient():
    """Bare 401 can be an edge-proxy WAF challenge (Cloudflare etc.) — retry."""
    err = _make_http_status_error(401, None, "Unauthorized")
    assert parse_oauth_error(err) == OAuthErrorKind.TRANSIENT


def test_parse_oauth_error_401_with_unknown_body_is_transient():
    err = _make_http_status_error(401, {"error": "some_edge_proxy_code"})
    assert parse_oauth_error(err) == OAuthErrorKind.TRANSIENT


def test_parse_oauth_error_400_invalid_grant_is_terminal_rejected():
    err = _make_http_status_error(400, {"error": "invalid_grant"})
    assert parse_oauth_error(err) == OAuthErrorKind.TERMINAL_REJECTED


def test_parse_oauth_error_400_invalid_client_is_terminal_rejected():
    err = _make_http_status_error(400, {"error": "invalid_client"})
    assert parse_oauth_error(err) == OAuthErrorKind.TERMINAL_REJECTED


def test_parse_oauth_error_400_unauthorized_client_is_terminal():
    """Per RFC 6749 §5.2, client is not authorised for this grant type —
    not a self-healing condition, so treat as terminal."""
    err = _make_http_status_error(400, {"error": "unauthorized_client"})
    assert parse_oauth_error(err) == OAuthErrorKind.TERMINAL_REJECTED


def test_parse_oauth_error_400_unsupported_grant_type_is_terminal():
    err = _make_http_status_error(400, {"error": "unsupported_grant_type"})
    assert parse_oauth_error(err) == OAuthErrorKind.TERMINAL_REJECTED


def test_parse_oauth_error_400_invalid_scope_is_terminal():
    err = _make_http_status_error(400, {"error": "invalid_scope"})
    assert parse_oauth_error(err) == OAuthErrorKind.TERMINAL_REJECTED


def test_parse_oauth_error_400_invalid_request_is_transient():
    err = _make_http_status_error(400, {"error": "invalid_request"})
    assert parse_oauth_error(err) == OAuthErrorKind.TRANSIENT


def test_parse_oauth_error_400_unknown_code_is_transient():
    err = _make_http_status_error(400, {"error": "rate_limited_on_refresh"})
    assert parse_oauth_error(err) == OAuthErrorKind.TRANSIENT


def test_parse_oauth_error_400_without_body_is_transient():
    err = _make_http_status_error(400, None, "Bad Request")
    assert parse_oauth_error(err) == OAuthErrorKind.TRANSIENT


def test_parse_oauth_error_400_with_non_dict_body_is_transient():
    err = _make_http_status_error(400, ["not a dict"])
    assert parse_oauth_error(err) == OAuthErrorKind.TRANSIENT


def test_parse_oauth_error_400_with_json_decode_error_is_transient():
    """Newer httpx versions raise json.JSONDecodeError not ValueError — catch both."""
    import json
    err = _make_http_status_error(400, None, "not json either")
    # Replace the side_effect with JSONDecodeError specifically.
    err.response.json = MagicMock(
        side_effect=json.JSONDecodeError("bad", "body", 0)
    )
    assert parse_oauth_error(err) == OAuthErrorKind.TRANSIENT


def test_parse_oauth_error_429_is_transient():
    err = _make_http_status_error(429, {"error": "rate_limited"})
    assert parse_oauth_error(err) == OAuthErrorKind.TRANSIENT


def test_parse_oauth_error_5xx_is_transient():
    err = _make_http_status_error(503, None, "Service Unavailable")
    assert parse_oauth_error(err) == OAuthErrorKind.TRANSIENT
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run python -m pytest tests/test_anthropic_api.py -q -k parse_oauth_error
```

Expected: FAIL with `ImportError: cannot import name 'parse_oauth_error'`

- [ ] **Step 3: Implement `parse_oauth_error` + `OAuthErrorKind` in `backend/services/anthropic_api.py`**

Insert at the top of the file AFTER existing imports and BEFORE the existing functions:

```python
import enum
from typing import Any

import httpx

# OAuth2 terminal error codes per RFC 6749 §5.2.  Each one names a condition
# that is NOT self-healing within hours — the refresh_token is dead, the
# client is mis-registered, or the scope is wrong — so the correct response
# is to demand a re-login.  Everything else (400 `invalid_request`, 400 with
# no body, 429, 5xx, network) is transient and eligible for exponential
# retry.
#
# `invalid_grant`          — refresh_token expired, revoked, or reused.
# `invalid_client`         — client authentication failed (we are mis-
#                            registered with the authz server).
# `unauthorized_client`    — client not allowed to use this grant type.
# `unsupported_grant_type` — authz server doesn't understand this grant.
# `invalid_scope`          — scope requested is invalid / out of range.
_TERMINAL_OAUTH_ERROR_CODES = frozenset({
    "invalid_grant",
    "invalid_client",
    "unauthorized_client",
    "unsupported_grant_type",
    "invalid_scope",
})


class OAuthErrorKind(enum.Enum):
    """Classification of a failed refresh request.

    ``TERMINAL_REVOKED``   — refresh token explicitly rejected by the authz
                             server; user must re-login.
    ``TERMINAL_REJECTED``  — client or request config problem the server
                             considers unrecoverable; user must re-login.
    ``TRANSIENT``          — every other failure (edge-proxy WAF challenges,
                             500-series, 429, network, 400 with non-terminal
                             error code, no parseable body).  Retry with
                             exponential backoff.
    """

    TERMINAL_REVOKED = "terminal_revoked"
    TERMINAL_REJECTED = "terminal_rejected"
    TRANSIENT = "transient"


def _extract_oauth_error_code(resp: httpx.Response) -> str | None:
    """Return the OAuth ``error`` field from a response body, or None if it
    is not a parseable OAuth2 error response."""
    try:
        body: Any = resp.json()
    except Exception:
        # httpx versions differ: older raise ValueError, newer raise
        # json.JSONDecodeError (which is a ValueError subclass but also
        // surfaces as-is).  Either way — no parseable body.
        return None
    if not isinstance(body, dict):
        return None
    code = body.get("error")
    return code if isinstance(code, str) else None


def parse_oauth_error(err: httpx.HTTPStatusError) -> OAuthErrorKind:
    """Classify a refresh-endpoint HTTP error into terminal/transient.

    Rule:
    * 401 or 400 whose body carries an ``error`` code in
      ``_TERMINAL_OAUTH_ERROR_CODES`` → TERMINAL_REVOKED (401) or
      TERMINAL_REJECTED (400).  These are RFC 6749 §5.2 terminal conditions.
    * Everything else → TRANSIENT.  Includes bare 401 without a body
      (frequently a Cloudflare / edge-proxy challenge that self-heals),
      bare 400, 400 with non-terminal code, 429, 5xx, malformed body.

    This is deliberately conservative: false-positive transient is a 2-minute
    backoff and a retry; false-positive terminal is a phantom-stale account
    the user cannot clear without the full re-login tmux dance.  The
    motivating production bug was the latter.
    """
    status = err.response.status_code
    code = _extract_oauth_error_code(err.response) if status in (400, 401) else None
    if code not in _TERMINAL_OAUTH_ERROR_CODES:
        return OAuthErrorKind.TRANSIENT
    return (
        OAuthErrorKind.TERMINAL_REVOKED
        if status == 401
        else OAuthErrorKind.TERMINAL_REJECTED
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run python -m pytest tests/test_anthropic_api.py -q -k parse_oauth_error
```

Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add backend/services/anthropic_api.py tests/test_anthropic_api.py
git commit -m "$(cat <<'EOF'
feat(oauth): parse refresh-endpoint error body — only invalid_grant/invalid_client are terminal

Adds OAuthErrorKind + parse_oauth_error(httpx.HTTPStatusError) that
classifies a refresh-endpoint failure into TERMINAL_REVOKED (401),
TERMINAL_REJECTED (400 with invalid_grant or invalid_client) or
TRANSIENT (everything else).  Callers in background.py will branch
on this instead of the pre-existing "any 400/401 → terminal" lump.
The current lump is why accounts that hit a 5-hour usage limit or
race the single-use refresh-token rotation end up stuck in phantom
"re-login required" state with perfectly valid tokens.

10 unit tests cover: 401 with/without body, 400 invalid_grant,
invalid_client, invalid_request, unknown code, no body, non-dict
body, 429, 5xx.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Milestone 2 — Branch on error kind in the poll loop, add transient backoff

**Files:**
- Modify: `backend/background.py:26-34` (backoff dicts), `:151-165` (error handler)
- Test: `tests/test_background.py`

### Task 2.1 — Add refresh-backoff dicts + `_TRANSIENT_400_ESCALATE_AFTER` constant

- [ ] **Step 1: Modify `backend/background.py` lines 26-34 (existing 429 backoff block)**

Replace the existing block:

```python
# ── Per-account 429 backoff state ────────────────────────────────────────────
# Maps email → monotonic deadline (seconds); if time.monotonic() < deadline,
# skip the probe and return stale cached data instead.
_backoff_until: dict[str, float] = {}
# Maps email → consecutive 429 count for exponential doubling.
_backoff_count: dict[str, int] = {}

_BACKOFF_INITIAL = settings.rate_limit_backoff_initial
_BACKOFF_MAX = settings.rate_limit_backoff_max
```

With:

```python
# ── Per-account 429 backoff state (probe path) ───────────────────────────────
# Maps email → monotonic deadline (seconds); if time.monotonic() < deadline,
# skip the probe and return stale cached data instead.
_backoff_until: dict[str, float] = {}
# Maps email → consecutive 429 count for exponential doubling.
_backoff_count: dict[str, int] = {}

# ── Per-account transient-refresh backoff state (refresh path) ───────────────
# Parallel to the 429 backoff above, but for Anthropic's refresh endpoint
# returning TRANSIENT classifications (400 with non-terminal error codes, bare
# 401, 429, 5xx).  Keeps the stale_reason marker off the account until we
# have tried enough times to be confident the refresh_token is genuinely dead.
# State resets on server restart — intentional; the first post-restart poll
# re-enters the escalation ladder from zero.
_refresh_backoff_until: dict[str, float] = {}
_refresh_backoff_count: dict[str, int] = {}
# When the FIRST transient failure for this email was observed.  Used as a
# wall-clock ceiling for escalation so rapid-fire retries can't prematurely
# flip an account stale AND a long-running hung state can't permanently
# avoid escalation via periodic counter resets.
_refresh_backoff_first_failure_at: dict[str, float] = {}

_BACKOFF_INITIAL = settings.rate_limit_backoff_initial
_BACKOFF_MAX = settings.rate_limit_backoff_max

# Consecutive-failure count at which we escalate to terminal stale_reason.
# Under exponential backoff the actual poll-cycle wall-clock to reach N=5
# is ~63 min in active mode (15 s cadence) and longer in idle mode (300 s
# cadence, each skipped poll wastes 300 s).
_TRANSIENT_REFRESH_ESCALATE_AFTER = 5
# Second independent escalation trigger: if the first transient failure is
# older than this many seconds, escalate regardless of the current counter
# value.  Protects against counter-reset loops where Anthropic intermittently
# succeeds (resetting the count) but fails overall for a day+.
_TRANSIENT_REFRESH_ESCALATE_AFTER_SECONDS = 24 * 3600
```

- [ ] **Step 2: Modify the error handler at `backend/background.py:151-165`**

Replace the existing block:

```python
            except httpx.HTTPStatusError as refresh_http_err:
                status = refresh_http_err.response.status_code
                # Anthropic returns 400 when the refresh_token has been
                # rotated or invalidated and 401 when it is explicitly
                # revoked.  Both are terminal — the follow-up probe would
                # just fail with 401 — so we raise a marker that skips the
                # probe and preserves the precise stale_reason.
                if status in (400, 401):
                    reason_detail = "revoked" if status == 401 else "rejected (400)"
                    logger.error(
                        "Refresh token %s for %s — re-login required.",
                        reason_detail, account.email,
                    )
                    new_stale_reason = f"Refresh token {reason_detail} — re-login required"
                    raise _RefreshTerminal()
```

With:

```python
            except httpx.HTTPStatusError as refresh_http_err:
                kind = anthropic_api.parse_oauth_error(refresh_http_err)
                status = refresh_http_err.response.status_code
                if kind is anthropic_api.OAuthErrorKind.TERMINAL_REVOKED:
                    logger.error(
                        "Refresh token revoked for %s (HTTP 401 + terminal body) — re-login required.",
                        account.email,
                    )
                    new_stale_reason = "Refresh token revoked — re-login required"
                    _refresh_backoff_until.pop(account.email, None)
                    _refresh_backoff_count.pop(account.email, None)
                    _refresh_backoff_first_failure_at.pop(account.email, None)
                    raise _RefreshTerminal()
                if kind is anthropic_api.OAuthErrorKind.TERMINAL_REJECTED:
                    logger.error(
                        "Refresh token rejected for %s (HTTP 400 + terminal OAuth code) — re-login required.",
                        account.email,
                    )
                    new_stale_reason = "Refresh token rejected — re-login required"
                    _refresh_backoff_until.pop(account.email, None)
                    _refresh_backoff_count.pop(account.email, None)
                    _refresh_backoff_first_failure_at.pop(account.email, None)
                    raise _RefreshTerminal()
                # TRANSIENT: 400 with non-terminal error, bare 401, 429, 5xx, network.
                now = time.monotonic()
                _refresh_backoff_first_failure_at.setdefault(account.email, now)
                first_failure_at = _refresh_backoff_first_failure_at[account.email]
                count = _refresh_backoff_count.get(account.email, 0) + 1
                _refresh_backoff_count[account.email] = count
                backoff_seconds = min(
                    _BACKOFF_INITIAL * (2 ** (count - 1)), _BACKOFF_MAX
                )
                _refresh_backoff_until[account.email] = now + backoff_seconds
                wall_age = now - first_failure_at
                escalate = (
                    count >= _TRANSIENT_REFRESH_ESCALATE_AFTER
                    or wall_age >= _TRANSIENT_REFRESH_ESCALATE_AFTER_SECONDS
                )
                if escalate:
                    logger.error(
                        "Refresh transient escalation for %s — count=%d wall=%ds last HTTP %d.",
                        account.email, count, int(wall_age), status,
                    )
                    new_stale_reason = (
                        f"Refresh endpoint transient failure ×{count} "
                        f"over {int(wall_age // 60)} min (last HTTP {status}) — "
                        f"re-login required"
                    )
                    _refresh_backoff_until.pop(account.email, None)
                    _refresh_backoff_count.pop(account.email, None)
                    _refresh_backoff_first_failure_at.pop(account.email, None)
                    raise _RefreshTerminal()
                logger.warning(
                    "Refresh transient for %s (HTTP %d, offense #%d, wall %ds) — "
                    "backing off %ds; will retry (no stale_reason yet).",
                    account.email, status, count, int(wall_age), backoff_seconds,
                )
                # Fall through to return cached usage — no stale_reason write.
                raise
```

- [ ] **Step 3: Add the refresh-backoff skip gate — modify `_process_single_account` around the refresh call at `backend/background.py:127`**

Find the line `if not account.stale_reason and not is_active:` and replace its condition:

```python
        if (
            not account.stale_reason
            and not is_active
            and _refresh_backoff_until.get(account.email, 0.0) <= time.monotonic()
        ):
```

- [ ] **Step 4: Clear refresh backoff on successful refresh — insert after the successful `save_refreshed_vault_token` call in the same block**

After the line `token = new_token` and `logger.info("Refreshed vault token for %s", account.email)` add:

```python
                            _refresh_backoff_until.pop(account.email, None)
                            _refresh_backoff_count.pop(account.email, None)
                            _refresh_backoff_first_failure_at.pop(account.email, None)
```

- [ ] **Step 4.5: Extend the autouse cleanup fixture so the new dicts are wiped between tests**

In `tests/test_background.py`, find the existing fixture `_wipe_cache_between_tests` (around line 30).  It currently clears `bg._backoff_until` + `bg._backoff_count`.  Extend it to also clear the three new dicts:

```python
@pytest.fixture(autouse=True)
async def _wipe_cache_between_tests():
    bg._backoff_until.clear()
    bg._backoff_count.clear()
    bg._refresh_backoff_until.clear()
    bg._refresh_backoff_count.clear()
    bg._refresh_backoff_first_failure_at.clear()
    await _cache._usage_cache.clear()   # or whatever the existing line is
    yield
    bg._backoff_until.clear()
    bg._backoff_count.clear()
    bg._refresh_backoff_until.clear()
    bg._refresh_backoff_count.clear()
    bg._refresh_backoff_first_failure_at.clear()
    await _cache._usage_cache.clear()
```

Without this extension, a test that fails mid-assertion leaves counter state behind and the next test sees phantom-backoff.  Preserve the EXACT shape of the pre-existing cache-clear call (whatever it is — copy from the current file verbatim); only the three refresh-backoff lines are new.

- [ ] **Step 5: Write failing tests at `tests/test_background.py`**

Append (do NOT replace) the following block.  The helpers `_make_account`, `_fresh_creds`, `_http_error` already exist — reuse them directly.

```python
# ── Transient refresh-failure handling ────────────────────────────────────

@pytest.mark.asyncio
async def test_refresh_400_invalid_grant_sets_terminal_stale(monkeypatch):
    """400 with error=invalid_grant → terminal stale_reason, no backoff counters."""
    from backend.services.anthropic_api import OAuthErrorKind  # noqa: F401

    bg._refresh_backoff_until.clear()
    bg._refresh_backoff_count.clear()
    near_expiry_ms = int(time.time() * 1000) + 5 * 60 * 1000
    account = _make_account(email="vault@example.com")

    monkeypatch.setattr(
        ac, "read_credentials_for_email",
        lambda email, active_email=None: _fresh_creds(expires_at_ms=near_expiry_ms),
    )

    async def fake_refresh(refresh_token):
        raise _http_error(400, json_body={"error": "invalid_grant"})

    monkeypatch.setattr(anthropic_api, "refresh_access_token", fake_refresh)

    async def fake_probe(token):
        return {}
    monkeypatch.setattr(anthropic_api, "probe_usage", fake_probe)

    _, stale = await bg._process_single_account(account, "other@example.com")
    assert stale == "Refresh token rejected — re-login required"
    assert "vault@example.com" not in bg._refresh_backoff_count
    assert "vault@example.com" not in bg._refresh_backoff_until


@pytest.mark.asyncio
async def test_refresh_400_invalid_request_is_transient_no_stale(monkeypatch):
    """400 with non-terminal error code → no stale_reason, backoff counter = 1."""
    bg._refresh_backoff_until.clear()
    bg._refresh_backoff_count.clear()
    near_expiry_ms = int(time.time() * 1000) + 5 * 60 * 1000
    account = _make_account(email="vault@example.com")

    monkeypatch.setattr(
        ac, "read_credentials_for_email",
        lambda email, active_email=None: _fresh_creds(expires_at_ms=near_expiry_ms),
    )

    async def fake_refresh(refresh_token):
        raise _http_error(400, json_body={"error": "invalid_request"})

    monkeypatch.setattr(anthropic_api, "refresh_access_token", fake_refresh)

    async def fake_probe(token):
        return {}
    monkeypatch.setattr(anthropic_api, "probe_usage", fake_probe)

    _, stale = await bg._process_single_account(account, "other@example.com")
    assert stale is None
    assert bg._refresh_backoff_count["vault@example.com"] == 1
    assert "vault@example.com" in bg._refresh_backoff_until


@pytest.mark.asyncio
async def test_refresh_transient_escalates_after_n_failures(monkeypatch):
    """After `_TRANSIENT_REFRESH_ESCALATE_AFTER` consecutive transients, mark stale."""
    # Pre-load the counter to one below the escalation threshold, and a
    # recent first-failure timestamp so the wall-clock ceiling does NOT fire.
    bg._refresh_backoff_count["vault@example.com"] = bg._TRANSIENT_REFRESH_ESCALATE_AFTER - 1
    bg._refresh_backoff_first_failure_at["vault@example.com"] = time.monotonic() - 10

    near_expiry_ms = int(time.time() * 1000) + 5 * 60 * 1000
    account = _make_account(email="vault@example.com")

    monkeypatch.setattr(
        ac, "read_credentials_for_email",
        lambda email, active_email=None: _fresh_creds(expires_at_ms=near_expiry_ms),
    )

    async def fake_refresh(refresh_token):
        raise _http_error(400, json_body={"error": "invalid_request"})

    monkeypatch.setattr(anthropic_api, "refresh_access_token", fake_refresh)

    async def fake_probe(token):
        return {}
    monkeypatch.setattr(anthropic_api, "probe_usage", fake_probe)

    _, stale = await bg._process_single_account(account, "other@example.com")
    assert stale is not None
    assert "transient failure" in stale
    assert f"×{bg._TRANSIENT_REFRESH_ESCALATE_AFTER}" in stale
    # Counters cleared on escalation.
    assert "vault@example.com" not in bg._refresh_backoff_count
    assert "vault@example.com" not in bg._refresh_backoff_first_failure_at


@pytest.mark.asyncio
async def test_refresh_transient_escalates_after_wall_clock_ceiling(monkeypatch):
    """If the first transient was > 24 h ago, escalate regardless of count.

    Protects against counter-reset loops (Anthropic intermittently succeeds
    resetting the count; feature still broken for the account in net).
    """
    # Count well below threshold, but first-failure timestamp older than the
    # 24 h ceiling — escalation must fire on this attempt.
    bg._refresh_backoff_count["vault@example.com"] = 2
    bg._refresh_backoff_first_failure_at["vault@example.com"] = (
        time.monotonic() - (bg._TRANSIENT_REFRESH_ESCALATE_AFTER_SECONDS + 60)
    )

    near_expiry_ms = int(time.time() * 1000) + 5 * 60 * 1000
    account = _make_account(email="vault@example.com")

    monkeypatch.setattr(
        ac, "read_credentials_for_email",
        lambda email, active_email=None: _fresh_creds(expires_at_ms=near_expiry_ms),
    )

    async def fake_refresh(refresh_token):
        raise _http_error(400, json_body={"error": "invalid_request"})

    monkeypatch.setattr(anthropic_api, "refresh_access_token", fake_refresh)

    async def fake_probe(token):
        return {}
    monkeypatch.setattr(anthropic_api, "probe_usage", fake_probe)

    _, stale = await bg._process_single_account(account, "other@example.com")
    assert stale is not None
    assert "transient failure" in stale
    assert "vault@example.com" not in bg._refresh_backoff_first_failure_at


@pytest.mark.asyncio
async def test_refresh_backoff_skips_retry_within_deadline(monkeypatch):
    """While refresh-backoff deadline is in the future, skip the refresh attempt."""
    bg._refresh_backoff_until.clear()
    bg._refresh_backoff_count.clear()
    bg._refresh_backoff_until["vault@example.com"] = time.monotonic() + 60.0
    bg._refresh_backoff_count["vault@example.com"] = 1

    near_expiry_ms = int(time.time() * 1000) + 5 * 60 * 1000
    account = _make_account(email="vault@example.com")

    monkeypatch.setattr(
        ac, "read_credentials_for_email",
        lambda email, active_email=None: _fresh_creds(expires_at_ms=near_expiry_ms),
    )

    refresh_calls = []
    async def fake_refresh(refresh_token):
        refresh_calls.append(refresh_token)
        return {"access_token": "new", "expires_in": 3600}

    monkeypatch.setattr(anthropic_api, "refresh_access_token", fake_refresh)

    async def fake_probe(token):
        return {}
    monkeypatch.setattr(anthropic_api, "probe_usage", fake_probe)

    _, stale = await bg._process_single_account(account, "other@example.com")
    assert stale is None
    assert refresh_calls == []  # refresh was skipped


@pytest.mark.asyncio
async def test_refresh_success_clears_backoff_counters(monkeypatch):
    bg._refresh_backoff_until.clear()
    bg._refresh_backoff_count.clear()
    bg._refresh_backoff_count["vault@example.com"] = 3
    # Deadline is in the past — no skip.
    bg._refresh_backoff_until["vault@example.com"] = time.monotonic() - 1.0

    near_expiry_ms = int(time.time() * 1000) + 5 * 60 * 1000
    account = _make_account(email="vault@example.com")

    monkeypatch.setattr(
        ac, "read_credentials_for_email",
        lambda email, active_email=None: _fresh_creds(expires_at_ms=near_expiry_ms),
    )

    async def fake_refresh(refresh_token):
        return {"access_token": "new-access", "expires_in": 3600}
    monkeypatch.setattr(anthropic_api, "refresh_access_token", fake_refresh)

    saved = {}
    def fake_save(email, new_token, new_expires_at_ms, new_refresh):
        saved["email"] = email
        saved["token"] = new_token
    monkeypatch.setattr(cp, "save_refreshed_vault_token", fake_save)

    async def fake_probe(token):
        return {}
    monkeypatch.setattr(anthropic_api, "probe_usage", fake_probe)

    _, stale = await bg._process_single_account(account, "other@example.com")
    assert stale is None
    assert saved["token"] == "new-access"
    assert "vault@example.com" not in bg._refresh_backoff_count
    assert "vault@example.com" not in bg._refresh_backoff_until
```

Update the existing `_http_error` helper in the test file if it does not already accept a `json_body` kwarg.  The current shape (per research) produces a `MagicMock(status_code=status)`; extend to also stamp `response.json`:

```python
def _http_error(status: int, json_body=None):
    import httpx
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    if json_body is not None:
        resp.json = MagicMock(return_value=json_body)
    else:
        resp.json = MagicMock(side_effect=ValueError("no json"))
    resp.text = ""
    req = httpx.Request("POST", "https://api.anthropic.com/oauth2/token")
    return httpx.HTTPStatusError("status", request=req, response=resp)
```

(If the existing helper is different, replace it so all existing tests still pass; the signature must remain compatible.)

- [ ] **Step 6: Verify the existing `test_vault_refresh_401_sets_revoked_stale_reason` still passes**

The string changes from `"Refresh token revoked — re-login required"` (no `(401)`) — assert line 217 in the existing test now reads `== "Refresh token revoked — re-login required"` (check; may need update).

- [ ] **Step 7: Run the full background test file**

```bash
uv run python -m pytest tests/test_background.py -q
```

Expected: all pre-existing tests + 5 new tests pass.

- [ ] **Step 8: Commit**

```bash
git add backend/background.py tests/test_background.py
git commit -m "$(cat <<'EOF'
feat(background): branch on OAuth error kind; transient 400 gets exp backoff, not stale

The pre-existing error handler treated any 400 or 401 from Anthropic's
refresh endpoint as terminal.  That was the root cause of phantom-stale
accounts observed in production — a 5-hour-rate-limited account's
refresh endpoint occasionally returns 400 with a non-invalid_grant
body, and the account would get stuck with "re-login required" forever
despite valid tokens.

After this commit:

  401                                → terminal (revoked)
  400 + error=invalid_grant/client   → terminal (rejected)
  400 + other error OR no body       → TRANSIENT, backed off, retried
  429 / 5xx / network                → TRANSIENT, backed off, retried

Transient failures increment _refresh_backoff_count and set a
monotonic deadline in _refresh_backoff_until.  The next poll cycle
skips the refresh attempt until the deadline elapses.  After
_TRANSIENT_REFRESH_ESCALATE_AFTER=5 consecutive transients (covering
~30 min wall time under exponential backoff), the account is
escalated to stale_reason.  A successful refresh clears both dicts.

5 new tests cover: terminal 400 (invalid_grant), transient 400
(invalid_request) leaves stale_reason None, escalation after N
failures, skip-while-backoff-active, reset on success.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

### After M2: 2-3 parallel validation agents

See "Validation protocol" at the bottom of this plan.

---

## Milestone 3 — `revalidate_account` orchestrator

**Files:**
- Modify: `backend/services/account_service.py` (add new function)
- Test: `tests/test_account_service.py` (or adjacent existing file; determine at implementation time)

### Task 3.1 — Write failing test for `revalidate_account`

- [ ] **Step 1: Append to `tests/test_account_service.py`** (file already exists — verified)

```python
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services import account_service as ac


@pytest.mark.asyncio
async def test_revalidate_account_success_clears_stale(monkeypatch):
    """Successful refresh clears stale_reason and returns success=True."""
    from backend.models import Account

    account = Account(
        id=42,
        email="vault@example.com",
        enabled=True,
        priority=0,
        threshold_pct=90,
        stale_reason="Refresh token rejected — re-login required",
    )

    db = MagicMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()

    monkeypatch.setattr(
        ac.aq, "get_account_by_id",
        AsyncMock(return_value=account),
    )
    monkeypatch.setattr(
        ac, "read_credentials_for_email",
        lambda email, active_email=None: {
            "claudeAiOauth": {
                "accessToken": "old",
                "refreshToken": "rt-old",
                "expiresAt": 0,
            },
            "oauthAccount": {"emailAddress": "vault@example.com"},
            "userID": "u",
        },
    )

    async def fake_refresh(refresh_token):
        assert refresh_token == "rt-old"
        return {"access_token": "new-access", "refresh_token": "rt-new", "expires_in": 3600}

    monkeypatch.setattr(ac.anthropic_api, "refresh_access_token", fake_refresh)

    saved = {}
    def fake_save(email, new_token, new_expires_at_ms, new_refresh):
        saved["email"] = email
        saved["access"] = new_token
        saved["refresh"] = new_refresh
    monkeypatch.setattr(ac.cp, "save_refreshed_vault_token", fake_save)

    monkeypatch.setattr(
        ac, "get_active_email_async",
        AsyncMock(return_value="other@example.com"),
    )

    result = await ac.revalidate_account(42, db)
    assert result["success"] is True
    assert result["stale_reason"] is None
    assert result["active_refused"] is False
    assert account.stale_reason is None
    assert saved["access"] == "new-access"
    assert saved["refresh"] == "rt-new"
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_revalidate_account_refuses_active_account(monkeypatch):
    """Active-account revalidate violates the single-refresher invariant
    (CLI owns the active refresh lifecycle) — must refuse with a clear
    error, leaving stale_reason untouched for the user to switch-then-retry."""
    from backend.models import Account

    account = Account(
        id=42,
        email="active@example.com",
        enabled=True,
        priority=0,
        threshold_pct=90,
        stale_reason="Refresh token rejected — re-login required",
    )

    db = MagicMock()
    db.commit = AsyncMock()

    monkeypatch.setattr(
        ac.aq, "get_account_by_id", AsyncMock(return_value=account),
    )
    monkeypatch.setattr(
        ac, "get_active_email_async",
        AsyncMock(return_value="active@example.com"),
    )
    # The refresh function should NEVER be called.
    refresh_calls = []
    async def fake_refresh(refresh_token):
        refresh_calls.append(refresh_token)
        return {}
    monkeypatch.setattr(ac.anthropic_api, "refresh_access_token", fake_refresh)

    result = await ac.revalidate_account(42, db)
    assert result["success"] is False
    assert result["active_refused"] is True
    assert "active" in result["stale_reason"].lower()
    # Original stale_reason unchanged — we don't overwrite user-facing state
    # for an operation we refused.
    assert account.stale_reason == "Refresh token rejected — re-login required"
    assert refresh_calls == []


@pytest.mark.asyncio
async def test_revalidate_account_concurrent_calls_serialize(monkeypatch):
    """Two simultaneous revalidate calls for the same email must not both
    POST the same single-use refresh_token to Anthropic — the per-email
    asyncio.Lock serialises them."""
    from backend.models import Account

    account = Account(
        id=42,
        email="vault@example.com",
        enabled=True,
        priority=0,
        threshold_pct=90,
        stale_reason="Refresh token rejected — re-login required",
    )

    db = MagicMock()
    db.commit = AsyncMock()

    monkeypatch.setattr(
        ac.aq, "get_account_by_id", AsyncMock(return_value=account),
    )
    monkeypatch.setattr(
        ac, "get_active_email_async",
        AsyncMock(return_value="other@example.com"),
    )

    credentials_by_call = [
        {
            "claudeAiOauth": {
                "accessToken": "a", "refreshToken": "rt-live", "expiresAt": 0,
            },
        },
        {
            "claudeAiOauth": {
                "accessToken": "a", "refreshToken": "rt-new", "expiresAt": 0,
            },
        },
    ]
    call_idx = {"n": 0}

    def fake_read(email, active_email=None):
        i = call_idx["n"]
        call_idx["n"] = min(i + 1, len(credentials_by_call) - 1)
        return credentials_by_call[i]

    monkeypatch.setattr(ac, "read_credentials_for_email", fake_read)

    refresh_calls: list[str] = []
    async def fake_refresh(refresh_token):
        refresh_calls.append(refresh_token)
        # simulate network latency so the concurrent call has time to queue
        await asyncio.sleep(0.05)
        return {
            "access_token": f"at-after-{refresh_token}",
            "refresh_token": f"rt-after-{refresh_token}",
            "expires_in": 3600,
        }
    monkeypatch.setattr(ac.anthropic_api, "refresh_access_token", fake_refresh)

    def fake_save(email, t, exp, r):
        pass
    monkeypatch.setattr(ac.cp, "save_refreshed_vault_token", fake_save)

    # Fire two revalidate calls in parallel.
    results = await asyncio.gather(
        ac.revalidate_account(42, db),
        ac.revalidate_account(42, db),
    )
    # Both succeed (first with rt-live, second with rt-new — because the
    // per-email lock forced them to serialise and the second saw the
    // rotated credentials from the first call).
    assert all(r["success"] is True for r in results)
    assert refresh_calls == ["rt-live", "rt-new"]


@pytest.mark.asyncio
async def test_revalidate_account_genuine_invalid_grant_keeps_stale(monkeypatch):
    """If refresh returns 400 invalid_grant, stale_reason is updated with precise reason."""
    from backend.models import Account

    account = Account(
        id=42,
        email="vault@example.com",
        enabled=True,
        priority=0,
        threshold_pct=90,
        stale_reason="Refresh endpoint transient failure — re-login required",
    )

    db = MagicMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()

    monkeypatch.setattr(
        ac.aq, "get_account_by_id", AsyncMock(return_value=account),
    )
    monkeypatch.setattr(
        ac, "read_credentials_for_email",
        lambda email, active_email=None: {
            "claudeAiOauth": {
                "accessToken": "old", "refreshToken": "rt-dead", "expiresAt": 0,
            },
            "oauthAccount": {"emailAddress": "vault@example.com"},
            "userID": "u",
        },
    )

    import httpx
    req = httpx.Request("POST", "https://api.anthropic.com/oauth2/token")
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 400
    resp.json = MagicMock(return_value={"error": "invalid_grant"})

    async def fake_refresh(refresh_token):
        raise httpx.HTTPStatusError("bad", request=req, response=resp)

    monkeypatch.setattr(ac.anthropic_api, "refresh_access_token", fake_refresh)

    # Active-check runs first — if account is not active, proceed.
    monkeypatch.setattr(
        ac, "get_active_email_async",
        AsyncMock(return_value="other@example.com"),
    )

    result = await ac.revalidate_account(42, db)
    assert result["success"] is False
    assert result["active_refused"] is False
    assert "rejected" in result["stale_reason"].lower()
    assert account.stale_reason == result["stale_reason"]


@pytest.mark.asyncio
async def test_revalidate_account_missing_account_returns_none(monkeypatch):
    db = MagicMock()
    monkeypatch.setattr(
        ac.aq, "get_account_by_id", AsyncMock(return_value=None),
    )
    result = await ac.revalidate_account(999, db)
    assert result is None


@pytest.mark.asyncio
async def test_revalidate_account_missing_refresh_token_returns_error(monkeypatch):
    from backend.models import Account

    account = Account(
        id=42,
        email="vault@example.com",
        enabled=True,
        priority=0,
        threshold_pct=90,
        stale_reason="something",
    )
    db = MagicMock()
    db.commit = AsyncMock()

    monkeypatch.setattr(
        ac.aq, "get_account_by_id", AsyncMock(return_value=account),
    )
    monkeypatch.setattr(
        ac, "get_active_email_async",
        AsyncMock(return_value="other@example.com"),
    )
    monkeypatch.setattr(
        ac, "read_credentials_for_email",
        lambda email, active_email=None: None,
    )

    result = await ac.revalidate_account(42, db)
    assert result["success"] is False
    assert result["active_refused"] is False
    assert "vault" in result["stale_reason"].lower()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run python -m pytest tests/test_account_service.py -q -k revalidate
```

Expected: ImportError / AttributeError `has no attribute 'revalidate_account'`.

- [ ] **Step 3: Implement `revalidate_account` in `backend/services/account_service.py`**

Append at the bottom of the file, after `build_ws_snapshot`.

```python
# Per-email async locks serialise concurrent revalidate calls on the same
# account.  Critical: refresh_tokens are single-use; two concurrent calls
# with the same token would have the losing call get 400 invalid_grant
# and overwrite the winner's success with a terminal stale_reason.
_revalidate_locks: dict[str, asyncio.Lock] = {}


def _get_revalidate_lock(email: str) -> asyncio.Lock:
    lock = _revalidate_locks.get(email)
    if lock is None:
        lock = asyncio.Lock()
        _revalidate_locks[email] = lock
    return lock


async def revalidate_account(account_id: int, db) -> dict | None:
    """Run a single on-demand refresh attempt for a stale **vault** account.

    Used by the new ``POST /api/accounts/{id}/revalidate`` endpoint so the
    user can recover accounts that were marked ``stale_reason`` by the poll
    loop's transient-failure escalation without going through the full
    re-login tmux flow.

    **Invariant:** this function refuses to operate on the currently-active
    account.  The CLI owns the active account's refresh lifecycle (see
    CLAUDE.md §"Credential storage") and racing it would corrupt the single-
    use refresh_token.  Users with a phantom-stale active account should
    switch to another account first, then revalidate the now-vault entry.

    Returns ``None`` if the account does not exist.  Otherwise returns:

        {
          "success":         bool,
          "stale_reason":    str | None,   # value after this call
          "email":           str,
          "active_refused":  bool,         # True iff we refused because
                                           # the account is currently active
        }

    On success: ``stale_reason`` is cleared in the DB, fresh tokens are
    written to the vault via ``save_refreshed_vault_token``, and the in-
    memory refresh-backoff counters for the email are cleared.

    On failure: the precise reason is written to ``stale_reason`` so the
    caller and the UI can show an accurate message (a genuine
    ``invalid_grant`` stays stuck; a transient 400 reflects "try again
    later").
    """
    from . import anthropic_api
    from . import credential_provider as cp
    from . import account_queries as aq
    # Late import to avoid circular on background module.
    from .. import background as bg

    account = await aq.get_account_by_id(account_id, db)
    if account is None:
        return None

    email = account.email

    # ── Invariant guard: refuse active-account revalidate ────────────────
    active_email = await get_active_email_async()
    if email == active_email:
        return {
            "success": False,
            "stale_reason": account.stale_reason,
            "email": email,
            "active_refused": True,
        }

    # ── Serialise concurrent calls on the same email ─────────────────────
    lock = _get_revalidate_lock(email)
    async with lock:
        credentials = read_credentials_for_email(email, active_email)
        if not credentials:
            account.stale_reason = "No access token in vault — re-login required"
            await db.commit()
            return {
                "success": False,
                "stale_reason": account.stale_reason,
                "email": email,
                "active_refused": False,
            }

        refresh_token = cp.refresh_token_of(credentials)
        if not refresh_token:
            account.stale_reason = "No refresh token in vault — re-login required"
            await db.commit()
            return {
                "success": False,
                "stale_reason": account.stale_reason,
                "email": email,
                "active_refused": False,
            }

        import httpx
        import time as _time

        try:
            resp = await anthropic_api.refresh_access_token(refresh_token)
        except httpx.HTTPStatusError as refresh_err:
            kind = anthropic_api.parse_oauth_error(refresh_err)
            if kind is anthropic_api.OAuthErrorKind.TERMINAL_REVOKED:
                account.stale_reason = "Refresh token revoked — re-login required"
            elif kind is anthropic_api.OAuthErrorKind.TERMINAL_REJECTED:
                account.stale_reason = "Refresh token rejected — re-login required"
            else:
                account.stale_reason = (
                    f"Refresh endpoint transient failure "
                    f"(HTTP {refresh_err.response.status_code}) — try again later"
                )
            await db.commit()
            return {
                "success": False,
                "stale_reason": account.stale_reason,
                "email": email,
                "active_refused": False,
            }
        except (httpx.RequestError, RuntimeError) as net_err:
            account.stale_reason = f"Refresh network error: {net_err}"
            await db.commit()
            return {
                "success": False,
                "stale_reason": account.stale_reason,
                "email": email,
                "active_refused": False,
            }

        new_token = resp.get("access_token")
        if not new_token:
            account.stale_reason = "Refresh succeeded but response had no access_token"
            await db.commit()
            return {
                "success": False,
                "stale_reason": account.stale_reason,
                "email": email,
                "active_refused": False,
            }

        expires_in = resp.get("expires_in")
        new_expires_at_ms = (
            int(_time.time() * 1000) + int(expires_in) * 1000
            if expires_in
            else None
        )
        new_refresh = resp.get("refresh_token")

        await asyncio.to_thread(
            cp.save_refreshed_vault_token,
            email, new_token, new_expires_at_ms, new_refresh,
        )

        # Clear backoff counters — next poll will re-probe normally.
        bg._refresh_backoff_until.pop(email, None)
        bg._refresh_backoff_count.pop(email, None)
        bg._refresh_backoff_first_failure_at.pop(email, None)

        account.stale_reason = None
        await db.commit()

        return {
            "success": True,
            "stale_reason": None,
            "email": email,
            "active_refused": False,
        }
```

Make sure `asyncio` is imported at the top of `account_service.py` (it already is — verify).

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run python -m pytest tests/test_account_service.py -q -k revalidate
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/services/account_service.py tests/test_account_service.py
git commit -m "$(cat <<'EOF'
feat(revalidate): on-demand recovery for stale vault accounts

account_service.revalidate_account(id, db) runs a single refresh attempt
for a stale account.  Hardened against the two most dangerous race modes:

1. Active-account refusal.  CLAUDE.md invariant: "CCSwitch never refreshes
   the active account; CLI owns that lifecycle."  Revalidating an active
   account would race the CLI's own refresh on the same single-use
   refresh_token.  We refuse upfront with active_refused=True so the
   caller (router → UI) can tell the user to switch first, then retry.

2. Concurrent-call serialisation.  A per-email asyncio.Lock in
   _revalidate_locks prevents two simultaneous revalidate calls from
   POSTing the same refresh_token to Anthropic; the second call would
   get 400 invalid_grant and overwrite the first call's success with
   a terminal stale_reason.

On success it clears stale_reason, writes fresh tokens to the vault,
resets all three background.py transient-refresh counters.  On failure
it writes the precise cause back to stale_reason so the UI can show an
accurate message (terminal = genuine invalid_grant → Re-login; transient
= HTTP code → try again later).

6 tests: success path, active-account refusal, concurrent-call
serialisation, genuine invalid_grant stays stuck, missing account (None),
missing vault entry (error).

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Milestone 4 — `POST /api/accounts/{id}/revalidate` endpoint

**Files:**
- Modify: `backend/schemas.py` (new response model)
- Modify: `backend/routers/accounts.py` (new route)
- Test: `tests/test_accounts_router.py`

### Task 4.1 — Add `RevalidateResult` schema

- [ ] **Step 1: Add schema at `backend/schemas.py`**

Append after `LoginVerifyResult` (or in the same section):

```python
class RevalidateResult(BaseModel):
    """Response from POST /api/accounts/{id}/revalidate.

    HTTP status codes:
    * 200 — ``success=True``.  Stale cleared.
    * 409 — Conflict.  The account's current state does not permit
            revalidation; ``stale_reason`` carries the accurate message
            and ``active_refused`` distinguishes "active account —
            switch first" from "refresh still failing — try later
            or Re-login".

    Using 409 on the failure path lets standard HTTP-error middleware
    and the frontend's ``api.js`` error wrapper catch logical failures
    without having to substring-match on success flags.
    """

    success: bool
    stale_reason: Optional[str] = None
    email: str
    active_refused: bool = False
```

### Task 4.2 — Write failing test for the endpoint

- [ ] **Step 1: Write failing test at `tests/test_accounts_router.py`**

Append to the existing file.  The `make_test_app` fixture from `conftest.py` returns a 2-tuple `(app, TestClient)` — not a 3-tuple; we instantiate our own async client with `ASGITransport` and use the WS manager via `ws.ws_manager` module-level object if the router imports it that way.

```python
# ── Revalidate endpoint ──────────────────────────────────────────────────

from contextlib import asynccontextmanager
from httpx import AsyncClient, ASGITransport


@asynccontextmanager
async def _async_client(app):
    """Context manager that yields an httpx AsyncClient wired to the ASGI app.
    Test files in this repo use different helpers; this is a local one."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_revalidate_endpoint_returns_200_on_success(make_test_app, monkeypatch):
    app, _test_client = await make_test_app()
    from backend.services import account_service as ac

    async def fake_revalidate(account_id, db):
        return {
            "success": True,
            "stale_reason": None,
            "email": "vault@example.com",
            "active_refused": False,
        }

    monkeypatch.setattr(ac, "revalidate_account", fake_revalidate)

    async with _async_client(app) as client:
        resp = await client.post("/api/accounts/1/revalidate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["stale_reason"] is None
    assert body["email"] == "vault@example.com"
    assert body["active_refused"] is False


@pytest.mark.asyncio
async def test_revalidate_endpoint_404_for_missing_account(make_test_app, monkeypatch):
    app, _ = await make_test_app()
    from backend.services import account_service as ac

    async def fake_revalidate(account_id, db):
        return None

    monkeypatch.setattr(ac, "revalidate_account", fake_revalidate)

    async with _async_client(app) as client:
        resp = await client.post("/api/accounts/999/revalidate")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_revalidate_endpoint_409_on_refresh_failure(make_test_app, monkeypatch):
    """Refresh-failure path: 409 Conflict with the accurate stale_reason in the body.
    Frontend error middleware can handle the 409 uniformly; the body stays readable."""
    app, _ = await make_test_app()
    from backend.services import account_service as ac

    async def fake_revalidate(account_id, db):
        return {
            "success": False,
            "stale_reason": "Refresh token rejected — re-login required",
            "email": "vault@example.com",
            "active_refused": False,
        }

    monkeypatch.setattr(ac, "revalidate_account", fake_revalidate)

    async with _async_client(app) as client:
        resp = await client.post("/api/accounts/1/revalidate")
    assert resp.status_code == 409
    body = resp.json()
    # FastAPI wraps non-200 responses from HTTPException as {"detail": {...}};
    # body shape depends on how the endpoint signals failure.  The endpoint
    # must put the RevalidateResult under `detail` OR preserve it via a
    # custom response — either is acceptable, we assert both.
    payload = body.get("detail", body)
    assert payload["success"] is False
    assert "rejected" in payload["stale_reason"].lower()
    assert payload["active_refused"] is False


@pytest.mark.asyncio
async def test_revalidate_endpoint_409_on_active_account(make_test_app, monkeypatch):
    """Active-account revalidate → 409 with active_refused=True and a message
    that tells the user to switch first."""
    app, _ = await make_test_app()
    from backend.services import account_service as ac

    async def fake_revalidate(account_id, db):
        return {
            "success": False,
            "stale_reason": "Refresh token rejected — re-login required",
            "email": "active@example.com",
            "active_refused": True,
        }

    monkeypatch.setattr(ac, "revalidate_account", fake_revalidate)

    async with _async_client(app) as client:
        resp = await client.post("/api/accounts/1/revalidate")
    assert resp.status_code == 409
    payload = resp.json().get("detail", resp.json())
    assert payload["active_refused"] is True


@pytest.mark.asyncio
async def test_revalidate_endpoint_broadcasts_account_updated_on_success(make_test_app, monkeypatch):
    """Success broadcasts account_updated with stale_reason=None so connected
    clients update the card immediately instead of waiting for next usage_updated."""
    app, _ = await make_test_app()
    from backend.services import account_service as ac
    from backend import ws as ws_mod

    async def fake_revalidate(account_id, db):
        return {
            "success": True,
            "stale_reason": None,
            "email": "vault@example.com",
            "active_refused": False,
        }

    monkeypatch.setattr(ac, "revalidate_account", fake_revalidate)

    broadcasts = []
    async def fake_broadcast(msg):
        broadcasts.append(msg)
    monkeypatch.setattr(ws_mod.ws_manager, "broadcast", fake_broadcast)

    async with _async_client(app) as client:
        resp = await client.post("/api/accounts/1/revalidate")
    assert resp.status_code == 200
    assert any(
        b.get("type") == "account_updated"
        and b.get("email") == "vault@example.com"
        and b.get("stale_reason") is None
        for b in broadcasts
    )
```

**Note on `make_test_app`:** `tests/conftest.py` exposes it as a factory that returns `(app, TestClient)` — no separate `ws_manager` or `db_factory` handles.  The WebSocket manager is a module-level singleton exposed as `backend.ws.ws_manager` and we monkeypatch its `broadcast` method directly.  If the actual conftest shape differs at implementation time (e.g. `make_test_app` is an async fixture not a factory), adapt the unpacking; the broadcast monkeypatch is unchanged.

- [ ] **Step 2: Run to verify failure**

```bash
uv run python -m pytest tests/test_accounts_router.py -q -k revalidate
```

Expected: 404 on the route (no such endpoint) or 405 Method Not Allowed.

### Task 4.3 — Implement the endpoint

- [ ] **Step 1: Modify `backend/routers/accounts.py`** — add route at the end of the router block, before the router is re-exported:

```python
@router.post("/{account_id}/revalidate", response_model=RevalidateResult)
async def revalidate(account_id: int, db: AsyncSession = Depends(get_db)):
    """Try once to refresh the account's tokens and clear stale_reason.

    Recovery path for accounts that were escalated to stale by the poll
    loop's transient-failure escalation but whose refresh_token is actually
    still valid (e.g. the failures were an Anthropic-side hiccup or a
    single-use-token race that has since cleared).

    HTTP semantics:
    * ``200`` — success, stale_reason cleared; broadcast account_updated.
    * ``409`` — conflict: refresh still failed OR the account is currently
                active (CLI owns that lifecycle).  Response body is the
                ``RevalidateResult`` with an accurate ``stale_reason`` and
                ``active_refused`` flag.
    * ``404`` — account id unknown.
    """
    result = await ac.revalidate_account(account_id, db)
    if result is None:
        raise HTTPException(404, "Account not found")

    # Success path — broadcast and return 200.
    if result["success"]:
        try:
            await ws_manager.broadcast({
                "type": "account_updated",
                "id": account_id,
                "email": result["email"],
                "stale_reason": None,
            })
        except Exception:
            logger.warning("Post-revalidate broadcast failed for %s", result["email"])
        return RevalidateResult(**result)

    # Failure path — 409 Conflict with the full RevalidateResult in `detail`
    # so standard HTTP-error middleware (frontend's api.js wrapper etc.)
    # can catch it uniformly while keeping the body readable.
    raise HTTPException(status_code=409, detail=RevalidateResult(**result).model_dump())
```

Ensure `RevalidateResult` is imported at the top:

```python
from ..schemas import (
    ...existing...,
    RevalidateResult,
)
```

- [ ] **Step 2: Run tests to verify they pass**

```bash
uv run python -m pytest tests/test_accounts_router.py -q -k revalidate
```

Expected: 4 passed.

- [ ] **Step 3: Commit**

```bash
git add backend/schemas.py backend/routers/accounts.py tests/test_accounts_router.py
git commit -m "$(cat <<'EOF'
feat(api): POST /api/accounts/{id}/revalidate — on-demand stale recovery

Thin router wrapper over account_service.revalidate_account.

HTTP semantics:
  200  success, stale cleared, WS account_updated broadcast with
       stale_reason:null so connected clients update immediately
       (verify-relogin's existing broadcast omits stale_reason and
       forces the UI to wait for the next usage_updated poll — this
       endpoint fixes that for its own flow).
  409  refresh failed OR active-account refused.  Body carries the
       RevalidateResult under `detail` so standard HTTP-error middleware
       can catch it uniformly; `active_refused` distinguishes the two
       cases so the frontend shows the right copy ("Switch to another
       account first, then Revalidate" vs "Refresh failed, try again
       later or Re-login").
  404  account id unknown.

5 tests cover: success (200), 404, refresh-failure (409), active
refusal (409), WS broadcast on success.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

### After M4: 2-3 parallel validation agents

See "Validation protocol" at the bottom.

---

## Milestone 5 — Frontend Revalidate button

**Files:**
- Modify: `frontend/src/api.js`
- Modify: `frontend/src/ui/accounts.js`
- Modify: `frontend/src/main.js`
- Modify: `frontend/src/style.css` (minor — button styling reuse)

### Task 5.1 — API method

- [ ] **Step 1: Add `revalidateAccount` in `frontend/src/api.js`**

Append to the existing API object/module (follow the same pattern as `reloginAccount` / verify-relogin):

```javascript
export async function revalidateAccount(accountId) {
  return api.post(`/api/accounts/${accountId}/revalidate`);
}
```

(Adapt to match the file's existing style — `post`/`fetch`/whatever wrapper is used.  Check the existing `reloginAccount` export as the template.)

### Task 5.2 — Render the Revalidate button on stale cards

- [ ] **Step 1: Modify `frontend/src/ui/accounts.js` around line 178 (the `isStale ? ... : ...` ternary)**

Replace:

```javascript
        ${isStale
          ? `<button class="btn primary relogin-btn" data-id="${acc.id}" data-email="${escapeHtml(acc.email)}" title="Open a terminal and re-authenticate this account">
              <svg>...</svg>
              Re-login
            </button>`
          : `<button class="btn primary ${isActive?"outlined":""} switch-btn" data-id="${acc.id}" ${isActive?"disabled":""} title="...">
              ${isActive ? "Currently active" : "Switch to"}
            </button>`
        }
```

With (keep the exact existing `<svg>` markup from the current Re-login button; only the surrounding structure changes):

```javascript
        ${isStale
          ? `<div class="stale-actions">
              <button class="btn secondary revalidate-btn" data-id="${acc.id}" data-email="${escapeHtml(acc.email)}" title="Try refreshing the tokens once.  Use this when the account was marked stale after a transient Anthropic hiccup and you believe the refresh_token is still valid.">
                <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
                Revalidate
              </button>
              <button class="btn primary relogin-btn" data-id="${acc.id}" data-email="${escapeHtml(acc.email)}" title="Open a terminal and re-authenticate this account">
                <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/><polyline points="10 17 15 12 10 7"/><line x1="15" y1="12" x2="3" y2="12"/></svg>
                Re-login
              </button>
            </div>`
          : `<button class="btn primary ${isActive?"outlined":""} switch-btn" data-id="${acc.id}" ${isActive?"disabled":""} title="...">
              ${isActive ? "Currently active" : "Switch to"}
            </button>`
        }
```

(Copy the EXACT existing SVG from the current Re-login button; the markup above is a placeholder pattern — replace both `<svg>` tags with the existing Re-login SVG and an appropriate refresh-circle SVG for Revalidate.)

- [ ] **Step 2: Add the event listener for `.revalidate-btn` at the bottom of the event-wiring block (around line 284)**

```javascript
  qsa(".revalidate-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      document.dispatchEvent(new CustomEvent("app:revalidate-account", {
        detail: { accountId: Number(btn.dataset.id), email: btn.dataset.email },
      }));
    });
  });
```

- [ ] **Step 3: Add styling snippet at `frontend/src/style.css`** — find the existing `.relogin-btn` block and add directly after:

```css
.stale-actions {
  display: flex;
  gap: 6px;
  align-items: center;
}
.stale-actions .btn {
  flex: 1;
}
.revalidate-btn {
  /* Lower-priority than re-login: secondary button style already gives a
     lighter background.  This class is reserved for future tweaks. */
}
```

### Task 5.3 — Wire the event handler in main.js

- [ ] **Step 1: Modify `frontend/src/api.js` to surface the 409 response body**

The existing `api` wrapper likely throws on non-2xx with a generic message.  The Revalidate endpoint returns 409 with a meaningful body.  Extend the wrapper so the caller can read the 409 `detail` payload.  Verify the actual shape of the existing wrapper first (the exact file structure is not documented in the research but the re-login verify route uses the same pattern — mirror whatever it does).

If the wrapper extracts `error.message` from `detail.error` today, extend it so callers can also inspect `error.body` or the raw response.  Minimum viable shape:

```javascript
// In frontend/src/api.js — replace or extend the existing request helper:
async function request(method, path, body = null) {
  const resp = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await resp.text();
  let parsed = null;
  try { parsed = text ? JSON.parse(text) : null; } catch { /* leave null */ }
  if (!resp.ok) {
    const err = new Error(
      (parsed && parsed.detail && parsed.detail.stale_reason)
        || (parsed && parsed.detail && parsed.detail.error)
        || (parsed && parsed.detail)
        || resp.statusText
    );
    err.status = resp.status;
    err.body = parsed;
    throw err;
  }
  return parsed;
}
```

**Only apply this change if the existing wrapper does NOT already carry
`err.status` and `err.body` — verify at implementation time.**  If it
already does, skip this step and move to Step 2.

- [ ] **Step 2: Modify `frontend/src/main.js`** — find the `app:relogin-account` listener and add a sibling:

```javascript
document.addEventListener("app:revalidate-account", async (ev) => {
  const { accountId, email } = ev.detail;
  try {
    const result = await api.revalidateAccount(accountId);
    // 200 path — success.
    toast.show(`Revalidated ${email} — tokens refreshed.`, "success");
  } catch (err) {
    // 409 path — failure or refusal.  err.body.detail is the RevalidateResult.
    const payload = (err.body && err.body.detail) || {};
    if (payload.active_refused) {
      toast.show(
        `${email} is currently active — CCSwitch cannot revalidate it because ` +
        `the Claude CLI owns the active account's refresh lifecycle.  ` +
        `Switch to another account first, then retry.`,
        "warning",
      );
      return;
    }
    const detail = payload.stale_reason || err.message || "unknown error";
    toast.show(
      `Revalidate failed for ${email}: ${detail}.  ` +
      `Try again later or click Re-login if the problem persists.`,
      "error",
    );
  }
});
```

Make sure `api` is already imported at the top of `main.js`; the existing re-login handler uses it.

### Task 5.4 — Manual browser verification

- [ ] **Step 1: Restart the running server**

```bash
bash scripts/restart.sh  # or kill the existing uvicorn and relaunch
```

- [ ] **Step 2: Visit `http://localhost:41924`, confirm a stale account card shows BOTH buttons**

Visually check the `leusnazarii@gmail.com` card (currently in phantom-stale state):

- "Revalidate" appears to the left (secondary button)
- "Re-login" appears to the right (primary button)
- Clicking Revalidate → toast success → stale banner disappears → Switch-to button returns

- [ ] **Step 3: Commit**

```bash
git add frontend/src/api.js frontend/src/ui/accounts.js frontend/src/main.js frontend/src/style.css
git commit -m "$(cat <<'EOF'
feat(frontend): Revalidate button on stale account cards

Adds a second action button alongside Re-login on stale cards: POSTs to
/api/accounts/{id}/revalidate (added in the previous commit) and on
success the stale banner disappears without the full tmux login dance.
On failure a toast describes the updated stale_reason so the user can
tell "Anthropic hiccup, try again later" apart from "actually need
Re-login".

Manually verified against the leusnazarii@gmail.com phantom-stale
account that motivated this series: one click, stale banner gone,
Switch-to button back.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Milestone 6 — Docs sync + integration test for full flow

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/superpowers/specs/2026-04-15-vault-swap-architecture.md` (new §9.10)
- Modify: `README.md`
- Test: `tests/test_integration_transient_refresh.py` (new)

### Task 6.1 — CLAUDE.md data flow update

- [ ] **Step 1: Modify Key data flow step 3 (the vault-account bullet) in `CLAUDE.md`**

Replace:

```
   - Vault-refresh 400/401: mark `stale_reason` with the precise
     reason; skip the probe that would just repeat the failure.
```

With:

```
   - Vault-refresh 401 or 400 with a body `error` code in the terminal
     set (`invalid_grant`, `invalid_client`, `unauthorized_client`,
     `unsupported_grant_type`, `invalid_scope`): mark `stale_reason`
     with the precise terminal reason; skip the probe.  The UI renders
     a Re-login button.
   - Vault-refresh bare 401, 400 with non-terminal body, 429, 5xx,
     network: classified TRANSIENT by `anthropic_api.parse_oauth_error`.
     Increment three in-memory per-account dicts (count, next-retry
     deadline, first-failure timestamp); do NOT set `stale_reason`.
     Return the last-known cached usage.  Next poll cycle skips the
     refresh until the backoff deadline elapses.  Escalates to a
     distinct terminal `stale_reason` when either (a) 5 consecutive
     transients observed OR (b) ≥ 24 h since the first transient in
     the current streak — whichever trips first.  A successful refresh
     clears all three counters.
```

### Task 6.2 — Spec new §9.10

- [ ] **Step 1: Append new section after §9.9 in the spec file**

```markdown
### 9.10 Transient vs terminal refresh failures

The pre-April-15 behaviour treated any 400 or 401 from Anthropic's
`/oauth2/token` refresh endpoint as terminal: `stale_reason` was
written, the poll loop stopped trying, and the UI demanded a full
tmux re-login.  Empirical evidence showed this misclassified a large
class of failures — refresh-endpoint rate-limits, single-use-refresh-
token rotation races with CLI-owned tokens, Anthropic-side account-
state hiccups — all of which clear within minutes to hours without
any user action.  Accounts would get stuck with perfectly valid
tokens and a phantom "re-login required" flag.

Post-fix classification (`anthropic_api.parse_oauth_error`):

| Response                                          | Kind                   | Action                                         |
|---                                                |---                     |---                                             |
| 401 with body `error` ∈ terminal set              | `TERMINAL_REVOKED`     | Set `stale_reason = "revoked"`, stop retrying. |
| 400 with body `error` ∈ terminal set              | `TERMINAL_REJECTED`    | Set `stale_reason = "rejected"`, stop.         |
| Bare 401 / 401 with non-terminal body             | `TRANSIENT`            | Exponential backoff, retry on next poll.       |
| 400 with non-terminal body / no body              | `TRANSIENT`            | As above — conservative default.               |
| 429 / 5xx / network                               | `TRANSIENT`            | As above.                                      |

The terminal set is the five OAuth2 `error` codes from RFC 6749 §5.2
that indicate a non-self-healing condition: `invalid_grant`,
`invalid_client`, `unauthorized_client`, `unsupported_grant_type`,
`invalid_scope`.  Bare 401 (no body or body without a terminal code)
is treated as transient because Anthropic's edge proxy occasionally
returns 401 for WAF / rate-limit / account-provisioning challenges
that clear within minutes.

Transient failures increment three module-level dicts (parallel to the
existing 429 backoff): `_refresh_backoff_count[email]` for the
consecutive-failure counter, `_refresh_backoff_until[email]` for the
monotonic deadline of the next retry, and
`_refresh_backoff_first_failure_at[email]` for the wall-clock timestamp
of the first failure in the current streak.  On the next poll within
the deadline, the refresh attempt is simply skipped — the stale cached
access token is still returned (and may be valid, as in the motivating
`leusnazarii` case where the CLI had refreshed it while the account was
active).

Escalation to terminal `stale_reason` fires when EITHER of these is
true:

- `_refresh_backoff_count[email] >= _TRANSIENT_REFRESH_ESCALATE_AFTER` (N=5)
- `now - _refresh_backoff_first_failure_at[email] >= _TRANSIENT_REFRESH_ESCALATE_AFTER_SECONDS` (24 h)

The second trigger defeats a pathological counter-reset loop where
Anthropic intermittently succeeds and resets the count while the
account is still net-broken.  The first failure's wall-clock timestamp
is cleared on any successful refresh (alongside the count and deadline),
so genuinely healthy accounts never accumulate.

Recovery without re-login: `POST /api/accounts/{id}/revalidate` runs
a single on-demand refresh attempt.  Two hard invariants:

1. **Active-account refusal.**  The endpoint refuses (`HTTP 409`,
   `active_refused=True`) on the currently-active account.  The CLI
   owns the active account's refresh lifecycle, and a revalidate
   would race its single-use refresh_token with the CLI's own
   refresh — either side's loss corrupts the other.  The user
   switches to another account first, then revalidates the now-vault
   entry.

2. **Per-email serialisation.**  `account_service._revalidate_locks`
   holds one `asyncio.Lock` per email.  Two simultaneous POSTs on
   the same account serialise; the second one sees the rotated
   refresh_token the first one wrote, so both succeed instead of
   the naive implementation's one-succeeds-one-fails-and-overwrites
   failure mode.

On success the account's `stale_reason` is cleared, all three in-memory
backoff counters are dropped, and a `ws account_updated` broadcast
fires with `stale_reason: None` so connected UIs update the card
immediately instead of waiting for the next `usage_updated` poll
cycle.

The frontend exposes this as a secondary "Revalidate" button on stale
cards, next to the primary "Re-login".  The 409 failure path carries
the `RevalidateResult` under `detail` so the frontend's HTTP error
middleware catches it uniformly and displays a differentiating toast:
"switch first" (active-refused) vs "try again later or Re-login"
(refresh-failed).
```

### Task 6.3 — README Troubleshooting + feature bullet

- [ ] **Step 1: Add Troubleshooting entry in `README.md` near the existing "Account switch not picked up in an existing claude pane" block**

```markdown
**Account card shows "Refresh token rejected — re-login required" but tokens are actually valid**
- This used to happen regularly before April 2026: Anthropic's refresh endpoint occasionally returns HTTP 400 for transient reasons (refresh-endpoint rate-limit, single-use-token rotation race with the CLI, account-side hiccups) and the old code interpreted any 400 as terminal.
- Click **Revalidate** (secondary button on stale cards).  It runs a single refresh attempt on-demand.  If the refresh succeeds — which it typically does for transient 400s that have since cleared — the stale flag disappears and the account is back in rotation.  If it fails with a terminal OAuth code (`invalid_grant` / `invalid_client` / `unauthorized_client` / `unsupported_grant_type` / `invalid_scope`) the tokens really are dead; use Re-login.
- The poll loop now distinguishes transient from terminal failures automatically (see spec §9.10).  You should only see this situation on accounts whose `stale_reason` was written BEFORE the April fix.

**Revalidate says "currently active — switch first"**
- CCSwitch refuses to revalidate the currently-active account.  The Claude CLI owns that account's refresh lifecycle, and running a concurrent CCSwitch refresh would race the CLI on the same single-use refresh_token and corrupt both.
- The recovery path is: switch to any other account (manually from the card, or let auto-switch fire), then revalidate the now-vault entry, then switch back if you want.

**Migrating accounts stuck in phantom-stale from before the upgrade**
- The April 2026 upgrade does NOT auto-clear `stale_reason` values written by the old code — those accounts are not polled so nothing retriggers a refresh.
- Go to each phantom-stale card and click **Revalidate**.  If the tokens were actually valid (the common case for accounts that hit the 5-hour rate window while the old code misclassified 400s) the flag clears in one click.  If a particular account really needs Re-login the Revalidate will fail with the accurate terminal reason.
```

- [ ] **Step 2: Update the feature bullet at line ~64-66 to mention transient recovery**

Keep the existing "Stale-account relogin" bullet; append a new bullet after it:

```markdown
- **Transient-failure recovery** — when Anthropic's refresh endpoint returns an ambiguous 400 (not `invalid_grant`), CCSwitch backs off exponentially rather than demanding a re-login; after repeated failures it escalates, and a one-click **Revalidate** button on the stale card tries once on demand.
```

### Task 6.4 — End-to-end integration test

- [ ] **Step 1: Create `tests/test_integration_transient_refresh.py`**

```python
"""End-to-end: a transient 400 storm does NOT mark the account stale;
the Revalidate endpoint recovers a truly stale-flagged account whose
refresh_token is actually valid.
"""
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from backend import background as bg
from backend.services import anthropic_api, account_service as ac
from backend.services import credential_provider as cp


def _http_400(error_code):
    req = httpx.Request("POST", "https://api.anthropic.com/oauth2/token")
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 400
    resp.json = MagicMock(return_value={"error": error_code})
    return httpx.HTTPStatusError("bad", request=req, response=resp)


@pytest.mark.asyncio
async def test_transient_400_storm_never_sets_stale_until_escalation(monkeypatch):
    """Four consecutive transient 400s leave stale_reason None; the fifth escalates."""
    bg._refresh_backoff_until.clear()
    bg._refresh_backoff_count.clear()
    near_expiry_ms = int(time.time() * 1000) + 5 * 60 * 1000

    from backend.models import Account
    account = Account(
        id=1, email="vault@example.com", enabled=True, priority=0,
        threshold_pct=90, stale_reason=None,
    )

    monkeypatch.setattr(
        ac, "read_credentials_for_email",
        lambda email, active_email=None: {
            "claudeAiOauth": {
                "accessToken": "at",
                "refreshToken": "rt",
                "expiresAt": near_expiry_ms,
            },
        },
    )

    async def always_transient(refresh_token):
        raise _http_400("some_transient_oauth_code")
    monkeypatch.setattr(anthropic_api, "refresh_access_token", always_transient)

    async def fake_probe(token):
        return {}
    monkeypatch.setattr(anthropic_api, "probe_usage", fake_probe)

    stale_history = []
    # Force deadline to the past each iteration so skip doesn't fire.
    for attempt in range(1, bg._TRANSIENT_REFRESH_ESCALATE_AFTER + 1):
        bg._refresh_backoff_until["vault@example.com"] = time.monotonic() - 1.0
        _, stale = await bg._process_single_account(account, "other@example.com")
        stale_history.append(stale)

    assert stale_history[:-1] == [None] * (bg._TRANSIENT_REFRESH_ESCALATE_AFTER - 1)
    assert stale_history[-1] is not None
    assert "transient failure" in stale_history[-1]


@pytest.mark.asyncio
async def test_revalidate_recovers_phantom_stale_account(monkeypatch):
    """An account stuck in stale_reason with a valid refresh_token recovers in one POST."""
    from backend.models import Account
    account = Account(
        id=1, email="vault@example.com", enabled=True, priority=0, threshold_pct=90,
        stale_reason="Refresh token rejected — re-login required",
    )

    db = MagicMock()
    db.commit = AsyncMock()

    monkeypatch.setattr(
        ac.aq, "get_account_by_id", AsyncMock(return_value=account),
    )
    monkeypatch.setattr(
        ac, "read_credentials_for_email",
        lambda email, active_email=None: {
            "claudeAiOauth": {
                "accessToken": "at-old", "refreshToken": "rt-live", "expiresAt": 0,
            },
        },
    )

    async def fake_refresh(refresh_token):
        return {"access_token": "at-new", "refresh_token": "rt-new", "expires_in": 3600}
    monkeypatch.setattr(ac.anthropic_api, "refresh_access_token", fake_refresh)

    saved = {}
    def fake_save(email, t, exp, r):
        saved["email"] = email
    monkeypatch.setattr(ac.cp, "save_refreshed_vault_token", fake_save)

    monkeypatch.setattr(
        ac, "get_active_email_async",
        AsyncMock(return_value="other@example.com"),
    )

    # Pre-populate backoff state — revalidate should clear all three dicts.
    bg._refresh_backoff_count["vault@example.com"] = 3
    bg._refresh_backoff_until["vault@example.com"] = time.monotonic() + 100
    bg._refresh_backoff_first_failure_at["vault@example.com"] = time.monotonic() - 60

    result = await ac.revalidate_account(1, db)
    assert result["success"] is True
    assert account.stale_reason is None
    assert "vault@example.com" not in bg._refresh_backoff_count
    assert "vault@example.com" not in bg._refresh_backoff_until
    assert "vault@example.com" not in bg._refresh_backoff_first_failure_at
    assert saved["email"] == "vault@example.com"


@pytest.mark.asyncio
async def test_revalidate_refuses_active_account_never_calls_anthropic(monkeypatch):
    """Active-account revalidate must refuse WITHOUT touching Anthropic.
    This protects the single-refresher invariant with the CLI."""
    from backend.models import Account
    account = Account(
        id=1, email="active@example.com", enabled=True, priority=0, threshold_pct=90,
        stale_reason="Refresh token rejected — re-login required",
    )
    db = MagicMock()
    db.commit = AsyncMock()

    monkeypatch.setattr(
        ac.aq, "get_account_by_id", AsyncMock(return_value=account),
    )
    monkeypatch.setattr(
        ac, "get_active_email_async",
        AsyncMock(return_value="active@example.com"),
    )

    refresh_calls = []
    async def fake_refresh(rt):
        refresh_calls.append(rt)
        return {}
    monkeypatch.setattr(ac.anthropic_api, "refresh_access_token", fake_refresh)

    result = await ac.revalidate_account(1, db)
    assert result["success"] is False
    assert result["active_refused"] is True
    assert refresh_calls == []
    # Original stale_reason preserved.
    assert account.stale_reason == "Refresh token rejected — re-login required"
```

- [ ] **Step 2: Run the integration tests**

```bash
uv run python -m pytest tests/test_integration_transient_refresh.py -q
```

Expected: 2 passed.

- [ ] **Step 3: Run the full test suite**

```bash
uv run python -m pytest tests/ -q
```

Expected: all tests green.

- [ ] **Step 4: Commit**

```bash
git add docs/ CLAUDE.md README.md tests/test_integration_transient_refresh.py
git commit -m "$(cat <<'EOF'
docs+tests: spec §9.10 + CLAUDE.md + README + integration test for transient refresh

Docs describe the classification table, the backoff lifecycle, and the
revalidate recovery path.  Two integration tests: transient storm never
writes stale_reason until escalation threshold; revalidate clears a
phantom-stale account with a valid refresh_token in one call.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

### After M6: final holistic review

See "Validation protocol" — this is the final-ship gate.

---

## Validation protocol — 2–3 parallel agents per milestone

After **every** milestone commit, dispatch 2–3 validation agents in a single message so they run concurrently.  Don't wait — push to the next milestone only after all agents return.

### Standard rotation (after M1, M2, M3, M4)

**Agent A — code-reviewer (superpowers:code-reviewer)** — brief: review the just-shipped commit(s) against the milestone's stated goal.  Check test coverage gaps, edge cases missed, DRY / YAGNI violations, unsafe error swallowing.  Return SHIP / HOLD + numbered findings.

**Agent B — security-focused Explore agent** — brief: attack-surface sweep on the diff.  For M1/M2/M3 focus on: body parsing (is `response.json()` ever awaitable instead of sync? can a malicious server send a body that pops the refresh-backoff dicts wrongly?), race conditions on the in-memory dicts (two concurrent revalidate calls?), input validation (numeric overflow on counter?), logging leaks (refresh_token in log lines?).  For M4 focus on: endpoint auth gating (does it reuse the existing TokenAuthMiddleware?), rate-limit on the POST (can a user DoS the refresh endpoint via /revalidate?), CSRF surface (local-only app, but still).

**Agent C — test-design critic** — brief: read the new tests in isolation.  Check: are edge cases missing (empty string, None, concurrent calls, what if the account is active vs vault)?  Are assertions strong enough (checking a value vs checking its type)?  Are mocks over-permissive (would a real implementation bug slip past)?  Return prioritized list of missing tests.

### Special rotation for M5 (frontend)

**Agent A — UX-flow reviewer** — brief: the Revalidate button + toast copy + button placement in the stale banner.  Anything confusing?  Accessibility (ARIA labels)?  Error-copy differentiates "try again later" from "re-login needed"?

**Agent B — browser-automation verifier** — brief: if a playwright / headless chrome harness exists, run it against the dev server.  If not, the user performs manual verification.  Report what they should click in what order.

### Special rotation for M6 (final ship gate)

**Three agents in parallel, all returning SHIP / HOLD:**

1. **superpowers:code-reviewer** — holistic cross-layer review of the full feature (M1–M6) — schema ↔ services ↔ routers ↔ frontend ↔ tests ↔ docs consistency.
2. **Explore agent — integration-scenario replay** — walk through the motivating `leusnazarii@gmail.com` scenario: simulate a transient 400 storm, verify no stale_reason writes; then simulate a real `invalid_grant`, verify stale_reason is set; then simulate a phantom-stale with valid tokens, verify Revalidate works.
3. **Explore agent — ToS + behaviour alignment** — does anything in this change bring CCSwitch closer to the Anthropic ToS gray zone (e.g., retrying refresh too aggressively)?  The existing `_BACKOFF_INITIAL = 120s` floor should protect us but verify.

---

## Shipping decision

`git push origin main` after M6's three agents all return SHIP.  If any return HOLD, address the finding in a targeted commit and re-run only that agent.
