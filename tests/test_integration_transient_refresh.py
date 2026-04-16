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


@pytest.fixture(autouse=True)
def _clear_backoff_state():
    from backend import background as bg
    from backend.services import account_service as ac
    bg._refresh_backoff_until.clear()
    bg._refresh_backoff_count.clear()
    bg._refresh_backoff_first_failure_at.clear()
    ac._refresh_locks.clear()
    yield
    bg._refresh_backoff_until.clear()
    bg._refresh_backoff_count.clear()
    bg._refresh_backoff_first_failure_at.clear()
    ac._refresh_locks.clear()


def _http_400(error_code):
    req = httpx.Request("POST", "https://api.anthropic.com/oauth2/token")
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 400
    resp.json = MagicMock(return_value={"error": error_code})
    return httpx.HTTPStatusError("bad", request=req, response=resp)


@pytest.mark.asyncio
async def test_transient_400_storm_never_sets_stale_until_escalation(monkeypatch):
    """Reactive path: N−1 consecutive transient 400s leave stale_reason
    None; the Nth escalates.  Each iteration clears the backoff + reactive
    cooldown so the reactive path runs on every call."""
    bg._refresh_backoff_until.clear()
    bg._refresh_backoff_count.clear()
    bg._last_reactive_refresh_at.clear()

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
                "expiresAt": int(time.time() * 1000) + 10_000_000,
            },
        },
    )

    async def always_transient(refresh_token):
        raise _http_400("some_transient_oauth_code")
    monkeypatch.setattr(anthropic_api, "refresh_access_token", always_transient)

    async def fake_probe(token):
        raise httpx.HTTPStatusError(
            "401",
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
            response=httpx.Response(
                401,
                request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
            ),
        )
    monkeypatch.setattr(anthropic_api, "probe_usage", fake_probe)

    stale_history = []
    # Force deadline + cooldown to the past each iteration so the reactive
    # refresh fires every cycle.
    for attempt in range(1, bg._TRANSIENT_REFRESH_ESCALATE_AFTER + 1):
        bg._refresh_backoff_until["vault@example.com"] = time.monotonic() - 1.0
        bg._last_reactive_refresh_at.pop("vault@example.com", None)
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
    def fake_save(email, t, *a, **kw):
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
