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
    bg._last_nudge_at.clear()
    bg._last_poll_monotonic = None
    yield
    _cache._usage.clear()
    _cache._token_info.clear()
    bg._backoff_until.clear()
    bg._backoff_count.clear()
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


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
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


@pytest.mark.asyncio
async def test_vault_refresh_400_sets_rejected_stale_reason(monkeypatch):
    account = _make_account(email="vault@example.com")
    near_expiry_ms = int(time.time() * 1000) + 5 * 60 * 1000

    monkeypatch.setattr(
        ac, "read_credentials_for_email",
        lambda email, active_email=None: _fresh_creds(expires_at_ms=near_expiry_ms),
    )

    async def fake_refresh(refresh_token):
        raise _http_error(400)

    monkeypatch.setattr(anthropic_api, "refresh_access_token", fake_refresh)

    probe_called = {"n": 0}

    async def fake_probe(token):
        probe_called["n"] += 1
        return {}

    monkeypatch.setattr(anthropic_api, "probe_usage", fake_probe)

    _, stale = await bg._process_single_account(account, "someone-else@example.com")
    assert stale == "Refresh token rejected (400) — re-login required"
    # probe was skipped
    assert probe_called["n"] == 0


@pytest.mark.asyncio
async def test_vault_refresh_401_sets_revoked_stale_reason(monkeypatch):
    account = _make_account(email="vault@example.com")
    near_expiry_ms = int(time.time() * 1000) + 5 * 60 * 1000

    monkeypatch.setattr(
        ac, "read_credentials_for_email",
        lambda email, active_email=None: _fresh_creds(expires_at_ms=near_expiry_ms),
    )

    async def fake_refresh(refresh_token):
        raise _http_error(401)

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
