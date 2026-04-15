"""
Tests for backend.background.

Covers the per-account poll body (``_process_single_account``) across
active/vault distinctions, refresh terminal states, nudge rate-limiting,
and the post-sleep stagger in ``poll_usage_and_switch``.

All Keychain + network calls are monkeypatched — no real subprocess or
HTTP traffic.
"""
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


@pytest.mark.skip(reason="M2 will re-enable via reactive path")
@pytest.mark.asyncio
async def test_vault_account_near_expiry_refreshes(monkeypatch):
    """A vault account within 20 min of expiry must refresh_access_token
    and persist the new tokens via save_refreshed_vault_token."""
    account = _make_account(email="vault@example.com")
    # Expiry just inside the 20-minute skew — now + 5 minutes.
    near_expiry_ms = int(time.time() * 1000) + 5 * 60 * 1000
    creds = _fresh_creds(expires_at_ms=near_expiry_ms)

    monkeypatch.setattr(
        ac, "read_credentials_for_email", lambda email, active_email=None: creds
    )

    async def fake_refresh(refresh_token):
        assert refresh_token == "rt"
        return {
            "access_token": "new-at",
            "expires_in": 3600,
            "refresh_token": "new-rt",
        }

    monkeypatch.setattr(anthropic_api, "refresh_access_token", fake_refresh)

    saved: dict = {}

    def fake_save(email, access_token, expires_at=None, refresh_token=None):
        saved["email"] = email
        saved["access_token"] = access_token
        saved["expires_at"] = expires_at
        saved["refresh_token"] = refresh_token

    monkeypatch.setattr(cp, "save_refreshed_vault_token", fake_save)

    async def fake_probe(token):
        return {"five_hour": {"utilization": 10.0, "resets_at": 1}}

    monkeypatch.setattr(anthropic_api, "probe_usage", fake_probe)

    # Non-active: active_email is some other account.
    _, stale = await bg._process_single_account(account, "someone-else@example.com")
    assert stale is None
    assert saved["email"] == "vault@example.com"
    assert saved["access_token"] == "new-at"
    assert saved["refresh_token"] == "new-rt"
    assert saved["expires_at"] is not None


@pytest.mark.skip(reason="M2 will re-enable via reactive path")
@pytest.mark.asyncio
async def test_vault_refresh_400_sets_rejected_stale_reason(monkeypatch):
    account = _make_account(email="vault@example.com")
    near_expiry_ms = int(time.time() * 1000) + 5 * 60 * 1000

    monkeypatch.setattr(
        ac, "read_credentials_for_email",
        lambda email, active_email=None: _fresh_creds(expires_at_ms=near_expiry_ms),
    )

    async def fake_refresh(refresh_token):
        # 400 + OAuth2 `error=invalid_grant` → terminal rejected.  A bare
        # 400 without a body is now TRANSIENT, so inject the RFC 6749 §5.2
        # terminal code here to keep this test exercising the terminal path.
        raise _http_error(400, json_body={"error": "invalid_grant"})

    monkeypatch.setattr(anthropic_api, "refresh_access_token", fake_refresh)

    probe_called = {"n": 0}

    async def fake_probe(token):
        probe_called["n"] += 1
        return {}

    monkeypatch.setattr(anthropic_api, "probe_usage", fake_probe)

    _, stale = await bg._process_single_account(account, "someone-else@example.com")
    assert stale == "Refresh token rejected — re-login required"
    # probe was skipped
    assert probe_called["n"] == 0


@pytest.mark.skip(reason="M2 will re-enable via reactive path")
@pytest.mark.asyncio
async def test_vault_refresh_401_sets_revoked_stale_reason(monkeypatch):
    account = _make_account(email="vault@example.com")
    near_expiry_ms = int(time.time() * 1000) + 5 * 60 * 1000

    monkeypatch.setattr(
        ac, "read_credentials_for_email",
        lambda email, active_email=None: _fresh_creds(expires_at_ms=near_expiry_ms),
    )

    async def fake_refresh(refresh_token):
        # 401 + OAuth2 `error=invalid_grant` → terminal revoked.  A bare
        # 401 without a body is now TRANSIENT (could be an edge-proxy
        # WAF challenge) and would not set stale_reason, so inject the
        # RFC 6749 §5.2 terminal code here.
        raise _http_error(401, json_body={"error": "invalid_grant"})

    monkeypatch.setattr(anthropic_api, "refresh_access_token", fake_refresh)

    async def fake_probe(token):
        return {}

    monkeypatch.setattr(anthropic_api, "probe_usage", fake_probe)

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

@pytest.mark.skip(reason="M2 will re-enable via reactive path")
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


@pytest.mark.skip(reason="M2 will re-enable via reactive path")
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


@pytest.mark.skip(reason="M2 will re-enable via reactive path")
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


@pytest.mark.skip(reason="M2 will re-enable via reactive path")
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


@pytest.mark.skip(reason="M2 will re-enable via reactive path")
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


@pytest.mark.skip(reason="M2 will re-enable via reactive path")
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


@pytest.mark.skip(reason="M2 will re-enable via reactive path")
@pytest.mark.asyncio
async def test_refresh_success_then_transient_starts_fresh_escalation_clock(monkeypatch):
    """After a successful refresh clears counters, a subsequent transient
    failure must start first_failure_at fresh — not reuse an old value that
    would let the 24h wall-clock trigger escalation prematurely."""
    near_expiry_ms = int(time.time() * 1000) + 5 * 60 * 1000
    account = _make_account(email="vault@example.com")

    monkeypatch.setattr(
        ac, "read_credentials_for_email",
        lambda email, active_email=None: _fresh_creds(expires_at_ms=near_expiry_ms),
    )

    async def fake_probe(token):
        return {}
    monkeypatch.setattr(anthropic_api, "probe_usage", fake_probe)

    # Phase 1: success — all three dicts clear.
    async def ok_refresh(rt):
        return {"access_token": "new", "expires_in": 3600}
    monkeypatch.setattr(anthropic_api, "refresh_access_token", ok_refresh)

    def noop_save(*args, **kwargs):
        pass
    monkeypatch.setattr(cp, "save_refreshed_vault_token", noop_save)

    _, stale = await bg._process_single_account(account, "other@example.com")
    assert stale is None
    assert "vault@example.com" not in bg._refresh_backoff_first_failure_at

    # Phase 2: one transient.  first_failure_at must be freshly set (close to now).
    before = time.monotonic()
    async def bad_refresh(rt):
        raise _http_error(400, json_body={"error": "invalid_request"})
    monkeypatch.setattr(anthropic_api, "refresh_access_token", bad_refresh)

    # Force deadline to past so the skip gate doesn't fire.
    bg._refresh_backoff_until.pop("vault@example.com", None)

    _, stale = await bg._process_single_account(account, "other@example.com")
    assert stale is None  # not escalated yet
    assert bg._refresh_backoff_count["vault@example.com"] == 1
    # first_failure_at is close to now, not some ancient value.
    first_at = bg._refresh_backoff_first_failure_at["vault@example.com"]
    assert first_at >= before  # freshly set
    assert first_at - time.monotonic() < 1.0  # within the past second


@pytest.mark.skip(reason="M2 will re-enable via reactive path")
@pytest.mark.asyncio
async def test_refresh_network_error_is_transient(monkeypatch):
    """httpx.RequestError on refresh must increment the transient backoff
    counter, not fall through to the generic-Exception handler (which
    would let sustained network outages avoid escalation entirely)."""
    import httpx
    near_expiry_ms = int(time.time() * 1000) + 5 * 60 * 1000
    account = _make_account(email="vault@example.com")

    monkeypatch.setattr(
        ac, "read_credentials_for_email",
        lambda email, active_email=None: _fresh_creds(expires_at_ms=near_expiry_ms),
    )

    async def net_error(rt):
        raise httpx.ConnectError("simulated network outage")
    monkeypatch.setattr(anthropic_api, "refresh_access_token", net_error)

    async def fake_probe(token):
        return {}
    monkeypatch.setattr(anthropic_api, "probe_usage", fake_probe)

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
