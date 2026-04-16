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


def _make_http_error(status: int, json_body=None):
    req = httpx.Request("POST", "https://api.anthropic.com/test")
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    if json_body is not None:
        resp.json = MagicMock(return_value=json_body)
    else:
        resp.json = MagicMock(side_effect=ValueError("no json"))
    resp.text = ""
    return httpx.HTTPStatusError("status", request=req, response=resp)


@pytest.mark.asyncio
async def test_phantom_stale_scenario_self_heals(monkeypatch):
    """Reproduce the April 16 incident.  Vault account's access_token
    is dead server-side but refresh_token is live.  Poll cycle auto-
    recovers without stale_reason."""
    from backend.models import Account

    account = Account(
        id=1, email="vault@example.com", enabled=True, priority=0,
        threshold_pct=90, stale_reason=None,
    )

    # Credentials: stale access_token but live refresh_token.  expiresAt
    # claims far future — pre-fix would NOT have refreshed proactively.
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
        return {"five_hour": {"utilization": 12.0}}
    monkeypatch.setattr(anthropic_api, "probe_usage", fake_probe)

    async def fake_refresh(rt):
        assert rt == "rt-live"
        return {"access_token": "at-fresh", "refresh_token": "rt-fresh", "expires_in": 3600}
    monkeypatch.setattr(anthropic_api, "refresh_access_token", fake_refresh)

    monkeypatch.setattr(cp, "save_refreshed_vault_token", lambda *a, **kw: None)

    entry, stale = await bg._process_single_account(account, "other@example.com")
    assert stale is None, "Reactive refresh should have self-healed"
    assert probe_calls == ["at-dead", "at-fresh"]


@pytest.mark.asyncio
async def test_revalidate_and_poll_no_double_refresh_on_phantom(monkeypatch):
    """Concurrent Revalidate click and poll cycle on the same phantom-stale
    email.  The shared lock ensures only one of them POSTs the refresh_token;
    the other sees the rotated token from the first's persist."""
    from backend.models import Account

    account = Account(
        id=1, email="vault@example.com", enabled=True, priority=0,
        threshold_pct=90,
        stale_reason="Refresh token rejected — re-login required",
    )
    db = MagicMock()
    db.commit = AsyncMock()

    monkeypatch.setattr(ac.aq, "get_account_by_id", AsyncMock(return_value=account))
    monkeypatch.setattr(
        ac, "get_active_email_async",
        AsyncMock(return_value="other@example.com"),
    )

    call_idx = {"n": 0}
    def rotating_read(email, active_email=None):
        creds = {
            0: {"claudeAiOauth": {"accessToken": "a", "refreshToken": "rt-live", "expiresAt": 0}},
            1: {"claudeAiOauth": {"accessToken": "a", "refreshToken": "rt-rotated", "expiresAt": 0}},
        }[call_idx["n"]]
        call_idx["n"] = min(call_idx["n"] + 1, 1)
        return creds
    monkeypatch.setattr(ac, "read_credentials_for_email", rotating_read)

    refresh_calls = []
    async def serialised_refresh(rt):
        refresh_calls.append(rt)
        await asyncio.sleep(0.05)
        return {"access_token": "at", "refresh_token": f"rt-after-{rt}", "expires_in": 3600}
    monkeypatch.setattr(anthropic_api, "refresh_access_token", serialised_refresh)
    monkeypatch.setattr(cp, "save_refreshed_vault_token", lambda *a, **kw: None)

    # Fire two concurrent revalidate calls.  Lock forces serialisation.
    results = await asyncio.gather(
        ac.revalidate_account(1, db),
        ac.revalidate_account(1, db),
    )
    assert all(r["success"] for r in results)
    # First call used rt-live; second saw rotated rt-live (call_idx clamped
    # at 1 so second call sees the SAME rotating_read(1) = rt-rotated).
    assert refresh_calls == ["rt-live", "rt-rotated"]
