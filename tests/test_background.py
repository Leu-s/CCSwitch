"""
Tests for backend.background.

Covers the per-account poll body (``_process_single_account``) across
active/vault distinctions, refresh terminal states, nudge rate-limiting,
and the post-sleep stagger in ``poll_usage_and_switch``.

All Keychain + network calls are monkeypatched — no real subprocess or
HTTP traffic.
"""
import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from backend import background as bg
from backend.cache import cache as _cache
from backend.models import Account
from backend.services import account_service as ac
from backend.services import anthropic_api
from backend.services import credential_provider as cp
from backend.services import tmux_service


# ── Shared fixtures ────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
async def _wipe_cache_between_tests():
    _cache._usage.clear()
    _cache._token_info.clear()
    bg._backoff_until.clear()
    bg._backoff_count.clear()
    bg._refresh_backoff_until.clear()
    bg._refresh_backoff_count.clear()
    bg._refresh_backoff_first_failure_at.clear()
    bg._last_nudge_at.clear()
    bg._last_reactive_refresh_at.clear()
    bg._last_poll_monotonic = None
    yield
    _cache._usage.clear()
    _cache._token_info.clear()
    bg._backoff_until.clear()
    bg._backoff_count.clear()
    bg._refresh_backoff_until.clear()
    bg._refresh_backoff_count.clear()
    bg._refresh_backoff_first_failure_at.clear()
    bg._last_nudge_at.clear()
    bg._last_reactive_refresh_at.clear()
    bg._last_poll_monotonic = None


def _make_account(**kwargs) -> Account:
    defaults = dict(
        id=1,
        email="a@example.com",
        threshold_pct=95.0,
        enabled=True,
        priority=0,
        stale_reason=None,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kwargs)
    return Account(**defaults)


def _fresh_creds(expires_at_ms: int | None = None) -> dict:
    return {
        "claudeAiOauth": {
            "accessToken": "at",
            "refreshToken": "rt",
            "expiresAt": expires_at_ms or int(time.time() * 1000) + 10_000_000,
        },
        "oauthAccount": {"emailAddress": "a@example.com"},
    }


def _http_error(status: int, json_body=None) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    if json_body is not None:
        import json as _json
        response = httpx.Response(
            status,
            request=request,
            content=_json.dumps(json_body).encode("utf-8"),
            headers={"content-type": "application/json"},
        )
    else:
        response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(str(status), request=request, response=response)


# ── Active account behaviour ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_active_account_reads_standard_never_refreshes(monkeypatch):
    """The active account reads from the standard Keychain entry and never
    calls refresh_access_token, even if the stored token is past expiry."""
    account = _make_account(email="active@example.com")

    reads: list[tuple[str, str | None]] = []

    def fake_read(email, active_email=None):
        reads.append((email, active_email))
        return _fresh_creds()

    monkeypatch.setattr(ac, "read_credentials_for_email", fake_read)

    refresh_called = {"n": 0}

    async def fake_refresh(*args, **kwargs):
        refresh_called["n"] += 1
        return {}

    monkeypatch.setattr(anthropic_api, "refresh_access_token", fake_refresh)

    async def fake_probe(token):
        return {"five_hour": {"utilization": 12.0, "resets_at": 1}}

    monkeypatch.setattr(anthropic_api, "probe_usage", fake_probe)

    entry, stale = await bg._process_single_account(account, "active@example.com")
    assert stale is None
    assert entry["email"] == "active@example.com"
    # read_credentials_for_email was passed active_email so it routes to standard.
    assert reads and reads[0][1] == "active@example.com"
    # No refresh call for active account, even if near expiry.
    assert refresh_called["n"] == 0


# ── Vault account refresh ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_vault_account_probe_401_reactive_refresh_persists(monkeypatch):
    """A vault account whose probe 401s triggers the reactive refresh path,
    which calls refresh_access_token and persists via
    save_refreshed_vault_token."""
    account = _make_account(email="vault@example.com")
    creds = _fresh_creds()

    monkeypatch.setattr(
        ac, "read_credentials_for_email", lambda email, active_email=None: creds
    )

    probe_calls: list[str] = []

    async def fake_probe(token):
        probe_calls.append(token)
        if len(probe_calls) == 1:
            raise _http_error(401)
        return {"five_hour": {"utilization": 10.0, "resets_at": 1}}

    monkeypatch.setattr(anthropic_api, "probe_usage", fake_probe)

    async def fake_refresh(refresh_token):
        assert refresh_token == "rt"
        return {
            "access_token": "new-at",
            "expires_in": 3600,
            "refresh_token": "new-rt",
        }

    monkeypatch.setattr(anthropic_api, "refresh_access_token", fake_refresh)

    saved: dict = {}

    def fake_save(email, access_token, expires_at=None, refresh_token=None, **kw):
        saved["email"] = email
        saved["access_token"] = access_token
        saved["expires_at"] = expires_at
        saved["refresh_token"] = refresh_token

    monkeypatch.setattr(cp, "save_refreshed_vault_token", fake_save)

    # Non-active: active_email is some other account.
    _, stale = await bg._process_single_account(account, "someone-else@example.com")
    assert stale is None
    assert saved["email"] == "vault@example.com"
    assert saved["access_token"] == "new-at"
    assert saved["refresh_token"] == "new-rt"
    assert saved["expires_at"] is not None
    # Reactive: probe happened twice (original 401 + retry after refresh).
    assert len(probe_calls) == 2


@pytest.mark.asyncio
async def test_vault_refresh_400_sets_rejected_stale_reason(monkeypatch):
    """Reactive path: vault probe 401 triggers refresh; refresh returns 400
    invalid_grant → terminal rejected stale_reason."""
    account = _make_account(email="vault@example.com")

    monkeypatch.setattr(
        ac, "read_credentials_for_email",
        lambda email, active_email=None: _fresh_creds(),
    )

    async def fake_probe(token):
        raise _http_error(401)

    monkeypatch.setattr(anthropic_api, "probe_usage", fake_probe)

    async def fake_refresh(refresh_token):
        # 400 + OAuth2 `error=invalid_grant` → terminal rejected.  A bare
        # 400 without a body is now TRANSIENT, so inject the RFC 6749 §5.2
        # terminal code here to keep this test exercising the terminal path.
        raise _http_error(400, json_body={"error": "invalid_grant"})

    monkeypatch.setattr(anthropic_api, "refresh_access_token", fake_refresh)

    _, stale = await bg._process_single_account(account, "someone-else@example.com")
    assert stale == "Refresh token rejected — re-login required"


@pytest.mark.asyncio
async def test_vault_refresh_401_sets_revoked_stale_reason(monkeypatch):
    """Reactive path: vault probe 401 triggers refresh; refresh returns 401
    invalid_grant → terminal revoked stale_reason."""
    account = _make_account(email="vault@example.com")

    monkeypatch.setattr(
        ac, "read_credentials_for_email",
        lambda email, active_email=None: _fresh_creds(),
    )

    async def fake_probe(token):
        raise _http_error(401)

    monkeypatch.setattr(anthropic_api, "probe_usage", fake_probe)

    async def fake_refresh(refresh_token):
        # 401 + OAuth2 `error=invalid_grant` → terminal revoked.  A bare
        # 401 without a body is now TRANSIENT (could be an edge-proxy
        # WAF challenge) and would not set stale_reason, so inject the
        # RFC 6749 §5.2 terminal code here.
        raise _http_error(401, json_body={"error": "invalid_grant"})

    monkeypatch.setattr(anthropic_api, "refresh_access_token", fake_refresh)

    _, stale = await bg._process_single_account(account, "someone-else@example.com")
    assert stale == "Refresh token revoked — re-login required"


# ── Active-account probe 401 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_active_probe_401_fires_nudge_once_and_keeps_cached(monkeypatch):
    """Active-account probe 401 triggers fire_nudge once, does NOT set
    stale_reason, and returns the cached last-known usage.  A second call
    within the cooldown window is NOT re-nudged."""
    account = _make_account(email="active@example.com")

    monkeypatch.setattr(
        ac, "read_credentials_for_email",
        lambda email, active_email=None: _fresh_creds(),
    )

    async def fake_refresh(*args, **kwargs):
        return {}

    monkeypatch.setattr(anthropic_api, "refresh_access_token", fake_refresh)

    async def fake_probe(token):
        raise _http_error(401)

    monkeypatch.setattr(anthropic_api, "probe_usage", fake_probe)

    nudge_calls = {"n": 0}

    def fake_nudge():
        nudge_calls["n"] += 1

    monkeypatch.setattr(tmux_service, "fire_nudge", fake_nudge)

    # Pre-seed cached "last known" usage.
    await _cache.set_usage(
        "active@example.com",
        {"five_hour": {"utilization": 44.0, "resets_at": 1}},
    )

    entry1, stale1 = await bg._process_single_account(account, "active@example.com")
    entry2, stale2 = await bg._process_single_account(account, "active@example.com")

    # Neither call marked stale — active-probe 401 is soft.
    assert stale1 is None
    assert stale2 is None
    # First call fires nudge; second is rate-limited out by _last_nudge_at.
    assert nudge_calls["n"] == 1
    # Cached usage is returned (non-empty dict).
    assert entry1["usage"]  # non-empty UsageData


# ── Active-account probe 429 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_probe_429_sets_backoff_without_stale_reason(monkeypatch):
    account = _make_account(email="active@example.com")

    monkeypatch.setattr(
        ac, "read_credentials_for_email",
        lambda email, active_email=None: _fresh_creds(),
    )

    async def fake_refresh(*args, **kwargs):
        return {}

    monkeypatch.setattr(anthropic_api, "refresh_access_token", fake_refresh)

    async def fake_probe(token):
        raise _http_error(429)

    monkeypatch.setattr(anthropic_api, "probe_usage", fake_probe)

    _, stale = await bg._process_single_account(account, "active@example.com")
    assert stale is None  # 429 does not set stale_reason
    assert "active@example.com" in bg._backoff_until
    # Cache entry has rate_limited flag.
    cached = await _cache.get_usage_async("active@example.com")
    assert cached.get("rate_limited") is True


# ── Post-sleep stagger ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sleep_jump_triggers_asyncio_sleep(monkeypatch):
    """A monotonic-time gap > 300s between polls triggers a random
    asyncio.sleep(0..30) before the refresh burst."""
    # Reset the global so we start from "no previous poll".
    bg._last_poll_monotonic = 0.0  # pretend a previous poll happened at t=0

    # Force time.monotonic to return t=10_000 (≫ 300s gap).
    monkeypatch.setattr(bg.time, "monotonic", lambda: 10_000.0)

    # Make random.uniform deterministic.
    monkeypatch.setattr(bg.random, "uniform", lambda a, b: 5.5)

    # Capture asyncio.sleep calls.
    sleep_args: list[float] = []

    async def fake_sleep(seconds):
        sleep_args.append(seconds)

    monkeypatch.setattr(bg.asyncio, "sleep", fake_sleep)

    # Stub DB + active-email + account-list so poll_usage_and_switch runs
    # the stagger branch and then short-circuits.
    class _FakeExec:
        def scalars(self):
            return SimpleNamespace(all=lambda: [])

    class _FakeSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def execute(self, _stmt): return _FakeExec()
        async def commit(self): pass

    def fake_session_ctor():
        return _FakeSession()

    monkeypatch.setattr(bg, "AsyncSessionLocal", fake_session_ctor)

    async def fake_get_active_email_async():
        return None

    monkeypatch.setattr(ac, "get_active_email_async", fake_get_active_email_async)

    async def fake_maybe(db, ws):
        return None

    monkeypatch.setattr(bg.sw, "maybe_auto_switch", fake_maybe)

    class _WS:
        async def broadcast(self, payload): return 0

    await bg.poll_usage_and_switch(_WS())

    assert 5.5 in sleep_args


# ── Return shape includes stale_reason ────────────────────────────────────


@pytest.mark.asyncio
async def test_process_returns_stale_reason_tuple(monkeypatch):
    """_process_single_account's return is a (usage_entry, stale_reason)
    tuple.  stale_reason should be None on the happy path."""
    account = _make_account(email="a@example.com")

    monkeypatch.setattr(
        ac, "read_credentials_for_email",
        lambda email, active_email=None: _fresh_creds(),
    )

    async def fake_probe(token):
        return {"five_hour": {"utilization": 1.0, "resets_at": 1}}

    monkeypatch.setattr(anthropic_api, "probe_usage", fake_probe)

    result = await bg._process_single_account(account, "a@example.com")
    assert isinstance(result, tuple)
    assert len(result) == 2
    entry, stale_reason = result
    assert stale_reason is None
    assert entry["email"] == "a@example.com"


# ── Transient refresh-failure handling ────────────────────────────────────

@pytest.mark.asyncio
async def test_refresh_400_invalid_grant_sets_terminal_stale(monkeypatch):
    """Reactive path: probe 401 triggers refresh; 400 invalid_grant →
    terminal stale_reason, no backoff counters."""
    from backend.services.anthropic_api import OAuthErrorKind  # noqa: F401

    bg._refresh_backoff_until.clear()
    bg._refresh_backoff_count.clear()
    account = _make_account(email="vault@example.com")

    monkeypatch.setattr(
        ac, "read_credentials_for_email",
        lambda email, active_email=None: _fresh_creds(),
    )

    async def fake_probe(token):
        raise _http_error(401)
    monkeypatch.setattr(anthropic_api, "probe_usage", fake_probe)

    async def fake_refresh(refresh_token):
        raise _http_error(400, json_body={"error": "invalid_grant"})

    monkeypatch.setattr(anthropic_api, "refresh_access_token", fake_refresh)

    _, stale = await bg._process_single_account(account, "other@example.com")
    assert stale == "Refresh token rejected — re-login required"
    assert "vault@example.com" not in bg._refresh_backoff_count
    assert "vault@example.com" not in bg._refresh_backoff_until


@pytest.mark.asyncio
async def test_refresh_400_invalid_request_is_transient_no_stale(monkeypatch):
    """Reactive path: probe 401 triggers refresh; 400 invalid_request (non-
    terminal) → no stale_reason, backoff counter = 1."""
    bg._refresh_backoff_until.clear()
    bg._refresh_backoff_count.clear()
    account = _make_account(email="vault@example.com")

    monkeypatch.setattr(
        ac, "read_credentials_for_email",
        lambda email, active_email=None: _fresh_creds(),
    )

    async def fake_probe(token):
        raise _http_error(401)
    monkeypatch.setattr(anthropic_api, "probe_usage", fake_probe)

    async def fake_refresh(refresh_token):
        raise _http_error(400, json_body={"error": "invalid_request"})

    monkeypatch.setattr(anthropic_api, "refresh_access_token", fake_refresh)

    _, stale = await bg._process_single_account(account, "other@example.com")
    assert stale is None
    assert bg._refresh_backoff_count["vault@example.com"] == 1
    assert "vault@example.com" in bg._refresh_backoff_until


@pytest.mark.asyncio
async def test_refresh_transient_escalates_after_n_failures(monkeypatch):
    """Reactive path: after `_TRANSIENT_REFRESH_ESCALATE_AFTER` consecutive
    transient refresh failures, mark stale."""
    # Pre-load the counter to one below the escalation threshold, and a
    # recent first-failure timestamp so the wall-clock ceiling does NOT fire.
    bg._refresh_backoff_count["vault@example.com"] = bg._TRANSIENT_REFRESH_ESCALATE_AFTER - 1
    bg._refresh_backoff_first_failure_at["vault@example.com"] = time.monotonic() - 10

    account = _make_account(email="vault@example.com")

    monkeypatch.setattr(
        ac, "read_credentials_for_email",
        lambda email, active_email=None: _fresh_creds(),
    )

    async def fake_probe(token):
        raise _http_error(401)
    monkeypatch.setattr(anthropic_api, "probe_usage", fake_probe)

    async def fake_refresh(refresh_token):
        raise _http_error(400, json_body={"error": "invalid_request"})

    monkeypatch.setattr(anthropic_api, "refresh_access_token", fake_refresh)

    _, stale = await bg._process_single_account(account, "other@example.com")
    assert stale is not None
    assert "transient failure" in stale
    assert f"×{bg._TRANSIENT_REFRESH_ESCALATE_AFTER}" in stale
    # Counters cleared on escalation.
    assert "vault@example.com" not in bg._refresh_backoff_count
    assert "vault@example.com" not in bg._refresh_backoff_first_failure_at


@pytest.mark.asyncio
async def test_refresh_transient_escalates_after_wall_clock_ceiling(monkeypatch):
    """Reactive path: if the first transient was > 24 h ago, escalate
    regardless of count.

    Protects against counter-reset loops (Anthropic intermittently succeeds
    resetting the count; feature still broken for the account in net).
    """
    # Count well below threshold, but first-failure timestamp older than the
    # 24 h ceiling — escalation must fire on this attempt.
    bg._refresh_backoff_count["vault@example.com"] = 2
    bg._refresh_backoff_first_failure_at["vault@example.com"] = (
        time.monotonic() - (bg._TRANSIENT_REFRESH_ESCALATE_AFTER_SECONDS + 60)
    )

    account = _make_account(email="vault@example.com")

    monkeypatch.setattr(
        ac, "read_credentials_for_email",
        lambda email, active_email=None: _fresh_creds(),
    )

    async def fake_probe(token):
        raise _http_error(401)
    monkeypatch.setattr(anthropic_api, "probe_usage", fake_probe)

    async def fake_refresh(refresh_token):
        raise _http_error(400, json_body={"error": "invalid_request"})

    monkeypatch.setattr(anthropic_api, "refresh_access_token", fake_refresh)

    _, stale = await bg._process_single_account(account, "other@example.com")
    assert stale is not None
    assert "transient failure" in stale
    assert "vault@example.com" not in bg._refresh_backoff_first_failure_at


@pytest.mark.asyncio
async def test_refresh_backoff_skips_retry_within_deadline(monkeypatch):
    """Reactive path: while refresh-backoff deadline is in the future, a
    probe 401 does NOT trigger a refresh attempt."""
    bg._refresh_backoff_until.clear()
    bg._refresh_backoff_count.clear()
    bg._refresh_backoff_until["vault@example.com"] = time.monotonic() + 60.0
    bg._refresh_backoff_count["vault@example.com"] = 1

    account = _make_account(email="vault@example.com")

    monkeypatch.setattr(
        ac, "read_credentials_for_email",
        lambda email, active_email=None: _fresh_creds(),
    )

    async def fake_probe(token):
        raise _http_error(401)
    monkeypatch.setattr(anthropic_api, "probe_usage", fake_probe)

    refresh_calls = []
    async def fake_refresh(refresh_token):
        refresh_calls.append(refresh_token)
        return {"access_token": "new", "expires_in": 3600}

    monkeypatch.setattr(anthropic_api, "refresh_access_token", fake_refresh)

    _, stale = await bg._process_single_account(account, "other@example.com")
    assert stale is None
    assert refresh_calls == []  # refresh was skipped (backoff active)


@pytest.mark.asyncio
async def test_refresh_success_clears_backoff_counters(monkeypatch):
    """Reactive path: a probe-401 + successful refresh clears the refresh-
    backoff counters."""
    bg._refresh_backoff_until.clear()
    bg._refresh_backoff_count.clear()
    bg._refresh_backoff_count["vault@example.com"] = 3
    # Deadline is in the past — no skip.
    bg._refresh_backoff_until["vault@example.com"] = time.monotonic() - 1.0

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
        return {"five_hour": {"utilization": 5.0, "resets_at": 1}}
    monkeypatch.setattr(anthropic_api, "probe_usage", fake_probe)

    async def fake_refresh(refresh_token):
        return {"access_token": "new-access", "expires_in": 3600}
    monkeypatch.setattr(anthropic_api, "refresh_access_token", fake_refresh)

    saved = {}
    def fake_save(email, access_token, expires_at=None, refresh_token=None, **kw):
        saved["email"] = email
        saved["token"] = access_token
    monkeypatch.setattr(cp, "save_refreshed_vault_token", fake_save)

    _, stale = await bg._process_single_account(account, "other@example.com")
    assert stale is None
    assert saved["token"] == "new-access"
    assert "vault@example.com" not in bg._refresh_backoff_count
    assert "vault@example.com" not in bg._refresh_backoff_until


@pytest.mark.asyncio
async def test_refresh_success_then_transient_starts_fresh_escalation_clock(monkeypatch):
    """Reactive path: after a successful refresh clears counters, a subsequent
    transient failure must start first_failure_at fresh — not reuse an old
    value that would let the 24 h wall-clock trigger escalation prematurely.

    Simulates via direct helper calls: phase 1 success clears, phase 2
    transient starts fresh escalation clock."""
    # Phase 1: successful refresh directly via the helper to clear counters.
    bg._refresh_backoff_count["vault@example.com"] = 2
    bg._refresh_backoff_first_failure_at["vault@example.com"] = time.monotonic() - 60

    async def ok_refresh(rt):
        return {"access_token": "new", "expires_in": 3600}
    monkeypatch.setattr(anthropic_api, "refresh_access_token", ok_refresh)

    def noop_save(*args, **kwargs):
        pass
    monkeypatch.setattr(cp, "save_refreshed_vault_token", noop_save)

    await bg._refresh_vault_token("vault@example.com", "rt-live")
    assert "vault@example.com" not in bg._refresh_backoff_first_failure_at
    assert "vault@example.com" not in bg._refresh_backoff_count

    # Phase 2: one transient.  first_failure_at must be freshly set.
    before = time.monotonic()
    async def bad_refresh(rt):
        raise _http_error(400, json_body={"error": "invalid_request"})
    monkeypatch.setattr(anthropic_api, "refresh_access_token", bad_refresh)

    with pytest.raises(httpx.HTTPStatusError):
        await bg._refresh_vault_token("vault@example.com", "rt-live")
    assert bg._refresh_backoff_count["vault@example.com"] == 1
    # first_failure_at is close to now, not some ancient value.
    first_at = bg._refresh_backoff_first_failure_at["vault@example.com"]
    assert first_at >= before  # freshly set
    assert first_at - time.monotonic() < 1.0  # within the past second


@pytest.mark.asyncio
async def test_refresh_network_error_is_transient(monkeypatch):
    """Reactive path: httpx.RequestError on refresh must increment the
    transient backoff counter, not fall through to the generic-Exception
    handler (which would let sustained network outages avoid escalation
    entirely)."""
    import httpx
    account = _make_account(email="vault@example.com")

    monkeypatch.setattr(
        ac, "read_credentials_for_email",
        lambda email, active_email=None: _fresh_creds(),
    )

    async def fake_probe(token):
        raise _http_error(401)
    monkeypatch.setattr(anthropic_api, "probe_usage", fake_probe)

    async def net_error(rt):
        raise httpx.ConnectError("simulated network outage")
    monkeypatch.setattr(anthropic_api, "refresh_access_token", net_error)

    _, stale = await bg._process_single_account(account, "other@example.com")
    assert stale is None  # not escalated yet
    assert bg._refresh_backoff_count["vault@example.com"] == 1
    assert "vault@example.com" in bg._refresh_backoff_first_failure_at


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

    def ok_save(email, access_token, expires_at=None, refresh_token=None, **kw):
        pass
    monkeypatch.setattr(cp, "save_refreshed_vault_token", ok_save)

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
        return {"five_hour": {"utilization": 42.0}}
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
    monkeypatch.setattr(cp, "save_refreshed_vault_token", lambda *a, **kw: None)

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
    monkeypatch.setattr(cp, "save_refreshed_vault_token", lambda *a, **kw: None)

    # Run _process_single_account twice in rapid succession.
    await bg._process_single_account(account, "other@example.com")
    await bg._process_single_account(account, "other@example.com")

    assert len(refresh_calls) == 1  # only first cycle refreshed


@pytest.mark.asyncio
async def test_reactive_refresh_cooldown_returns_cached_without_stale(monkeypatch):
    """Vault probe 401 within the 60 s reactive-refresh cooldown window
    skips the refresh, returns the cached usage, and DOES NOT set
    stale_reason.  The previous refresh attempt might still be propagating
    on Anthropic's side — marking stale now would cause UI flicker on a
    recoverable degraded state."""
    bg._last_reactive_refresh_at.clear()
    bg._refresh_backoff_until.clear()

    email = "vault@example.com"
    account = _make_account(email=email)
    monkeypatch.setattr(
        ac, "read_credentials_for_email",
        lambda e, active_email=None: _fresh_creds(),
    )

    # Pre-set a reactive-refresh timestamp inside the cooldown window so
    # the next probe 401 hits the cooldown branch.
    bg._last_reactive_refresh_at[email] = time.monotonic()

    # Seed a non-empty cached usage entry to assert shape preservation.
    cached_usage = {"five_hour": {"utilization": 33.0, "resets_at": 1}}
    await _cache.set_usage(email, cached_usage)

    async def fake_probe(token):
        raise _http_error(401)
    monkeypatch.setattr(anthropic_api, "probe_usage", fake_probe)

    refresh_calls = []
    async def fake_refresh(rt):
        refresh_calls.append(rt)
        return {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
    monkeypatch.setattr(anthropic_api, "refresh_access_token", fake_refresh)

    entry, stale = await bg._process_single_account(account, "other@example.com")

    # Refresh was NOT attempted — cooldown branch short-circuited.
    assert len(refresh_calls) == 0
    # stale_reason stays None — cooldown skip is not a staleness signal.
    assert stale is None
    # Returned usage has the cached shape (build_usage flattens five_hour →
    # five_hour_pct); the cache entry itself is untouched.
    stored = await _cache.get_usage_async(email)
    assert stored == cached_usage
    # Flattened entry should reflect the cached utilization.
    assert entry.get("usage", {}).get("five_hour_pct") == 33


@pytest.mark.asyncio
async def test_different_emails_do_not_serialize_refresh(monkeypatch):
    """Per-email refresh locks must NOT serialise across different emails.
    Two concurrent _refresh_vault_token calls on distinct emails should
    overlap — otherwise get_refresh_lock has regressed into a single
    global lock and N accounts would block on one another's refreshes."""
    ac._refresh_locks.clear()

    enter_times: list[float] = []

    async def timed_refresh(rt):
        enter_times.append(asyncio.get_event_loop().time())
        await asyncio.sleep(0.1)  # sizable hold so serialisation would be visible
        return {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}

    monkeypatch.setattr(anthropic_api, "refresh_access_token", timed_refresh)
    monkeypatch.setattr(cp, "save_refreshed_vault_token", lambda *a, **kw: None)

    async def task_a():
        lock = ac.get_refresh_lock("a@example.com")
        async with lock:
            await bg._refresh_vault_token("a@example.com", "rt-a")

    async def task_b():
        lock = ac.get_refresh_lock("b@example.com")
        async with lock:
            await bg._refresh_vault_token("b@example.com", "rt-b")

    await asyncio.gather(task_a(), task_b())

    assert len(enter_times) == 2
    # STRICT: both entered within ~50 ms of each other, i.e. concurrently.
    # A broken implementation (single global lock) would put them
    # ~100 ms apart due to the awaited sleep inside the critical section.
    assert abs(enter_times[1] - enter_times[0]) < 0.05, (
        f"Refreshes on different emails serialised: "
        f"enter[0]={enter_times[0]} enter[1]={enter_times[1]}"
    )


def test_forget_account_state_clears_all_tracking_dicts():
    """forget_account_state must clear EVERY module-level per-account
    bookkeeping dict.  A future refactor that drops one of these from the
    helper body would leak state across account churn (delete → re-add
    under the same email would resurrect stale backoff counters)."""
    email = "forget-me@example.com"

    # Seed every dict the helper claims to clear.
    bg._backoff_until[email] = 1.0
    bg._backoff_count[email] = 2
    bg._last_nudge_at[email] = 3.0
    bg._refresh_backoff_until[email] = 4.0
    bg._refresh_backoff_count[email] = 5
    bg._refresh_backoff_first_failure_at[email] = 6.0
    bg._last_reactive_refresh_at[email] = 7.0

    bg.forget_account_state(email)

    assert email not in bg._backoff_until
    assert email not in bg._backoff_count
    assert email not in bg._last_nudge_at
    assert email not in bg._refresh_backoff_until
    assert email not in bg._refresh_backoff_count
    assert email not in bg._refresh_backoff_first_failure_at
    assert email not in bg._last_reactive_refresh_at


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
    monkeypatch.setattr(cp, "save_refreshed_vault_token", lambda *a, **kw: None)

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
