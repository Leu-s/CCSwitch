"""
Integration test for the full poll → detect threshold → auto-switch → notify chain.

Exercises poll_usage_and_switch() end-to-end with two accounts where
account A exceeds its threshold, triggering an auto-switch to account B.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_account(id_, email, priority, threshold_pct=80.0, config_dir="/tmp/fake"):
    a = MagicMock()
    a.id = id_
    a.email = email
    a.config_dir = f"{config_dir}-{id_}"
    a.priority = priority
    a.threshold_pct = threshold_pct
    a.stale_reason = None
    a.enabled = True
    return a


def _make_db(accounts, active_email, next_account):
    """Build a mock DB session that handles the full query sequence:
    1. service_enabled
    2. accounts list
    3. auto_switch_enabled
    4. get_account_by_email (active account lookup)
    5. get_next_account (next eligible)
    6. enabled credential targets (inside perform_switch)
    7. get_account_by_email (from-account in perform_switch)
    """
    mock_db = AsyncMock()

    def make_setting(value):
        s = MagicMock()
        s.value = value
        return s

    by_email = {a.email: a for a in accounts}
    call_count = [0]

    def execute_side_effect(query):
        result = MagicMock()
        call_count[0] += 1
        n = call_count[0]
        if n == 1:  # service_enabled
            result.scalars.return_value.first.return_value = make_setting("true")
        elif n == 2:  # accounts
            result.scalars.return_value.all.return_value = list(accounts)
        elif n == 3:  # auto_switch_enabled
            result.scalars.return_value.first.return_value = make_setting("true")
        elif n == 4:  # get_account_by_email (active)
            result.scalars.return_value.first.return_value = by_email.get(active_email)
        elif n == 5:  # get_next_account
            result.scalars.return_value.first.return_value = next_account
        else:
            result.scalars.return_value.first.return_value = None
            result.scalars.return_value.all.return_value = []
        return result

    mock_db.execute = AsyncMock(side_effect=execute_side_effect)
    mock_db.commit = AsyncMock()
    return mock_db


@pytest.mark.asyncio
async def test_full_poll_switch_notify_chain():
    """Two accounts: A active at 90% (threshold 80%), B idle.
    poll_usage_and_switch should probe both, detect A over threshold,
    and auto-switch to B."""
    from backend.background import poll_usage_and_switch
    from backend.cache import cache

    acct_a = _make_account(1, "a@test.com", priority=0, threshold_pct=80.0)
    acct_b = _make_account(2, "b@test.com", priority=1, threshold_pct=80.0)

    # Pre-populate cache: A at 90% so _maybe_auto_switch sees it
    await cache.set_usage("a@test.com", {
        "five_hour": {"utilization": 90.0, "resets_at": "2099-01-01T00:00:00Z"},
    })

    mock_ws = AsyncMock()
    mock_db = _make_db([acct_a, acct_b], "a@test.com", acct_b)

    # Mock probe results: A at 90%, B at 10%
    probe_results = {
        "tok-1": {"five_hour": {"utilization": 90.0, "resets_at": "2099-01-01T00:00:00Z"}},
        "tok-2": {"five_hour": {"utilization": 10.0, "resets_at": "2099-01-01T00:00:00Z"}},
    }

    def get_token(config_dir):
        return f"tok-{config_dir.split('-')[-1]}"

    mock_perform_switch = AsyncMock()

    with patch("backend.background.AsyncSessionLocal") as mock_session_cls, \
         patch("backend.background.ac.get_access_token_from_config_dir", side_effect=get_token), \
         patch("backend.background.ac.get_token_info", return_value={}), \
         patch("backend.background.ac.save_refreshed_token"), \
         patch("backend.background.anthropic_api.probe_usage",
               new_callable=AsyncMock, side_effect=lambda tok: probe_results[tok]), \
         patch("backend.services.switcher.ac.get_active_email", return_value="a@test.com"), \
         patch("backend.services.switcher.aq.get_account_by_email",
               new_callable=AsyncMock, return_value=acct_a), \
         patch("backend.services.switcher.get_next_account",
               new_callable=AsyncMock, return_value=acct_b), \
         patch("backend.services.switcher.perform_switch", mock_perform_switch), \
         patch("backend.services.switcher.tmux_service.fire_nudge"):
        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        await poll_usage_and_switch(mock_ws)

    # ── Assertions ────────────────────────────────────────────────────────────

    # 1. perform_switch was called once with account B and reason "threshold"
    mock_perform_switch.assert_called_once()
    call_args = mock_perform_switch.call_args
    assert call_args[0][0] is acct_b, "Should switch to account B"
    assert call_args[0][1] == "threshold", "Reason should be 'threshold'"

    # 2. WS broadcast includes usage_updated with both accounts
    usage_broadcast = None
    for call in mock_ws.broadcast.call_args_list:
        payload = call[0][0]
        if payload.get("type") == "usage_updated":
            usage_broadcast = payload
            break
    assert usage_broadcast is not None, "usage_updated broadcast must be sent"
    assert len(usage_broadcast["accounts"]) == 2, "Both accounts should be in the broadcast"

    # 3. Cache was updated for both accounts
    a_usage = await cache.get_usage_async("a@test.com")
    b_usage = await cache.get_usage_async("b@test.com")
    assert a_usage.get("five_hour", {}).get("utilization") == 90.0
    assert b_usage.get("five_hour", {}).get("utilization") == 10.0

    # Cleanup
    await cache.invalidate("a@test.com")
    await cache.invalidate("b@test.com")
