# Reactive Vault Refresh — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Align CCSwitch with OAuth 2.1 RTR (Refresh-Token Rotation) security model by removing proactive vault-account refreshes.  The current proactive pattern (refresh 20 minutes before claimed expiry) generates ~1 rotation event per idle vault account per hour, each one a potential broken-chain trigger for Anthropic's server-side reuse-detection that invalidates the entire token family.  After this plan lands, vault-token refreshes fire ONLY when they are actually needed: on probe 401 (reactive) or on promotion-to-active (pre-fresh for CLI).

**Prior work.** This plan extends the transient-refresh-failure-recovery feature
(April 15, see `docs/superpowers/plans/2026-04-15-transient-refresh-failure-recovery.md`).
That feature introduced `_record_transient_refresh_failure`, the three
`_refresh_backoff_*` dicts, `_revalidate_locks`, and `revalidate_account` —
all of which are reused, renamed, or extended here.

**Architecture:** Four-layer change, strictly local to `background.py` and `account_service.py`.

1. **M1 — Remove proactive vault refresh.**  Delete the `expires_at - 20min` pre-fetch gate from `_process_single_account`.  Vault tokens expire naturally; the next poll's probe catches that.  Extract the refresh-error handling (terminal / transient / network branches) into a reusable `_refresh_vault_token(email, refresh_token)` helper that returns the new tokens or raises `_RefreshTerminal` (with `reason` attribute carrying the stale_reason string).
2. **M2 — Reactive refresh on vault probe 401 (under shared lock).**  Combined with the lock-rename work: rename `_revalidate_locks` in `account_service.py` to `_refresh_locks`, expose `get_refresh_lock` + `forget_refresh_lock`, update the router, and add the reactive path in `_process_single_account`.  When a vault probe returns 401, acquire the shared per-email refresh lock, call the helper from M1 to refresh, retry the probe ONCE with the new token, and only if that second probe still 401s → write stale_reason.  The lock coverage prevents a user-triggered Revalidate from racing a poll-loop reactive-refresh on the same single-use refresh_token.  Landing reactive refresh without the shared lock would reintroduce the Revalidate-vs-poll race the April-15 plan fixed.
3. **M3 — Refresh-on-promotion in swap.**  `swap_to_account` step 0.5 (new, between load-incoming and checkpoint-outgoing): attempt one refresh on the incoming vault's refresh_token under the shared lock.  On success, use the fresh tokens as `incoming`.  On terminal failure, abort the swap with a clear SwapError.  On transient/network failure, log and proceed with stored tokens (CLI will refresh on its first use, just like today).  Minimises the window where a newly-promoted account's access_token is near expiry and forces a CLI-side refresh on the first user keypress.
4. **M4 — Checkpoint omits stale `expiresAt`.**  `_merge_checkpoint`: the `expiresAt` field copied from the standard Keychain during step 2 is a CLI-authored timestamp that may already be stale relative to Anthropic's server state (the CLI's last refresh response may have set it, but the server may have since rotated).  Strip `expiresAt` from the checkpointed blob (both nested and legacy root-level shapes).  The next successful refresh (via M1's helper) writes a fresh one.

> **MVP scoping.**  Milestones M1 + M2 (reactive refresh + unified lock, combined per
> the wave-1 review) are the CORE fix for the production phantom-stale incident.
> M3 (swap-on-promotion) and M4 (checkpoint expiresAt strip) are hardening polish
> that can ship in follow-on PRs.  If time-to-deploy is critical, land M1+M2 alone
> first; M3 and M4 do not affect correctness of the core fix.  Default scoping:
> all four milestones + M5 (docs + PR) ship as one unit.

**Tech stack:** Python 3.12+ / FastAPI / SQLAlchemy-async / httpx / pytest-asyncio.  macOS Keychain via `security`.  No schema change, no migration, no frontend change.

**Out of scope:**
- UI changes (stats page / token-age indicator) — tracked separately.
- Admin API key alternative (Anthropic's `setup-token`) — different architecture.
- Settings toggle for refresh policy — not useful; the reactive policy is strictly safer.

---

## Files touched

| Role | File | Change |
|---|---|---|
| Core (M1) | `backend/background.py` | Delete proactive-refresh block; add `_refresh_vault_token` helper with `_RefreshTerminal(reason)` exception |
| Core (M2) | `backend/background.py` | Add reactive branch to probe-401 handler (acquires shared lock, with thundering-herd cooldown) |
| Orchestration (M2) | `backend/services/account_service.py` | Rename `_revalidate_locks` → `_refresh_locks`; expose `get_refresh_lock` + `forget_refresh_lock`; update router |
| Orchestration (M3) | `backend/services/account_service.py` | Swap step 0.5 (refresh on promotion) |
| Orchestration (M4) | `backend/services/account_service.py` | Strip `expiresAt` in `_merge_checkpoint` (both nested + legacy shapes) |
| Tests | `tests/test_background.py` | Update refresh-related tests; new reactive-path tests; lock integration |
| Tests | `tests/test_account_service.py` | Update revalidate to use renamed lock; new swap-refresh tests; checkpoint test |
| Tests | `tests/test_integration_reactive_refresh.py` | NEW — end-to-end phantom-stale recovery scenario |
| Docs (M5) | `CLAUDE.md` | Key data flow step 3: rewrite vault-refresh bullet |
| Docs (M5) | `docs/superpowers/specs/2026-04-15-vault-swap-architecture.md` | NEW §9.11 "Reactive refresh policy" |
| Docs (M5) | `README.md` | Troubleshooting + ToS note |

---

## Milestone 1 — Extract `_refresh_vault_token` helper, remove proactive gate

**Files:**
- Modify: `backend/background.py:177-292` (the `_process_single_account` body, specifically the refresh block)
- Test: `tests/test_background.py`

### Task 1.0 — Pre-flight: audit existing `_RefreshTerminal()` bare callers

The April-15 feature already introduced `_RefreshTerminal` (without the `reason`
argument — it was bare).  This plan's wave-1 revision made `reason` required.
Any pre-existing test or code site calling `_RefreshTerminal()` bare will break.

- [ ] **Step 1: Grep the backend + tests for bare `_RefreshTerminal` calls**:

```bash
grep -rn 'raise _RefreshTerminal()' backend/ tests/
grep -rn '_RefreshTerminal()' backend/ tests/
grep -rn 'except _RefreshTerminal:' backend/ tests/
```

Expected pre-existing matches (from the April-15 feature):

- `backend/background.py` — multiple `raise _RefreshTerminal()` inside the
  now-deleted proactive-refresh block (they go away with M1 anyway).
- `backend/background.py` `except _RefreshTerminal: raise` — catches without
  reading reason; unchanged behavior, still fine.
- Any tests asserting `pytest.raises(_RefreshTerminal)` without capturing
  `.reason` — still pass because the constructor now accepts reason but
  callers don't need to read it.

- [ ] **Step 2: If any call site exists that raises `_RefreshTerminal()` WITHOUT
  a reason and that code path survives M1**, convert it to pass an appropriate
  reason string.  Most likely: zero such sites (proactive block deletion removes
  the surviving raises).  Document the grep result in the M1 commit message so
  a reviewer doesn't wonder whether it was checked.

### Task 1.1 — Write failing tests for the helper's contract

- [ ] **Step 1: Append to `tests/test_background.py`** after the existing transient tests.  The helper contract: takes `(email, refresh_token)`, returns the new-token dict on success (new access_token, new expires_at_ms, optional new refresh_token), raises `_RefreshTerminal(reason=...)` on terminal (the exception itself carries the stale_reason via `err.reason`), propagates `_RefreshTerminal` after recording a transient offense that trips escalation, propagates `httpx.HTTPStatusError` / `httpx.RequestError` after recording a sub-threshold transient offense.

```python
# ── _refresh_vault_token helper contract ──────────────────────────────────

@pytest.mark.asyncio
async def test_refresh_vault_token_success_returns_new_blob(monkeypatch):
    """Successful refresh returns a dict with new access_token, expires_at_ms,
    and optionally new refresh_token.  Clears all three backoff dicts on success."""
    bg._refresh_backoff_count["vault@example.com"] = 2
    bg._refresh_backoff_until["vault@example.com"] = time.monotonic() - 1.0
    bg._refresh_backoff_first_failure_at["vault@example.com"] = time.monotonic() - 60

    async def fake_refresh(rt):
        assert rt == "rt-old"
        return {"access_token": "at-new", "refresh_token": "rt-new", "expires_in": 3600}

    monkeypatch.setattr(anthropic_api, "refresh_access_token", fake_refresh)

    result = await bg._refresh_vault_token("vault@example.com", "rt-old")
    assert result["access_token"] == "at-new"
    assert result["refresh_token"] == "rt-new"
    # expires_at_ms should be ~= now + 3600 s (±5s tolerance for async overhead)
    expected = int(time.time() * 1000) + 3600 * 1000
    assert abs(result["expires_at_ms"] - expected) < 5000
    # All three backoff dicts cleared.
    assert "vault@example.com" not in bg._refresh_backoff_count
    assert "vault@example.com" not in bg._refresh_backoff_until
    assert "vault@example.com" not in bg._refresh_backoff_first_failure_at


@pytest.mark.asyncio
async def test_refresh_vault_token_terminal_400_raises(monkeypatch):
    """Terminal 400 (e.g. invalid_grant / invalid_request_error) → helper
    raises _RefreshTerminal carrying the stale_reason on err.reason."""
    async def fake_refresh(rt):
        raise _http_error(400, json_body={"error": "invalid_grant"})
    monkeypatch.setattr(anthropic_api, "refresh_access_token", fake_refresh)

    with pytest.raises(bg._RefreshTerminal) as excinfo:
        await bg._refresh_vault_token("vault@example.com", "rt-dead")
    assert "rejected" in excinfo.value.reason or "revoked" in excinfo.value.reason


@pytest.mark.asyncio
async def test_refresh_vault_token_terminal_401_raises(monkeypatch):
    async def fake_refresh(rt):
        raise _http_error(401, json_body={"error": "invalid_grant"})
    monkeypatch.setattr(anthropic_api, "refresh_access_token", fake_refresh)
    with pytest.raises(bg._RefreshTerminal) as excinfo:
        await bg._refresh_vault_token("vault@example.com", "rt-dead")
    assert "revoked" in excinfo.value.reason


@pytest.mark.asyncio
async def test_refresh_vault_token_transient_escalates_after_n(monkeypatch):
    """Below escalation threshold: records offense, re-raises HTTPStatusError."""
    bg._refresh_backoff_count["vault@example.com"] = 0
    async def fake_refresh(rt):
        raise _http_error(400, json_body={"error": "invalid_request"})
    monkeypatch.setattr(anthropic_api, "refresh_access_token", fake_refresh)

    # First transient — records offense, re-raises HTTPStatusError (not _RefreshTerminal).
    with pytest.raises(httpx.HTTPStatusError):
        await bg._refresh_vault_token("vault@example.com", "rt-probably-live")
    assert bg._refresh_backoff_count["vault@example.com"] == 1

    # Push to threshold.
    bg._refresh_backoff_count["vault@example.com"] = bg._TRANSIENT_REFRESH_ESCALATE_AFTER - 1
    bg._refresh_backoff_first_failure_at["vault@example.com"] = time.monotonic() - 10

    # Nth transient — escalates, raises _RefreshTerminal with a reason.
    with pytest.raises(bg._RefreshTerminal) as excinfo:
        await bg._refresh_vault_token("vault@example.com", "rt-probably-live")
    assert excinfo.value.reason  # non-empty stale_reason string


@pytest.mark.asyncio
async def test_refresh_vault_token_network_error_records_transient(monkeypatch):
    """httpx.RequestError → same transient ladder.  Below threshold → re-raises
    RequestError, counter incremented."""
    bg._refresh_backoff_count.pop("vault@example.com", None)
    async def fake_refresh(rt):
        raise httpx.ConnectError("simulated")
    monkeypatch.setattr(anthropic_api, "refresh_access_token", fake_refresh)

    with pytest.raises(httpx.RequestError):
        await bg._refresh_vault_token("vault@example.com", "rt-live")
    assert bg._refresh_backoff_count["vault@example.com"] == 1
    assert "vault@example.com" in bg._refresh_backoff_first_failure_at


@pytest.mark.asyncio
async def test_refresh_vault_token_keychain_persist_failure_after_rotation_escalates(monkeypatch):
    """Anthropic rotated our tokens; Keychain persist fails 3×.  The helper
    must escalate to _RefreshTerminal with a clear reason, NOT silently
    return success with a non-persisted token (which would break chain on
    next refresh)."""
    async def fake_refresh(rt):
        return {"access_token": "at-new", "refresh_token": "rt-new", "expires_in": 3600}
    monkeypatch.setattr(anthropic_api, "refresh_access_token", fake_refresh)

    attempts = []
    def always_fail(email, access_token, expires_at=None, refresh_token=None, **kw):
        attempts.append(1)
        raise OSError("Keychain locked")
    monkeypatch.setattr(cp, "save_refreshed_vault_token", always_fail)

    with pytest.raises(bg._RefreshTerminal) as excinfo:
        await bg._refresh_vault_token("vault@example.com", "rt-live")
    assert "Keychain write failed" in excinfo.value.reason
    assert len(attempts) == 3  # retry loop exhausted


@pytest.mark.asyncio
async def test_refresh_vault_token_persist_timeout_aborts_without_retry(monkeypatch):
    """subprocess.TimeoutExpired on Keychain write aborts IMMEDIATELY
    without retrying — the subprocess was hung on UI password prompt
    and retrying solves nothing."""
    import subprocess as sp

    async def fake_refresh(rt):
        return {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
    monkeypatch.setattr(anthropic_api, "refresh_access_token", fake_refresh)

    attempts = []
    def timeout_once(email, access_token, expires_at=None, refresh_token=None, **kw):
        attempts.append(1)
        raise sp.TimeoutExpired("/usr/bin/security", 5)
    monkeypatch.setattr(cp, "save_refreshed_vault_token", timeout_once)

    with pytest.raises(bg._RefreshTerminal) as excinfo:
        await bg._refresh_vault_token("vault@example.com", "rt-live")
    assert "TimeoutExpired" in excinfo.value.reason
    assert len(attempts) == 1  # no retry
```

- [ ] **Step 2: Run tests to verify they fail with AttributeError on `_refresh_vault_token`**

```bash
uv run python -m pytest tests/test_background.py -q -k refresh_vault_token
```

Expected: all 7 tests fail with `AttributeError: module 'backend.background' has no attribute '_refresh_vault_token'` (or `_RefreshTerminal`).

### Task 1.2 — Implement the helper + delete proactive block

- [ ] **Step 1: Add `_refresh_vault_token` module-level helper in `backend/background.py`** just after `_record_transient_refresh_failure` (which it reuses).  Exact code:

```python
class _RefreshTerminal(Exception):
    """Raised when a refresh attempt returned a terminal status.  The
    ``reason`` attribute carries the stale_reason string the caller
    should write to account.stale_reason."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


async def _refresh_vault_token(email: str, refresh_token: str) -> dict:
    """Perform one refresh attempt for a vault account's refresh_token.

    Success path returns:

        {
          "access_token":   str,
          "refresh_token":  str | None,          # None if server did not rotate
          "expires_at_ms":  int | None,          # absolute epoch ms from expires_in
        }

    and clears all three transient-refresh backoff dicts for ``email``.

    Failure paths (all raise with the stale_reason on ``err.reason``):
    * ``httpx.HTTPStatusError`` terminal (OAuthErrorKind.TERMINAL_*) —
      raise ``_RefreshTerminal(reason=<specific>)``.
    * HTTPStatusError transient below escalation threshold —
      ``_record_transient_refresh_failure`` logs + records, then
      re-raise the original ``HTTPStatusError`` so the caller can
      decide whether to fall through.
    * HTTPStatusError transient that trips escalation —
      raise ``_RefreshTerminal(reason=<escalation>)``.
    * ``httpx.RequestError`` (network) — same ladder as above,
      re-raise on sub-threshold / ``_RefreshTerminal`` on escalation.
    * Keychain persist failure after successful rotation (retries 3×
      with backoff, then escalates) — raise ``_RefreshTerminal(
      reason="Keychain write failed after refresh ... — re-login
      required")``.  Anthropic has already rotated, we cannot persist,
      so the next refresh attempt would present a dead refresh_token.

    ``_RefreshTerminal`` is the signal to the caller: "this refresh
    attempt closed the book on this account — write the reason and
    stop."  The exact stale_reason string lives on ``err.reason``;
    the caller reads it from ``except _RefreshTerminal as e: ...
    e.reason``.  No module-level sidecar dict — the exception is the
    contract.

    Contract guarantees: the function ONLY reads Anthropic and writes
    the vault + backoff dicts.  It does NOT touch the DB, the cache
    (other than via save_refreshed_vault_token's Keychain write), or
    any lock (that is the caller's responsibility — see M2).
    """
    try:
        resp = await anthropic_api.refresh_access_token(refresh_token)
    except httpx.HTTPStatusError as refresh_http_err:
        kind = anthropic_api.parse_oauth_error(refresh_http_err)
        status = refresh_http_err.response.status_code
        if kind is anthropic_api.OAuthErrorKind.TERMINAL_REVOKED:
            logger.error(
                "Refresh token revoked for %s (HTTP 401 + terminal body) — re-login required.",
                email,
            )
            _refresh_backoff_until.pop(email, None)
            _refresh_backoff_count.pop(email, None)
            _refresh_backoff_first_failure_at.pop(email, None)
            raise _RefreshTerminal("Refresh token revoked — re-login required") from refresh_http_err
        if kind is anthropic_api.OAuthErrorKind.TERMINAL_REJECTED:
            logger.error(
                "Refresh token rejected for %s (HTTP %d + terminal OAuth code) — re-login required.",
                email, status,
            )
            _refresh_backoff_until.pop(email, None)
            _refresh_backoff_count.pop(email, None)
            _refresh_backoff_first_failure_at.pop(email, None)
            raise _RefreshTerminal("Refresh token rejected — re-login required") from refresh_http_err
        # TRANSIENT
        stale = _record_transient_refresh_failure(email, status)
        if stale is not None:
            raise _RefreshTerminal(stale) from refresh_http_err
        raise
    except httpx.RequestError as refresh_net_err:
        logger.warning(
            "Refresh network error for %s: %s", email, refresh_net_err,
        )
        stale = _record_transient_refresh_failure(email, None)
        if stale is not None:
            raise _RefreshTerminal(stale) from refresh_net_err
        raise

    new_token = resp.get("access_token")
    if not new_token:
        # Shouldn't happen on a 200 — defensive.
        raise RuntimeError("Refresh response missing access_token")

    expires_in = resp.get("expires_in")
    new_expires_at_ms = (
        int(time.time() * 1000) + int(expires_in) * 1000
        if expires_in
        else None
    )
    new_refresh = resp.get("refresh_token")

    # Atomic persist: server has rotated, we MUST successfully store the
    # new tokens or the next refresh attempt will present a dead refresh_token
    # and Anthropic will family-revoke all tokens for this user session.
    # Retry the Keychain write briefly before giving up.  Distinguish
    # subprocess.TimeoutExpired (Keychain UI blocked on user password
    # prompt — retrying is wasted work) from other exceptions (genuine
    # transient — retry with backoff).
    import subprocess as _sp

    persist_err: Exception | None = None
    for attempt in range(3):
        try:
            await asyncio.to_thread(
                cp.save_refreshed_vault_token,
                email, new_token, expires_at=new_expires_at_ms,
                refresh_token=new_refresh,
            )
            persist_err = None
            break
        except _sp.TimeoutExpired as e:
            # Keychain subprocess hung — likely waiting for a UI password
            # prompt the user isn't responding to.  Retrying is a waste.
            # Abort immediately with escalation.
            persist_err = e
            logger.warning(
                "Keychain persist timed out for %s (Keychain locked UI?): %s",
                email, e,
            )
            break
        except Exception as e:
            persist_err = e
            logger.warning(
                "Keychain persist failed for %s attempt %d/3: %s",
                email, attempt + 1, e,
            )
            await asyncio.sleep(0.1 * (attempt + 1))
    if persist_err is not None:
        # All retries failed (or TimeoutExpired aborted immediately).
        # Anthropic has rotated; we cannot persist.  Next refresh WILL
        # fail.  Escalate to stale_reason NOW rather than leaving the
        # account quietly broken.
        logger.error(
            "Keychain persist exhausted retries for %s — marking stale: %s",
            email, persist_err,
        )
        raise _RefreshTerminal(
            f"Keychain write failed after refresh ({type(persist_err).__name__}) — "
            f"re-login required"
        )

    logger.info("Refreshed vault token for %s", email)

    _refresh_backoff_until.pop(email, None)
    _refresh_backoff_count.pop(email, None)
    _refresh_backoff_first_failure_at.pop(email, None)

    return {
        "access_token": new_token,
        "refresh_token": new_refresh,
        "expires_at_ms": new_expires_at_ms,
    }
```

- [ ] **Step 2: Delete the entire proactive-refresh block** at `backend/background.py:208-292`.  Replace with a single comment marking the removal:

```python
        # Note: proactive vault-token refresh (previously triggered when
        # expires_at - now ≤ 20 min) has been removed.  Rationale: per
        # OAuth 2.1 RTR best practices, refresh only on demand.  The
        # reactive path below (probe 401 → try refresh once → retry
        # probe) + swap step 0.5 (refresh on promotion) handle every
        # case the proactive path used to handle, without generating
        # rotation events for idle vault accounts.  See spec §9.11.
```

**Caveat:** preserve the `forget_account_state` bookkeeping cleanups (lines 162-174 of the current file) — they still apply to the backoff dicts and are called from the account-delete router.  Do NOT delete those.  (Note: `forget_account_state` does NOT need updating for `_pending_terminal_reason` — that dict no longer exists; the `.reason` attribute on `_RefreshTerminal` is ephemeral and ties to the exception lifetime.  The M2 revision adds `_last_reactive_refresh_at` cleanup here instead.)

- [ ] **Step 3: Run helper tests — expect 7 pass**

```bash
uv run python -m pytest tests/test_background.py -q -k refresh_vault_token
```

- [ ] **Step 4: Run full `test_background.py`** — some existing tests WILL FAIL because we just deleted the proactive block.  Expected failures: tests that relied on the proactive refresh firing (e.g. `test_refresh_backoff_skips_retry_within_deadline`, `test_refresh_success_then_transient_starts_fresh_escalation_clock`).  These tests need to be re-scoped to exercise the new `_refresh_vault_token` directly OR deleted if redundant with the new helper tests.

Update the three existing tests that reference the proactive path: `test_refresh_400_invalid_grant_sets_terminal_stale`, `test_refresh_400_invalid_request_is_transient_no_stale`, `test_refresh_transient_escalates_after_n_failures`, `test_refresh_transient_escalates_after_wall_clock_ceiling`, `test_refresh_backoff_skips_retry_within_deadline`, `test_refresh_success_clears_backoff_counters`, `test_refresh_success_then_transient_starts_fresh_escalation_clock`, `test_refresh_network_error_is_transient`:

- Those that directly tested the proactive path → rewrite to call `_refresh_vault_token` directly (the helper is now the contract surface).
- Those that tested downstream side effects (stale_reason set on poll result) → rewrite to simulate the M2 reactive-refresh path (will be easier to finish in M2).  For M1's commit, mark them `@pytest.mark.skip(reason="M2 will re-enable via reactive path")`.  Delete the skip in M2.

- [ ] **Step 5: Run the full test suite** — expect no green regressions outside the explicitly-skipped ones.

```bash
uv run python -m pytest tests/ -q
```

Target: 207 pre-fix — N skipped (document N in commit message).

- [ ] **Step 6: Commit**

```bash
git add backend/background.py tests/test_background.py
git commit -m "$(cat <<'EOF'
refactor(background): extract _refresh_vault_token helper, remove proactive gate (M1)

Part 1 of the reactive-vault-refresh plan (docs/superpowers/plans/
2026-04-16-reactive-vault-refresh.md).

Removed: proactive refresh block in _process_single_account that
fired whenever (now > expires_at - 20min) for vault accounts.  Per
OAuth 2.1 RTR best practices, this pattern generates ~1 rotation
event per idle vault account per hour; each rotation is a potential
broken-chain trigger for Anthropic's server-side reuse detection that
invalidates the entire token family.  After this commit, vault
tokens are refreshed ONLY on demand:
  - M2 (next commit): probe 401 → try refresh under shared lock, retry probe once.
  - M3: swap step 0.5 (refresh on promotion).

Added: _refresh_vault_token(email, refresh_token) helper that
encapsulates the full error-classification ladder (terminal 400/401,
transient with escalation, network, Keychain persist failure).  The
helper returns the new-token dict on success and raises
_RefreshTerminal(reason=...) on any terminal outcome — callers read
err.reason directly; no sidecar dict, no hidden coupling.  Keychain
persist retries 3× with backoff, then escalates to a terminal
_RefreshTerminal so a post-rotation Keychain write failure cannot
silently leave us holding a dead chain.

7 new tests cover the helper contract directly (incl. a persist-
failure-after-rotation escalation test + a subprocess.TimeoutExpired
abort-without-retry test).  N pre-existing tests that exercised the
proactive path are temporarily skipped (M2 will re-enable them
against the reactive path).

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Milestone 2 — Reactive refresh on vault probe 401 (under shared lock)

> **Why this milestone combines two concerns.**  The reactive-refresh path
> (was M2) depends on the unified per-email refresh lock (was M3).  Shipping
> the reactive path without the lock would reintroduce the Revalidate-vs-poll
> race that the April-15 plan fixed: both would POST the same single-use
> refresh_token, Anthropic would invalid_grant the loser, and the loser would
> overwrite the winner's success with a phantom-stale_reason.  So the lock
> rename + reactive path + router update all land as one milestone.

**Cooldown asymmetry.**  The 60 s reactive-refresh cooldown
(`_last_reactive_refresh_at`) applies ONLY to the poll-loop path, NOT to
user-triggered `revalidate_account`.  Rationale: poll-loop fires automatically
on probe 401; without cooldown, N vault accounts hitting 401 simultaneously
(Anthropic degraded state) would issue N concurrent refresh POSTs.  User-
triggered Revalidate is a conscious act initiated by a click; cooldown-skipping
it would silently drop the action and confuse UX ("I clicked it, nothing
happened").  The shared `get_refresh_lock` still prevents concurrent Revalidate
races.  Sequential Revalidate within 60 s (user rapid-clicking) is accepted as
intentional; frontend debounce could be added in a follow-up UI fix if real
users exhibit the behavior.

**Files:**
- Modify: `backend/services/account_service.py:446-462` (rename `_revalidate_locks` → `_refresh_locks`; export `get_refresh_lock` + `forget_refresh_lock`)
- Modify: `backend/routers/accounts.py` (delete-account handler uses renamed helper)
- Modify: `backend/background.py:319-349` (probe-error handler; acquire `get_refresh_lock` around reactive refresh; add thundering-herd cooldown)
- Test: `tests/test_background.py`
- Test: `tests/test_account_service.py` (lock serialisation)

### Task 2.1 — Rename + re-export the lock helper

- [ ] **Step 1: In `backend/services/account_service.py`**, rename:

```python
# OLD
_revalidate_locks: dict[str, asyncio.Lock] = {}

def _get_revalidate_lock(email: str) -> asyncio.Lock:
    ...
    return _revalidate_locks.setdefault(email, asyncio.Lock())

def forget_revalidate_lock(email: str) -> None:
    _revalidate_locks.pop(email, None)
```

To:

```python
# NEW — unified refresh lock, covers revalidate + poll-loop reactive
# refresh + swap-refresh.  Single-use refresh_tokens race across these
# code paths; one lock per email is the right granularity (different
# emails don't contend; same email serialises).
_refresh_locks: dict[str, asyncio.Lock] = {}


def get_refresh_lock(email: str) -> asyncio.Lock:
    """Return the single asyncio.Lock instance for ``email``, creating
    it atomically via dict.setdefault if absent."""
    return _refresh_locks.setdefault(email, asyncio.Lock())


def forget_refresh_lock(email: str) -> None:
    """Drop the per-email refresh lock.  Called on account delete."""
    _refresh_locks.pop(email, None)
```

Update every reference in the same file (`revalidate_account` calls `_get_revalidate_lock` → change to `get_refresh_lock`).

Update the router at `backend/routers/accounts.py` delete-account handler — change the `forget_revalidate_lock` call to `forget_refresh_lock`.

- [ ] **Step 2: In `backend/background.py`**, add an import at the top:

```python
# Shared per-email refresh lock — serialises the reactive-refresh path
# with revalidate_account + swap-refresh.
from .services.account_service import get_refresh_lock
```

### Task 2.2 — Write failing tests for reactive-refresh behavior + lock coverage

- [ ] **Step 1: Add tests at `tests/test_background.py`** after the M1 helper tests.

```python
# ── Reactive refresh on vault probe 401 ──────────────────────────────────

@pytest.mark.asyncio
async def test_vault_probe_401_triggers_refresh_and_retry_success(monkeypatch):
    """When a vault probe returns 401 and the subsequent refresh succeeds
    + retry-probe succeeds, stale_reason is NOT written.  This is the
    common case: access_token died early but refresh_token is still live."""
    bg._refresh_backoff_until.clear()
    bg._last_reactive_refresh_at.clear()

    account = _make_account(email="vault@example.com")
    monkeypatch.setattr(
        ac, "read_credentials_for_email",
        lambda email, active_email=None: _fresh_creds(),
    )

    probe_calls: list[str] = []
    async def fake_probe(token):
        probe_calls.append(token)
        if len(probe_calls) == 1:
            raise _http_error(401)
        # Second probe (with new token) succeeds.
        return {"five_hour": {"utilization": 0.42}}
    monkeypatch.setattr(anthropic_api, "probe_usage", fake_probe)

    async def fake_refresh(rt):
        return {"access_token": "at-new", "refresh_token": "rt-new", "expires_in": 3600}
    monkeypatch.setattr(anthropic_api, "refresh_access_token", fake_refresh)

    def fake_save(email, access_token, expires_at=None, refresh_token=None, **kw):
        pass
    monkeypatch.setattr(cp, "save_refreshed_vault_token", fake_save)

    entry, stale = await bg._process_single_account(account, "other@example.com")
    assert stale is None
    assert len(probe_calls) == 2  # original + retry after refresh
    # Returned entry carries the successful usage, not an error.
    assert entry.get("usage", {}).get("five_hour_pct") == 42


@pytest.mark.asyncio
async def test_vault_probe_401_refresh_success_but_retry_still_401(monkeypatch):
    """Refresh succeeds but retry-probe still 401s.  Genuinely dead token
    server-side in a way refresh cannot recover.  Write stale_reason."""
    bg._refresh_backoff_until.clear()
    bg._last_reactive_refresh_at.clear()

    account = _make_account(email="vault@example.com")
    monkeypatch.setattr(
        ac, "read_credentials_for_email",
        lambda email, active_email=None: _fresh_creds(),
    )

    async def fake_probe(token):
        raise _http_error(401)  # both calls fail
    monkeypatch.setattr(anthropic_api, "probe_usage", fake_probe)

    async def fake_refresh(rt):
        return {"access_token": "at-new", "refresh_token": "rt-new", "expires_in": 3600}
    monkeypatch.setattr(anthropic_api, "refresh_access_token", fake_refresh)

    def fake_save(email, access_token, expires_at=None, refresh_token=None, **kw):
        pass
    monkeypatch.setattr(cp, "save_refreshed_vault_token", fake_save)

    _, stale = await bg._process_single_account(account, "other@example.com")
    assert stale == "Anthropic API returned 401 — re-login required"


@pytest.mark.asyncio
async def test_vault_probe_401_refresh_terminal_sets_exact_stale(monkeypatch):
    """Probe 401 → refresh returns 400 invalid_grant → stale_reason
    reflects the refresh-path terminal reason, not the probe-path one."""
    bg._refresh_backoff_until.clear()
    bg._last_reactive_refresh_at.clear()

    account = _make_account(email="vault@example.com")
    monkeypatch.setattr(
        ac, "read_credentials_for_email",
        lambda email, active_email=None: _fresh_creds(),
    )

    async def fake_probe(token):
        raise _http_error(401)
    monkeypatch.setattr(anthropic_api, "probe_usage", fake_probe)

    async def fake_refresh(rt):
        raise _http_error(400, json_body={"error": "invalid_grant"})
    monkeypatch.setattr(anthropic_api, "refresh_access_token", fake_refresh)

    _, stale = await bg._process_single_account(account, "other@example.com")
    assert stale == "Refresh token rejected — re-login required"


@pytest.mark.asyncio
async def test_vault_probe_401_refresh_transient_no_stale_yet(monkeypatch):
    """Probe 401 + refresh returns transient (below escalation) → no stale_reason
    written this cycle; next cycle will retry per the backoff ladder."""
    bg._refresh_backoff_until.clear()
    bg._refresh_backoff_count.clear()
    bg._last_reactive_refresh_at.clear()

    account = _make_account(email="vault@example.com")
    monkeypatch.setattr(
        ac, "read_credentials_for_email",
        lambda email, active_email=None: _fresh_creds(),
    )

    async def fake_probe(token):
        raise _http_error(401)
    monkeypatch.setattr(anthropic_api, "probe_usage", fake_probe)

    async def fake_refresh(rt):
        raise _http_error(400, json_body={"error": "invalid_request"})
    monkeypatch.setattr(anthropic_api, "refresh_access_token", fake_refresh)

    _, stale = await bg._process_single_account(account, "other@example.com")
    assert stale is None
    assert bg._refresh_backoff_count["vault@example.com"] == 1


@pytest.mark.asyncio
async def test_active_probe_401_unchanged_no_reactive_refresh(monkeypatch):
    """Regression: ACTIVE account probe 401 must still nudge tmux and return
    cached usage.  The reactive-refresh path is vault-only — CLI owns the
    active account's refresh lifecycle."""
    account = _make_account(email="active@example.com")
    monkeypatch.setattr(
        ac, "read_credentials_for_email",
        lambda email, active_email=None: _fresh_creds(),
    )

    async def fake_probe(token):
        raise _http_error(401)
    monkeypatch.setattr(anthropic_api, "probe_usage", fake_probe)

    refresh_calls = []
    async def fake_refresh(rt):
        refresh_calls.append(rt)
        return {}
    monkeypatch.setattr(anthropic_api, "refresh_access_token", fake_refresh)

    nudge_calls = []
    monkeypatch.setattr(bg, "_maybe_nudge_active", lambda e: nudge_calls.append(e))

    _, stale = await bg._process_single_account(account, "active@example.com")
    assert stale is None  # active path returns cached, never writes stale
    assert refresh_calls == []  # refresh was NOT called for active
    assert nudge_calls == ["active@example.com"]


@pytest.mark.asyncio
async def test_active_probe_401_never_reactive_refreshes(monkeypatch):
    """Reinforce: active-account 401 path bypasses reactive refresh entirely.
    CLI owns the active refresh lifecycle; CCSwitch must never rotate the
    standard Keychain entry behind the CLI's back."""
    bg._last_reactive_refresh_at.clear()
    account = _make_account(email="activeonly@example.com")
    monkeypatch.setattr(
        ac, "read_credentials_for_email",
        lambda email, active_email=None: _fresh_creds(),
    )

    async def fake_probe(token):
        raise _http_error(401)
    monkeypatch.setattr(anthropic_api, "probe_usage", fake_probe)

    refresh_calls = []
    async def fake_refresh(rt):
        refresh_calls.append(rt)
        return {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
    monkeypatch.setattr(anthropic_api, "refresh_access_token", fake_refresh)

    monkeypatch.setattr(bg, "_maybe_nudge_active", lambda e: None)

    _, stale = await bg._process_single_account(account, "activeonly@example.com")
    assert stale is None
    assert refresh_calls == []  # STRICT: zero refresh calls on active path
    assert "activeonly@example.com" not in bg._last_reactive_refresh_at


@pytest.mark.asyncio
async def test_vault_probe_401_retry_probe_returns_500_no_stale(monkeypatch):
    """Reactive refresh succeeds, retry-probe returns 500 (not 401).  Should
    NOT stale — bubble up the 500 via existing error path, returning cached."""
    bg._refresh_backoff_until.clear()
    bg._last_reactive_refresh_at.clear()

    account = _make_account(email="vault@example.com")
    monkeypatch.setattr(
        ac, "read_credentials_for_email",
        lambda email, active_email=None: _fresh_creds(),
    )

    probe_calls: list[str] = []
    async def fake_probe(token):
        probe_calls.append(token)
        if len(probe_calls) == 1:
            raise _http_error(401)
        raise _http_error(500)  # retry probe returns transient upstream error
    monkeypatch.setattr(anthropic_api, "probe_usage", fake_probe)

    async def fake_refresh(rt):
        return {"access_token": "at-new", "refresh_token": "rt-new", "expires_in": 3600}
    monkeypatch.setattr(anthropic_api, "refresh_access_token", fake_refresh)
    monkeypatch.setattr(cp, "save_refreshed_vault_token", lambda *a: None)

    _, stale = await bg._process_single_account(account, "other@example.com")
    assert stale is None  # 500 is transient, must NOT stale


@pytest.mark.asyncio
async def test_vault_probe_401_no_refresh_token_marks_stale(monkeypatch):
    """Vault entry has access_token but NO refresh_token.  Cannot refresh.
    Write stale immediately — nothing to recover with."""
    bg._last_reactive_refresh_at.clear()
    account = _make_account(email="vault@example.com")

    creds_no_rt = {"claudeAiOauth": {"accessToken": "at-only", "refreshToken": None}}
    monkeypatch.setattr(
        ac, "read_credentials_for_email",
        lambda email, active_email=None: creds_no_rt,
    )

    async def fake_probe(token):
        raise _http_error(401)
    monkeypatch.setattr(anthropic_api, "probe_usage", fake_probe)

    refresh_calls = []
    async def fake_refresh(rt):
        refresh_calls.append(rt)
        return {}
    monkeypatch.setattr(anthropic_api, "refresh_access_token", fake_refresh)

    _, stale = await bg._process_single_account(account, "other@example.com")
    assert stale == "Anthropic API returned 401 — re-login required"
    assert refresh_calls == []  # no refresh attempt (no token to use)


@pytest.mark.asyncio
async def test_reactive_refresh_cooldown_prevents_herd(monkeypatch):
    """Two probe-401 cycles within 60s: first triggers refresh, second is
    cooldown-skipped.  Prevents thundering-herd on degraded Anthropic."""
    bg._last_reactive_refresh_at.clear()
    bg._refresh_backoff_until.clear()

    account = _make_account(email="vault@example.com")
    monkeypatch.setattr(
        ac, "read_credentials_for_email",
        lambda email, active_email=None: _fresh_creds(),
    )

    # probe always 401 (both original + retry).  If cooldown works, only the
    # first cycle should fire a refresh; the second cycle skips refresh and
    # returns cached usage with no new stale_reason.
    async def fake_probe(token):
        raise _http_error(401)
    monkeypatch.setattr(anthropic_api, "probe_usage", fake_probe)

    refresh_calls = []
    async def counting_refresh(rt):
        refresh_calls.append(1)
        return {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
    monkeypatch.setattr(anthropic_api, "refresh_access_token", counting_refresh)
    monkeypatch.setattr(cp, "save_refreshed_vault_token", lambda *a: None)

    # Run _process_single_account twice in rapid succession.
    await bg._process_single_account(account, "other@example.com")
    await bg._process_single_account(account, "other@example.com")

    assert len(refresh_calls) == 1  # only first cycle refreshed


@pytest.mark.asyncio
async def test_poll_reactive_refresh_and_revalidate_serialize(monkeypatch):
    """Concurrent Revalidate (user click) and poll-loop reactive refresh
    on the same email must NOT both POST the same single-use refresh_token.
    They share get_refresh_lock → second entrant sees first's rotated
    refresh_token."""
    ac._refresh_locks.clear()

    # Use asyncio.Event for deterministic synchronisation rather than sleep.
    first_released = asyncio.Event()
    second_acquired = asyncio.Event()
    enter_times: list[float] = []
    exit_times: list[float] = []

    async def timed_refresh(rt):
        enter_times.append(asyncio.get_event_loop().time())
        if not first_released.is_set():
            # First call: hold the lock until we explicitly release it.
            await asyncio.sleep(0)  # yield so second task can queue on the lock
            await first_released.wait()
            exit_times.append(asyncio.get_event_loop().time())
            return {"access_token": "at", "refresh_token": "rt-new", "expires_in": 3600}
        # Second call (happens only after first_released is set).
        second_acquired.set()
        exit_times.append(asyncio.get_event_loop().time())
        return {"access_token": "at", "refresh_token": "rt-new2", "expires_in": 3600}

    monkeypatch.setattr(anthropic_api, "refresh_access_token", timed_refresh)
    monkeypatch.setattr(cp, "save_refreshed_vault_token", lambda *a: None)

    async def task_a():
        lock = ac.get_refresh_lock("vault@example.com")
        async with lock:
            await bg._refresh_vault_token("vault@example.com", "rt-live-a")

    async def task_b():
        lock = ac.get_refresh_lock("vault@example.com")
        async with lock:
            await bg._refresh_vault_token("vault@example.com", "rt-live-b")

    t1 = asyncio.create_task(task_a())
    # Yield so task_a enters and holds the lock.
    await asyncio.sleep(0)
    t2 = asyncio.create_task(task_b())
    # Release the first task's hold; verify second acquires strictly after.
    first_released.set()
    await asyncio.gather(t1, t2)
    await second_acquired.wait()

    assert len(enter_times) == 2
    # STRICT: second call entered strictly after first exited.
    assert enter_times[1] >= exit_times[0]
```

- [ ] **Step 2: Run, verify fail**

```bash
uv run python -m pytest tests/test_background.py -q -k "vault_probe_401 or active_probe_401 or reactive_refresh_cooldown or poll_reactive_refresh"
```

Expected: ~8 fail (reactive path + cooldown + lock coverage not yet implemented — probe 401 still writes stale immediately per old code), 1 pass (the active-probe regression test should pass on current code).

### Task 2.3 — Implement reactive branch (with cooldown + shared lock)

- [ ] **Step 0: Extend the autouse fixture in `tests/test_background.py`** to clear
  `bg._last_reactive_refresh_at` before and after each test.  Find the existing
  `_wipe_cache_between_tests` fixture (~ line 30) — it currently clears 6 dicts;
  extend to 7.  Without this the test suite would have cross-test pollution once
  the cooldown dict is live.

```python
@pytest.fixture(autouse=True)
async def _wipe_cache_between_tests():
    bg._backoff_until.clear()
    bg._backoff_count.clear()
    bg._refresh_backoff_until.clear()
    bg._refresh_backoff_count.clear()
    bg._refresh_backoff_first_failure_at.clear()
    bg._last_reactive_refresh_at.clear()     # NEW
    await _cache._usage_cache.clear()
    yield
    bg._backoff_until.clear()
    bg._backoff_count.clear()
    bg._refresh_backoff_until.clear()
    bg._refresh_backoff_count.clear()
    bg._refresh_backoff_first_failure_at.clear()
    bg._last_reactive_refresh_at.clear()     # NEW
    await _cache._usage_cache.clear()
```

(The integration-test fixture in M5 already clears `_last_reactive_refresh_at`
— it was added during wave-1 revision; only the main `test_background.py`
autouse needs updating here.)

- [ ] **Step 1: Add module-level cooldown state** in `backend/background.py` near the other module-level dicts:

```python
# Per-account cooldown between reactive refresh attempts.  Prevents N
# concurrent poll cycles from firing N refresh POSTs when Anthropic
# is briefly returning 401 to all requests (degraded state).
_REACTIVE_REFRESH_COOLDOWN_SECONDS = 60
_last_reactive_refresh_at: dict[str, float] = {}
```

- [ ] **Step 2: Extend `forget_account_state`** to also clear this dict:

```python
def forget_account_state(email: str) -> None:
    _backoff_until.pop(email, None)
    _backoff_count.pop(email, None)
    _last_nudge_at.pop(email, None)
    _refresh_backoff_until.pop(email, None)
    _refresh_backoff_count.pop(email, None)
    _refresh_backoff_first_failure_at.pop(email, None)
    _last_reactive_refresh_at.pop(email, None)  # NEW
```

- [ ] **Step 3: Modify `_process_single_account` probe-error handler** at `backend/background.py:319-349`.  Replace the vault-branch of the 401 handler with:

```python
            if status == 401:
                if is_active:
                    # Active-account 401: unchanged.  Nudge tmux + cached usage.
                    _maybe_nudge_active(account.email)
                    cached = await cache.get_usage_async(account.email)
                    cached_dict = cached if isinstance(cached, dict) else {}
                    cached_ti = await cache.get_token_info_async(account.email) or {}
                    try:
                        flat = build_usage(cached_dict, cached_ti)
                        flat_dict = flat.model_dump() if flat else {}
                    except Exception as _bu_err:
                        logger.warning(
                            "build_usage failed for %s: %s",
                            account.email, _bu_err,
                        )
                        flat_dict = {}
                    return {
                        "id": account.id,
                        "email": account.email,
                        "usage": flat_dict,
                        "error": cached_dict.get("error"),
                    }, account.stale_reason

                # Vault 401: try a reactive refresh + retry probe ONCE before
                # writing stale_reason.  The access_token may be dead server-
                # side (rotation, idle-invalidation) but the refresh_token
                # may still be live.  Self-heal instead of giving up.
                if account.stale_reason:
                    # Already stale — don't try again this cycle.
                    new_stale_reason = account.stale_reason
                    raise
                refresh_token = cp.refresh_token_of(credentials)
                if not refresh_token:
                    new_stale_reason = "Anthropic API returned 401 — re-login required"
                    raise
                if _refresh_backoff_until.get(account.email, 0.0) > time.monotonic():
                    # Already in a refresh-backoff window — don't hammer.
                    logger.debug(
                        "Vault 401 for %s but refresh-backoff active; returning cached",
                        account.email,
                    )
                    cached = await cache.get_usage_async(account.email) or {}
                    return {
                        "id": account.id,
                        "email": account.email,
                        "usage": (build_usage(cached, token_info).model_dump()
                                  if build_usage(cached, token_info) else {}),
                        "error": cached.get("error"),
                    }, None

                # Thundering-herd guard: if we recently attempted a reactive
                # refresh for this email, don't hammer Anthropic (degraded-
                # state 401s to all callers will otherwise fan out into N
                # refresh POSTs per poll cycle).
                last_reactive = _last_reactive_refresh_at.get(account.email, 0.0)
                if time.monotonic() - last_reactive < _REACTIVE_REFRESH_COOLDOWN_SECONDS:
                    logger.debug(
                        "Vault 401 for %s but reactive-refresh cooldown active",
                        account.email,
                    )
                    cached = await cache.get_usage_async(account.email) or {}
                    return {
                        "id": account.id,
                        "email": account.email,
                        "usage": (build_usage(cached, token_info).model_dump()
                                  if build_usage(cached, token_info) else {}),
                        "error": cached.get("error"),
                    }, None
                _last_reactive_refresh_at[account.email] = time.monotonic()

                # Vault 401 reactive refresh — under the shared lock so
                # we don't race a concurrent Revalidate on the same email.
                lock = get_refresh_lock(account.email)
                async with lock:
                    try:
                        new_tokens = await _refresh_vault_token(account.email, refresh_token)
                    except _RefreshTerminal as term_err:
                        # Helper carries stale_reason on .reason.
                        new_stale_reason = term_err.reason or "Refresh token invalid — re-login required"
                        raise
                    except (httpx.HTTPStatusError, httpx.RequestError):
                        # Transient below threshold — DO NOT stale.  Return cached.
                        cached = await cache.get_usage_async(account.email) or {}
                        return {
                            "id": account.id,
                            "email": account.email,
                            "usage": (build_usage(cached, token_info).model_dump()
                                      if build_usage(cached, token_info) else {}),
                            "error": cached.get("error"),
                        }, None

                # Retry probe with the fresh access_token.
                try:
                    usage = await anthropic_api.probe_usage(new_tokens["access_token"])
                except httpx.HTTPStatusError as retry_err:
                    if retry_err.response.status_code == 401:
                        # Fresh token still 401 — genuinely dead upstream.
                        new_stale_reason = "Anthropic API returned 401 — re-login required"
                        raise
                    raise  # Other status (e.g. 500) bubbles to outer handler.
                # Fresh token succeeded — fall through to the success path.
                await cache.set_usage(account.email, usage)
                _backoff_until.pop(account.email, None)
                _backoff_count.pop(account.email, None)
                # Recovery succeeded — clear the cooldown so a GENUINELY
                # NEW 401 on the next poll cycle is treated as a fresh
                # event, not falsely 60s-skipped as if we already tried.
                _last_reactive_refresh_at.pop(account.email, None)
                flat = build_usage(usage, token_info) if usage else None
                flat_dict = flat.model_dump() if flat else {}
                return {
                    "id": account.id,
                    "email": account.email,
                    "usage": flat_dict,
                    "error": None,
                }, None
```

Note: the existing `elif status == 429:` and `else: raise` branches stay unchanged.

- [ ] **Step 4: Re-enable M1-skipped tests** that exercise end-to-end poll-loop behavior (the ones marked `@pytest.mark.skip(reason="M2 will re-enable via reactive path")`).  Remove the skip decorators.

- [ ] **Step 5: Run the M2 test subset — expect all pass**

```bash
uv run python -m pytest tests/test_background.py -q -k "vault_probe_401 or active_probe_401 or reactive_refresh_cooldown or poll_reactive_refresh"
```

- [ ] **Step 6: Full suite** — expect 207 + M1's new 7 + M2's new ~10 ≈ 224.  (No regressions.)

```bash
uv run python -m pytest tests/ -q
```

- [ ] **Step 7: Commit**

```bash
git add backend/background.py backend/services/account_service.py backend/routers/accounts.py tests/test_background.py tests/test_account_service.py
git commit -m "$(cat <<'EOF'
feat(background+refresh): reactive refresh on vault probe 401 under shared lock (M2)

Combines the reactive-refresh path with the lock-rename work from
the original M3.  These two concerns ship together because landing
reactive refresh alone reintroduces the Revalidate-vs-poll race
the April-15 plan fixed.

Changes:
1. Renamed _revalidate_locks → _refresh_locks; exposed
   get_refresh_lock(email) + forget_refresh_lock(email).  Now every
   code path that calls anthropic_api.refresh_access_token on a
   vault entry acquires the same per-email lock:
     - revalidate_account (user-triggered via API)
     - poll-loop reactive refresh (this commit)
     - swap step 0.5 (M3, next commit)
2. Added module-level _last_reactive_refresh_at cooldown dict
   (_REACTIVE_REFRESH_COOLDOWN_SECONDS = 60).  Prevents thundering-
   herd refresh POSTs when Anthropic briefly 401s all requests.
3. Added reactive-refresh branch in _process_single_account: on
   vault 401, acquire shared lock, call _refresh_vault_token, retry
   probe once with fresh access_token.  Only writes stale_reason if
   RETRY also 401s (genuinely dead upstream).

Failure modes handled:
- refresh terminal (invalid_grant / Keychain persist failed) →
  stale with err.reason from _RefreshTerminal
- refresh transient (below escalation) → no stale, cached usage
- refresh network error → same transient ladder
- retry-probe 401 → stale "Anthropic API returned 401 — re-login
  required"
- retry-probe 500 or other non-401 → bubble to outer handler, no
  stale (consistent with existing error path)
- no refresh_token in vault entry → immediate stale (nothing to
  refresh with)
- cooldown active → skip refresh, return cached

Active account handling is unchanged: probe 401 still fires tmux
nudge and returns cached usage; CCSwitch never refreshes the
active account (CLI owns that lifecycle).

~10 new tests cover the outcome classes + cooldown + lock-
serialisation.  Re-enables the M1-skipped downstream tests.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Milestone 3 — Swap step 0.5: refresh on promotion

**Files:**
- Modify: `backend/services/account_service.py:_swap_to_account_locked` (add step between 1 and 2)
- Modify: `backend/services/credential_provider.py` (add `already_locked` parameter to `save_refreshed_vault_token`)
- Modify: `backend/background.py` (thread `already_locked` parameter through `_refresh_vault_token`)
- Test: `tests/test_account_service.py`

> **RLock reentrance hazard.**  `swap_to_account` already holds
> `cp._credential_lock` (threading.RLock) on the swap thread.  A naïve
> implementation would call `asyncio.run(bg._refresh_vault_token(...))`
> from inside the swap — which in turn does `asyncio.to_thread(
> cp.save_refreshed_vault_token, ...)` to run the Keychain write on a
> different worker thread.  RLock is per-thread reentrant, so the
> worker thread's `with cp._credential_lock` call BLOCKS indefinitely
> waiting for the swap thread to release — classic deadlock.
>
> Fix: refactor `cp.save_refreshed_vault_token` to take an
> `already_locked: bool = False` parameter and skip the internal
> `with cp._credential_lock` when True.  Thread the same flag through
> `bg._refresh_vault_token(email, rt, *, already_locked=False)`.  The
> swap path passes `already_locked=True`; all other callers (poll-loop
> reactive, revalidate) pass the default False.  This is cleaner than
> wrapping `asyncio.run` with a release/re-acquire dance around the
> RLock, and keeps the single-owner lifecycle of the lock clear.

### Task 3.1 — Add swap-time refresh tests

- [ ] **Step 1: Append to `tests/test_account_service.py`**:

```python
@pytest.mark.asyncio
async def test_swap_refreshes_incoming_before_promotion(monkeypatch):
    """Swap step 0.5: before promoting a vault token to standard, attempt
    one refresh under the shared lock.  Ensures the CLI gets a freshly-
    minted access_token and doesn't immediately hit 401 on its first call."""
    # ... swap_to_account is sync (runs under threading.Lock) so the test
    # needs to call it via asyncio.to_thread in the harness ...

    stale_vault = {
        "claudeAiOauth": {
            "accessToken": "at-old",
            "refreshToken": "rt-live",
            # expiresAt is in the past — ripe for refresh.
            "expiresAt": int(time.time() * 1000) - 60_000,
        },
        "oauthAccount": {"emailAddress": "vault@example.com"},
        "userID": "u",
    }
    monkeypatch.setattr(ac.cp, "read_vault",
                        lambda email: stale_vault if email == "vault@example.com" else None)

    # refresh returns fresh tokens
    async def fake_refresh(rt):
        return {"access_token": "at-FRESH", "refresh_token": "rt-FRESH", "expires_in": 3600}
    monkeypatch.setattr(ac.anthropic_api, "refresh_access_token", fake_refresh)

    # standard is empty (first-ever swap)
    monkeypatch.setattr(ac.cp, "read_standard", lambda: {})

    written_standard = {}
    def fake_write_standard(blob):
        written_standard.update(blob)
        return True
    monkeypatch.setattr(ac.cp, "write_standard", fake_write_standard)
    monkeypatch.setattr(ac.cp, "write_vault", lambda email, blob: True)
    monkeypatch.setattr(ac, "_rewrite_claude_json_identity", lambda b: None)
    monkeypatch.setattr(ac, "_atomic_write_json", lambda p, b: None)

    def fake_save_refresh(email, access_token, expires_at=None, refresh_token=None, *, already_locked=False):
        stale_vault["claudeAiOauth"] = {
            "accessToken": access_token, "refreshToken": refresh_token, "expiresAt": expires_at,
        }
    monkeypatch.setattr(ac.cp, "save_refreshed_vault_token", fake_save_refresh)

    result = await asyncio.to_thread(ac.swap_to_account, "vault@example.com")
    assert result["target_email"] == "vault@example.com"
    # The standard entry must have the FRESH token, not at-old.
    assert written_standard.get("claudeAiOauth", {}).get("accessToken") == "at-FRESH"


@pytest.mark.asyncio
async def test_swap_proceeds_when_incoming_refresh_transient(monkeypatch):
    """If the swap-time refresh fails transiently (network, 5xx), swap still
    proceeds with the stored tokens.  Log warning, don't block the user's
    switch request."""
    stale_vault = {
        "claudeAiOauth": {
            "accessToken": "at-stored",
            "refreshToken": "rt-live",
            "expiresAt": int(time.time() * 1000) + 3600_000,
        },
        "oauthAccount": {"emailAddress": "vault@example.com"},
        "userID": "u",
    }
    monkeypatch.setattr(ac.cp, "read_vault",
                        lambda email: stale_vault if email == "vault@example.com" else None)

    async def fake_refresh(rt):
        raise httpx.ConnectError("simulated network failure")
    monkeypatch.setattr(ac.anthropic_api, "refresh_access_token", fake_refresh)

    monkeypatch.setattr(ac.cp, "read_standard", lambda: {})
    written_standard = {}
    monkeypatch.setattr(ac.cp, "write_standard",
                        lambda b: written_standard.update(b) or True)
    monkeypatch.setattr(ac.cp, "write_vault", lambda email, blob: True)
    monkeypatch.setattr(ac, "_rewrite_claude_json_identity", lambda b: None)
    monkeypatch.setattr(ac, "_atomic_write_json", lambda p, b: None)

    result = await asyncio.to_thread(ac.swap_to_account, "vault@example.com")
    assert result["target_email"] == "vault@example.com"
    # Swap proceeded with STORED token (refresh failed transiently).
    assert written_standard.get("claudeAiOauth", {}).get("accessToken") == "at-stored"


@pytest.mark.asyncio
async def test_swap_aborts_when_incoming_refresh_terminal(monkeypatch):
    """If the swap-time refresh returns terminal (invalid_grant etc.),
    SwapError is raised and the standard entry is NOT overwritten.
    User sees clear "re-login first" error."""
    stale_vault = {
        "claudeAiOauth": {
            "accessToken": "at-stored",
            "refreshToken": "rt-dead",
            "expiresAt": int(time.time() * 1000) - 60_000,
        },
        "oauthAccount": {"emailAddress": "vault@example.com"},
        "userID": "u",
    }
    monkeypatch.setattr(ac.cp, "read_vault",
                        lambda email: stale_vault if email == "vault@example.com" else None)

    async def fake_refresh(rt):
        raise _http_error(400, json_body={"error": "invalid_grant"})
    monkeypatch.setattr(ac.anthropic_api, "refresh_access_token", fake_refresh)

    written_standard = {}
    def fake_write_standard(blob):
        written_standard.update(blob)
        return True
    monkeypatch.setattr(ac.cp, "write_standard", fake_write_standard)
    monkeypatch.setattr(ac.cp, "read_standard", lambda: {})
    monkeypatch.setattr(ac.cp, "write_vault", lambda email, blob: True)
    monkeypatch.setattr(ac, "_rewrite_claude_json_identity", lambda b: None)
    monkeypatch.setattr(ac, "_atomic_write_json", lambda p, b: None)

    with pytest.raises(ac.SwapError) as excinfo:
        await asyncio.to_thread(ac.swap_to_account, "vault@example.com")
    assert "re-login" in str(excinfo.value).lower()
    # STRICT: standard Keychain entry NOT overwritten — user stays on previous active.
    assert written_standard == {}
```

### Task 3.2 — Refactor `save_refreshed_vault_token` + `_refresh_vault_token` to accept `already_locked`

- [ ] **Step 1: In `backend/services/credential_provider.py`**, extend `save_refreshed_vault_token`'s signature and internal lock acquisition:

```python
def save_refreshed_vault_token(
    email: str,
    access_token: str,
    expires_at: int | None = None,
    refresh_token: str | None = None,
    *,
    already_locked: bool = False,
) -> None:
    """Persist a refreshed vault token tuple.

    When ``already_locked`` is True, the caller is already holding
    ``_credential_lock`` on THIS thread — skip the internal re-acquire
    so we don't deadlock against ourselves on a separate worker thread
    when the caller is an `asyncio.run(...)` wrapped in the RLock-
    holding swap path.
    """
    if already_locked:
        _save_refreshed_vault_token_locked(email, access_token, expires_at, refresh_token)
    else:
        with _credential_lock:
            _save_refreshed_vault_token_locked(email, access_token, expires_at, refresh_token)
```

(Hoist the current body into `_save_refreshed_vault_token_locked`.)

- [ ] **Step 2: In `backend/background.py`**, extend `_refresh_vault_token` to accept + forward the flag:

```python
async def _refresh_vault_token(
    email: str,
    refresh_token: str,
    *,
    already_locked: bool = False,
) -> dict:
    # ...(docstring updated to mention already_locked)...
    # ...all logic unchanged except the one line that calls
    #    save_refreshed_vault_token — now passes already_locked=already_locked.
    #    Note the keyword arg name is `expires_at` (matches the real
    #    cp.save_refreshed_vault_token signature), not `expires_at_ms`;
    #    the internal variable name `new_expires_at_ms` stays for
    #    descriptiveness.
    for attempt in range(3):
        try:
            await asyncio.to_thread(
                cp.save_refreshed_vault_token,
                email, new_token, expires_at=new_expires_at_ms,
                refresh_token=new_refresh,
                already_locked=already_locked,
            )
            # ...
```

### Task 3.3 — Implement swap step 0.5

- [ ] **Step 1: Modify `_swap_to_account_locked` in `account_service.py`**, insert after the step-1 validations (after line 199, "Refuse to proceed if either is missing"):

```python
    # ── Step 0.5: refresh incoming tokens on promotion ────────────────────
    # Minimises the window where a newly-promoted account has an
    # access_token near or past expiry, forcing the CLI to stall on its
    # first API call.  Shares the per-email refresh lock with the poll
    # loop's reactive path + revalidate_account, so no race on single-
    # use refresh_tokens.  Uses asyncio from a sync function via a
    # small helper — swap_to_account is called from both sync (tests)
    # and async (router) contexts so we always go through a short-lived
    # event loop.
    incoming = _refresh_incoming_on_promotion(target_email, incoming)
```

- [ ] **Step 2: Add the helper** at module level in `account_service.py`:

```python
def _refresh_incoming_on_promotion(email: str, incoming: dict) -> dict:
    """Attempt one refresh on the vault entry's refresh_token.  Returns
    the updated ``incoming`` blob (possibly with fresh access + refresh
    tokens) or the original unchanged if refresh failed transiently.
    Raises ``SwapError`` on terminal failure (refresh_token genuinely
    dead).

    ⚠ Lock-ordering note: this function is called from inside
    ``_swap_to_account_locked`` which holds ``cp._credential_lock``
    (threading.RLock) on the current thread.  Inside
    ``_refresh_vault_token`` we call ``asyncio.to_thread(
    cp.save_refreshed_vault_token, ...)`` which, in the default path,
    acquires ``cp._credential_lock`` synchronously on a WORKER thread.
    RLock re-entrance is per-thread, so that worker's acquire would
    block on the swap thread's hold — classic deadlock.

    Workaround: pass ``already_locked=True`` all the way through to
    ``cp.save_refreshed_vault_token`` so the worker skips its internal
    acquire.  We're not race-vulnerable because the RLock is already
    held on the swap thread for the entire duration of this call.
    """
    from .. import background as bg

    rt = cp.refresh_token_of(incoming)
    if not rt:
        return incoming

    async def _do_refresh():
        lock = get_refresh_lock(email)
        async with lock:
            return await bg._refresh_vault_token(email, rt, already_locked=True)

    try:
        new = asyncio.run(_do_refresh())
    except bg._RefreshTerminal as term_err:
        reason = term_err.reason or "refresh_token invalid"
        raise SwapError(
            f"Cannot activate {email}: {reason} — click Re-login first"
        )
    except Exception as e:
        # Transient / network — proceed with stored tokens; CLI will
        # refresh on its first call.  Log so ops can see the event.
        logger.warning(
            "Swap-time refresh for %s failed transiently (%s: %s); "
            "proceeding with stored tokens",
            email, type(e).__name__, e,
        )
        return incoming

    # Success — fold fresh tokens into the incoming blob.
    inner = dict(incoming.get("claudeAiOauth") or {})
    inner["accessToken"] = new["access_token"]
    if new.get("refresh_token"):
        inner["refreshToken"] = new["refresh_token"]
    if new.get("expires_at_ms"):
        inner["expiresAt"] = new["expires_at_ms"]
    fresh_incoming = dict(incoming)
    fresh_incoming["claudeAiOauth"] = inner
    return fresh_incoming
```

**Asyncio note:** `asyncio.run` from a sync function requires no existing event loop.  `swap_to_account` is called from: (a) router handlers via `asyncio.to_thread(ac.swap_to_account, …)` — the to_thread spawns a thread, no loop conflict; (b) tests that either run it directly (sync) or via to_thread.  In case (a), the thread has no running loop, so `asyncio.run` is safe.  In case (b) with direct sync call, same thing.

If this pattern causes test flakes, fallback: take a `loop` parameter on the helper and require callers to provide one via `asyncio.get_event_loop()`.

- [ ] **Step 3: Run swap tests**

```bash
uv run python -m pytest tests/test_account_service.py -q -k "swap_refreshes or swap_proceeds or swap_aborts"
```

- [ ] **Step 4: Full suite**

```bash
uv run python -m pytest tests/ -q
```

- [ ] **Step 5: Commit**

```bash
git add backend/services/account_service.py backend/services/credential_provider.py backend/background.py tests/test_account_service.py
git commit -m "$(cat <<'EOF'
feat(swap): refresh incoming tokens on promotion (M3)

swap_to_account step 0.5 (new): before promoting a vault entry to
the standard Keychain slot, attempt one refresh_access_token call
under the shared refresh lock (M2).  On success, write the fresh
tokens back to the vault AND use them as the incoming blob for
promotion — CLI starts its first post-swap API call with a fresh
access_token that has ~1 hour of runway.

Failure modes:
- terminal (invalid_grant / 400) → SwapError, standard entry NOT
  overwritten.  User must Re-login first.
- transient (network, 5xx, below-threshold 400 transient) → warning
  logged, swap proceeds with stored tokens.  CLI will refresh on
  its first call as it always does.

RLock reentrance fix: cp.save_refreshed_vault_token now takes an
already_locked=False keyword parameter; bg._refresh_vault_token
threads the same flag through.  The swap path passes True so the
asyncio.to_thread(save_refreshed_vault_token) worker thread skips
its internal cp._credential_lock acquire (which would otherwise
deadlock against the swap thread's hold — RLock is per-thread).

Closes the "CLI hits 401 on first post-swap keypress" UX gap.

3 new tests cover the three outcome classes (success + fresh tokens
promoted, transient + stored tokens promoted, terminal + swap
aborted + standard entry untouched).

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Milestone 4 — Checkpoint omits stale `expiresAt`

**Files:**
- Modify: `backend/services/account_service.py:_merge_checkpoint` (lines ~256-280)
- Test: `tests/test_account_service.py`

### Task 4.1 — Test that checkpointed vault entries have no expiresAt (nested + legacy shapes)

- [ ] **Step 1: Append tests**:

```python
def test_merge_checkpoint_strips_stale_expires_at(monkeypatch):
    """On swap step 2 (checkpoint), do NOT copy the CLI's expiresAt
    claim into the vault.  The CLI's cached timestamp may be stale
    relative to Anthropic's server state; copying it into vault
    propagates the lie.  The next successful _refresh_vault_token
    writes a fresh expiresAt instead."""
    fresh_standard = {
        "claudeAiOauth": {
            "accessToken": "at", "refreshToken": "rt",
            "expiresAt": 99999999999,  # bogus claim
        },
        "oauthAccount": {"emailAddress": "out@example.com"},
        "userID": "u",
    }
    previous_vault = {"oauthAccount": {"emailAddress": "out@example.com"}, "userID": "u"}
    monkeypatch.setattr(ac.cp, "read_vault", lambda email: previous_vault)

    merged = ac._merge_checkpoint("out@example.com", fresh_standard)
    inner = merged.get("claudeAiOauth", {})
    assert inner.get("accessToken") == "at"
    assert inner.get("refreshToken") == "rt"
    # expiresAt is absent — will be set by the next refresh.
    assert "expiresAt" not in inner


def test_merge_checkpoint_strips_expires_at_from_legacy_shape(monkeypatch):
    """Some old standard entries have top-level expiresAt (not nested).
    Strip it from both the nested and legacy paths."""
    fresh_standard = {
        "accessToken": "at", "refreshToken": "rt",
        "expiresAt": 99999999999,  # legacy root-level placement
        "subscriptionType": "max",
    }
    merged = ac._merge_checkpoint("out@example.com", fresh_standard)
    # Merged should have NO expiresAt at any level.
    assert "expiresAt" not in merged
    assert "expiresAt" not in merged.get("claudeAiOauth", {})
```

### Task 4.2 — Implement the strip

- [ ] **Step 1: Modify `_merge_checkpoint`** at `account_service.py` around line 260.  Ensure both the nested and legacy (root-level) shapes drop `expiresAt`:

```python
    nested = fresh_standard.get("claudeAiOauth")
    if isinstance(nested, dict):
        # Strip expiresAt — it's a CLI-authored claim that can be stale
        # relative to Anthropic's server state.  The next successful
        # refresh (via _refresh_vault_token in M1) writes a fresh one.
        stripped = {k: v for k, v in nested.items() if k != "expiresAt"}
        merged["claudeAiOauth"] = stripped
    else:
        # Legacy root-level shape (no claudeAiOauth wrapper).  Same
        # expiresAt-strip applies here — the CLI-authored timestamp
        # is a client-side claim that we don't want propagated.
        token_fields = {
            k: fresh_standard[k]
            for k in ("accessToken", "refreshToken", "subscriptionType")  # expiresAt removed
            if k in fresh_standard
        }
        # ...rest unchanged; ensure the top-level merged dict does NOT
        # carry a root-level expiresAt from fresh_standard either.
        merged.pop("expiresAt", None)
```

- [ ] **Step 2: Run tests**

```bash
uv run python -m pytest tests/test_account_service.py -q -k merge_checkpoint_strips
```

- [ ] **Step 3: Full suite**

```bash
uv run python -m pytest tests/ -q
```

- [ ] **Step 4: Commit**

```bash
git add backend/services/account_service.py tests/test_account_service.py
git commit -m "$(cat <<'EOF'
fix(checkpoint): strip stale expiresAt from standard→vault copy (M4)

_merge_checkpoint previously copied the CLI's expiresAt claim from
the standard Keychain entry into the vault blob during swap step 2.
Empirically the CLI's expiresAt is a claim-at-write-time that can
be stale relative to Anthropic's server state by the time we
checkpoint (server may have rotated the token between the CLI's
last use and our swap).  Propagating that lie into the vault meant
the poll loop trusted an invalid expiry for up to an hour.

Strip it — from BOTH the nested claudeAiOauth shape (modern) AND
the root-level shape (legacy builds).  The next successful
_refresh_vault_token (from M1) will write a fresh expiresAt based
on the response's expires_in field.

Two regression tests pin the contract: after checkpoint the vault's
claudeAiOauth has no expiresAt key, and the legacy shape also has
no root-level expiresAt.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Milestone 5 — Docs sync + integration test + final holistic review + PR

**Files:**
- Modify: `CLAUDE.md` (Key data flow point 3)
- Modify: `docs/superpowers/specs/2026-04-15-vault-swap-architecture.md` (new §9.11)
- Modify: `README.md` (Troubleshooting + ToS note)
- Create: `tests/test_integration_reactive_refresh.py`

### Task 5.1 — CLAUDE.md update

- [ ] **Step 1: In `CLAUDE.md`**, replace the current Vault-refresh bullet under Key data flow step 3 (lines 188-202):

```
   - Vault-account probe returning 401 triggers a reactive refresh:
     call `anthropic_api.refresh_access_token`, persist fresh tokens
     via `cp.save_refreshed_vault_token`, retry the probe ONCE with
     the new access_token.  If retry succeeds → usage reported as
     normal.  If refresh returns terminal (401 or 400 with a body
     `error` in the terminal set — RFC 6749 §5.2 + Anthropic codes
     `invalid_request_error` / `authentication_error`) → mark
     `stale_reason = "Refresh token {revoked,rejected} — re-login
     required"`.  If refresh returns transient OR the retry-probe
     still 401s → no stale_reason this cycle; existing transient
     ladder handles retries.
   - Vault-refresh is NEVER triggered proactively.  The `expires_at`
     claim from the CLI's last write is advisory, not authoritative;
     proactive refresh-before-expiry generated rotation events that
     increased broken-chain exposure to Anthropic's server-side
     reuse-detection.  Aligned with OAuth 2.1 RTR best practices:
     refresh only on demand.  On-demand = probe 401 (reactive) OR
     swap step 0.5 (promotion refresh).
```

### Task 5.2 — Spec §9.11

- [ ] **Step 1: Append to `docs/superpowers/specs/2026-04-15-vault-swap-architecture.md`** after §9.10:

```
### 9.11 Reactive-only refresh policy

Pre-April-16 CCSwitch proactively refreshed vault tokens when
`now > expires_at - 20min`.  Empirically (see incident trail in
the plan `2026-04-16-reactive-vault-refresh.md`), this pattern
generated one rotation event per idle vault account per hour.
Each rotation is a potential broken-chain trigger for Anthropic's
server-side single-use-refresh-token reuse detection — any client/
server state divergence (network blip mid-persist, partial response,
async Keychain write vs server-commit race) invalidates the entire
token family for that user session.  Observed symptom: multiple
idle vault accounts going phantom-stale simultaneously within 1-2
hours of server restart (all-family revocation from one broken
rotation).

Post-fix policy (per OAuth 2.1 RTR / Auth0 guidance): **refresh
only on demand, never proactively.**  The three demand triggers
after this change are:

1. **Probe 401 (reactive).**  `_process_single_account` vault path:
   on a 401 from `/v1/messages` probe, call `_refresh_vault_token`
   once.  On success, retry the probe with the fresh access_token.
   On retry-success: no stale_reason.  On retry-401: mark
   `"Anthropic API returned 401 — re-login required"`.  On refresh
   terminal: exact refresh-path reason.  On refresh transient: no
   stale, existing ladder handles retries.

2. **Swap step 0.5 (promotion).**  Before `_swap_to_account_locked`
   writes to the standard Keychain entry, attempt one refresh on
   the incoming vault's refresh_token.  Ensures the CLI starts the
   newly-active account with fresh tokens (no 401 on first keypress).
   Terminal refresh failure → `SwapError`, swap aborts.  Transient
   → proceed with stored tokens, CLI self-refreshes.

3. **User-triggered Revalidate.**  Unchanged from the M1-M6 feature
   in the previous plan.  Now shares the same per-email refresh
   lock (`get_refresh_lock`) with the reactive path + swap step 0.5.

All three paths acquire `account_service.get_refresh_lock(email)`
before calling `anthropic_api.refresh_access_token`.  This forbids
two concurrent refresh attempts on the same email (Anthropic's
single-use refresh_tokens would have the loser return 400
invalid_grant and overwrite the winner's success — the empirical
"cleared then stale again within 15s" symptom).

Checkpoint (§2.4 step 2) no longer copies the CLI's `expiresAt`
into the vault.  That field is a client-side claim that can be
stale relative to Anthropic; propagating it meant the poll loop
might trust an invalid expiry for up to an hour.  The next
successful refresh writes a fresh `expiresAt`; until then, the
vault entry has no claim about the token's lifetime (truthfully).
```

### Task 5.3 — README Troubleshooting + ToS note

- [ ] **Step 1: Update the "Account card shows 'Refresh token rejected'" block** to mention that phantom-stale from broken-chain RTR is now self-healing via the reactive path — user should only see the Re-login prompt when the refresh_token itself is genuinely revoked.

- [ ] **Step 2: Add a ToS transparency note to README.md's "Project Status" or "Prior art" section**:

```
Note: CCSwitch operates strictly as credential storage rotation — it does not
proxy, intercept, or redirect API traffic.  Under Anthropic's Feb 2026 ToS
clarification ("OAuth tokens from subscription plans may not be used in
third-party tools"), CCSwitch stays architecturally safe because the traffic
is generated by the native Claude Code CLI using the native Keychain entry.
However, Anthropic can server-side invalidate tokens at their discretion; if
terms tighten further or server-side fingerprinting flags this usage pattern,
tokens may be revoked.  Evaluate your organisation's risk tolerance.
```

### Task 5.4 — End-to-end integration test

- [ ] **Step 1: Create `tests/test_integration_reactive_refresh.py`**:

```python
"""End-to-end: the motivating phantom-stale scenario is now self-healing.

Scenario: idle vault account's access_token gets invalidated server-side
before its claimed expiry (Anthropic rotation).  Pre-fix behavior:
poll → probe 401 → stale_reason, demand re-login.  Post-fix: poll →
probe 401 → _refresh_vault_token → retry probe → success, no stale.
"""
import asyncio, time
from unittest.mock import AsyncMock, MagicMock
import httpx
import pytest

from backend import background as bg
from backend.services import anthropic_api, account_service as ac
from backend.services import credential_provider as cp


@pytest.fixture(autouse=True)
def _clear_state():
    bg._refresh_backoff_until.clear()
    bg._refresh_backoff_count.clear()
    bg._refresh_backoff_first_failure_at.clear()
    bg._last_reactive_refresh_at.clear()
    ac._refresh_locks.clear()
    yield
    bg._refresh_backoff_until.clear()
    bg._refresh_backoff_count.clear()
    bg._refresh_backoff_first_failure_at.clear()
    bg._last_reactive_refresh_at.clear()
    ac._refresh_locks.clear()


@pytest.mark.asyncio
async def test_phantom_stale_scenario_self_heals(monkeypatch):
    """Reproduce the April 16 incident.  Vault account's access_token
    is dead server-side but refresh_token is live.  Poll cycle should
    auto-recover without stale_reason."""
    from backend.models import Account
    account = Account(
        id=1, email="vault@example.com", enabled=True, priority=0,
        threshold_pct=90, stale_reason=None,
    )

    # Creds: stale access_token but live refresh_token.  expiresAt claims
    # far future — pre-fix would NOT have refreshed proactively.
    monkeypatch.setattr(
        ac, "read_credentials_for_email",
        lambda email, active_email=None: {
            "claudeAiOauth": {
                "accessToken": "at-dead",
                "refreshToken": "rt-live",
                "expiresAt": int(time.time() * 1000) + 7200_000,
            },
        },
    )

    probe_calls = []
    async def fake_probe(token):
        probe_calls.append(token)
        if token == "at-dead":
            raise _make_http_error(401)
        return {"five_hour": {"utilization": 0.12}}
    monkeypatch.setattr(anthropic_api, "probe_usage", fake_probe)

    async def fake_refresh(rt):
        assert rt == "rt-live"
        return {"access_token": "at-fresh", "refresh_token": "rt-fresh", "expires_in": 3600}
    monkeypatch.setattr(anthropic_api, "refresh_access_token", fake_refresh)

    monkeypatch.setattr(cp, "save_refreshed_vault_token", lambda *a: None)

    entry, stale = await bg._process_single_account(account, "other@example.com")
    assert stale is None, "Reactive refresh should have self-healed"
    assert probe_calls == ["at-dead", "at-fresh"]
    assert entry["usage"]["five_hour_pct"] == 12
```

(Helper `_make_http_error` — reuse the existing one from test_background.py; import or re-copy.)

- [ ] **Step 2: Run**

```bash
uv run python -m pytest tests/test_integration_reactive_refresh.py -q
uv run python -m pytest tests/ -q
```

Full suite must be all-green.

### Task 5.5 — Holistic final review

- [ ] **Step 1: Dispatch 3 parallel validation agents** — see "Validation protocol" section.

### Task 5.6 — PR

- [ ] **Step 1: Since `main` is protected**, follow the PR flow:

```bash
branch="fix/reactive-vault-refresh-$(date +%s)"
git checkout -b "$branch"
git push -u origin "$branch"
gh pr create --base main --head "$branch" \
  --title "Reactive vault refresh (OAuth 2.1 RTR alignment)" \
  --body "$(cat <<'EOF'
## Summary
Remove proactive vault-token refresh (the `expires_at - 20min` gate);
add reactive refresh-on-probe-401 + refresh-on-promotion + unified
per-email refresh lock + checkpoint expiresAt strip.

Aligns CCSwitch with OAuth 2.1 Refresh-Token Rotation best practice:
refresh only on demand, not periodically.  Eliminates the class of
phantom-stale incidents caused by broken-chain rotation events on
idle vault accounts.

See `docs/superpowers/plans/2026-04-16-reactive-vault-refresh.md`
for the full plan and `docs/superpowers/specs/2026-04-15-vault-
swap-architecture.md` §9.11 for the architectural rationale.

## Test plan
- [x] `uv run python -m pytest tests/ -q` — all green
- [x] Integration test (`test_integration_reactive_refresh.py`)
      reproduces the phantom-stale scenario and verifies self-healing
- [ ] Manual: restart server; leave two vault accounts idle for 2 hours;
      verify neither goes phantom-stale

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
gh pr merge --rebase --delete-branch
git checkout main
git reset --hard origin/main
```

---

## Validation protocol

After **every** milestone commit, dispatch 2–3 validation agents in a single message so they run concurrently.  Don't advance to the next milestone until all return SHIP or all flagged issues are fixed.

### Standard rotation (after M1, M2, M3, M4)

- **superpowers:code-reviewer** — plan fidelity + test quality + regression check
- **Explore agent — adversarial** — probe for races, failure modes, security
- **Explore agent — integration-scenario trace** — walk the data path end-to-end for the typical + worst case

### Final M5 rotation (5 parallel)

1. superpowers:code-reviewer — holistic cross-layer review
2. adversarial devils advocate — attack the full feature surface
3. integration-scenario replay — three scenarios: idle-phantom-stale, genuine invalid_grant, concurrent Revalidate + poll
4. OAuth-theory alignment — do the new code paths actually implement RTR correctly?
5. ToS/safety — does this change move CCSwitch closer to or further from ToS gray zone?

All 5 must return SHIP before `gh pr merge`.

---

## Shipping decision

PR merged via rebase (required_linear_history=true).  Feature branch auto-deleted
(delete_branch_on_merge=true).

**Post-deploy monitoring (24 h):**

1. `stale_reason` counts by type — `SELECT reason, COUNT(*) FROM accounts
   WHERE stale_reason IS NOT NULL`.  Pre-fix baseline: observed 2 phantom-stale
   within 1-2 hours of server restart.  Post-fix target: ≤ 1 phantom-stale per
   24 h.  If higher → rollback or root-cause re-investigation.
2. Refresh-endpoint call volume via log count: `grep "Refreshed vault token"
   ~/.local/state/ccswitch/server.log | wc -l`.  Pre-fix: ~1/h per vault account.
   Post-fix target: ~1/day per vault account (only when probe 401 fires).
3. No new "Refresh endpoint transient failure ×5" escalations for accounts
   that were healthy pre-deploy.

If post-deploy metrics fail target → `gh pr revert` and re-investigate.  No
schema change means clean revert.
