"""
Tests for backend.background.

background.py now:
- reads per-account threshold from account.threshold_pct instead of a global Setting
- calls account_service.get_access_token_from_config_dir(account.config_dir)
- calls account_service.get_active_email() (no args)
- gates all work behind service_enabled setting — the old auto_switch_enabled
  sub-flag has been removed; service_enabled now *is* the auto-switch decision
- delegates auto-switch decisions to switcher.maybe_auto_switch, which also
  fires a tmux nudge (tmux_service.fire_nudge) after every successful switch
  when the user has opted in via settings.
"""
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.fixture(autouse=True)
def clear_backoff_state():
    """Reset module-level 429 backoff state between tests."""
    import backend.background as bg_mod
    bg_mod._backoff_until.clear()
    bg_mod._backoff_count.clear()
    yield
    bg_mod._backoff_until.clear()
    bg_mod._backoff_count.clear()


@pytest.mark.asyncio
async def test_poll_broadcasts_usage_updated_when_service_enabled():
    """When service_enabled=true and no accounts exist, still broadcasts usage_updated."""
    from backend.background import poll_usage_and_switch

    mock_ws = AsyncMock()
    mock_db = AsyncMock()

    def make_setting(value):
        s = MagicMock()
        s.value = value
        return s

    call_count = [0]

    def execute_side_effect(query):
        result = MagicMock()
        call_count[0] += 1
        if call_count[0] == 1:  # accounts query (first DB hit post-refactor)
            result.scalars.return_value.all.return_value = []
        elif call_count[0] == 2:  # service_enabled inside maybe_auto_switch → "true" to proceed
            result.scalars.return_value.first.return_value = make_setting("true")
        else:
            result.scalars.return_value.first.return_value = None
            result.scalars.return_value.all.return_value = []
        return result

    mock_db.execute = AsyncMock(side_effect=execute_side_effect)

    with patch("backend.background.AsyncSessionLocal") as mock_session_cls, \
         patch("backend.services.switcher.ac.get_active_email", return_value=None):
        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        await poll_usage_and_switch(mock_ws)

    # Should broadcast usage_updated with empty accounts list
    mock_ws.broadcast.assert_called_once()
    call_data = mock_ws.broadcast.call_args[0][0]
    assert call_data["type"] == "usage_updated"


@pytest.mark.asyncio
async def test_poll_disabled_still_broadcasts_usage_but_skips_switch():
    """When service_enabled=false, polling still runs so the dashboard's
    usage bars stay live — only the auto-switch decision inside
    maybe_auto_switch is skipped.
    """
    from backend.background import poll_usage_and_switch

    mock_ws = AsyncMock()
    mock_db = AsyncMock()

    def make_setting(value):
        s = MagicMock()
        s.value = value
        return s

    call_count = [0]

    def execute_side_effect(query):
        result = MagicMock()
        call_count[0] += 1
        if call_count[0] == 1:  # accounts query
            result.scalars.return_value.all.return_value = []
        elif call_count[0] == 2:  # service_enabled inside maybe_auto_switch → "false" → return
            result.scalars.return_value.first.return_value = make_setting("false")
        else:
            result.scalars.return_value.first.return_value = None
            result.scalars.return_value.all.return_value = []
        return result

    mock_db.execute = AsyncMock(side_effect=execute_side_effect)

    with patch("backend.background.AsyncSessionLocal") as mock_session_cls, \
         patch("backend.services.switcher.perform_switch", new_callable=AsyncMock) as mock_switch:
        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        await poll_usage_and_switch(mock_ws)

    # Polling always broadcasts usage_updated, even with zero accounts.
    mock_ws.broadcast.assert_called_once()
    assert mock_ws.broadcast.call_args[0][0]["type"] == "usage_updated"
    # No auto-switch fires because service_enabled=false short-circuits maybe_auto_switch.
    mock_switch.assert_not_called()


# ── Race-condition fix: error path reads and writes cache inside one lock ──────

def _make_account(email, config_dir="/tmp/fake", priority=0):
    a = MagicMock()
    a.id = 1
    a.email = email
    a.config_dir = config_dir
    a.priority = priority
    a.threshold_pct = 95.0
    return a


def _make_db_for_one_account(account):
    """Return a mock async DB that yields one account for the probe loop and
    stops maybe_auto_switch cold via service_enabled=false on its own query.

    Post-refactor call order (poll_usage_and_switch no longer gates on
    service_enabled before polling — usage bars are always live):
      1. accounts query   → [account]
      2. service_enabled  → "false" (inside maybe_auto_switch, triggers early return)
    """
    mock_db = AsyncMock()

    def make_setting(value):
        s = MagicMock()
        s.value = value
        return s

    call_count = [0]

    def execute_side_effect(query):
        result = MagicMock()
        call_count[0] += 1
        if call_count[0] == 1:          # accounts
            result.scalars.return_value.all.return_value = [account]
        elif call_count[0] == 2:        # service_enabled (inside maybe_auto_switch)
            result.scalars.return_value.first.return_value = make_setting("false")
        else:
            result.scalars.return_value.first.return_value = None
            result.scalars.return_value.all.return_value = []
        return result

    mock_db.execute = AsyncMock(side_effect=execute_side_effect)
    return mock_db


@pytest.mark.asyncio
async def test_rate_limited_error_preserves_previous_data():
    """
    When probe_usage raises a 429-like error and a valid previous entry exists,
    the cache entry must gain rate_limited=True while keeping the original data.
    This verifies the race-condition fix: read + write happen inside one lock.
    """
    import httpx
    from backend.background import poll_usage_and_switch
    from backend.cache import cache

    email = "rate-limited@example.com"
    previous_data = {
        "five_hour": {"utilization": 50.0, "resets_at": "2099-01-01T00:00:00Z"},
        "seven_day": {"utilization": 20.0, "resets_at": "2099-01-01T00:00:00Z"},
    }

    # Pre-populate the cache with a valid entry
    await cache.set_usage(email, dict(previous_data))

    account = _make_account(email)
    mock_ws = AsyncMock()
    mock_db = _make_db_for_one_account(account)

    # Simulate a 429 rate-limit error from probe_usage
    rate_limit_exc = Exception("HTTP 429 rate_limit exceeded")

    with patch("backend.background.AsyncSessionLocal") as mock_session_cls, \
         patch("backend.background.ac.get_access_token_from_config_dir", return_value="tok"), \
         patch("backend.background.ac.get_token_info", return_value={}), \
         patch("backend.background.ac.save_refreshed_token"), \
         patch("backend.background.anthropic_api.probe_usage",
               new_callable=AsyncMock, side_effect=rate_limit_exc), \
         patch("backend.services.switcher.ac.get_active_email", return_value=None):
        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        await poll_usage_and_switch(mock_ws)

    entry = await cache.get_usage_async(email)

    assert entry.get("rate_limited") is True, "cache entry must have rate_limited=True"
    # Original usage data must be preserved
    assert "five_hour" in entry, "five_hour key must be preserved after rate-limit"
    assert "seven_day" in entry, "seven_day key must be preserved after rate-limit"
    assert "error" not in entry, "error key must not be present when rate-limited with prev data"

    # Cleanup
    await cache.invalidate(email)


@pytest.mark.asyncio
async def test_probe_401_marks_account_stale():
    """A 401 from probe_usage should set account.stale_reason so the UI can
    show a re-login prompt, and db.commit should be called once."""
    import httpx
    from backend.background import poll_usage_and_switch
    from backend.cache import cache

    email = "stale@example.com"
    await cache.invalidate(email)

    account = _make_account(email)
    account.stale_reason = None

    mock_ws = AsyncMock()
    mock_db = _make_db_for_one_account(account)
    mock_db.commit = AsyncMock()

    # Fake a 401 HTTP error from probe_usage
    resp = MagicMock()
    resp.status_code = 401
    resp.json = MagicMock(return_value={"error": {"message": "invalid token"}})
    http_err = httpx.HTTPStatusError("401", request=MagicMock(), response=resp)

    with patch("backend.background.AsyncSessionLocal") as mock_session_cls, \
         patch("backend.background.ac.get_access_token_from_config_dir", return_value="tok"), \
         patch("backend.background.ac.get_token_info", return_value={}), \
         patch("backend.background.ac.save_refreshed_token"), \
         patch("backend.background.anthropic_api.probe_usage",
               new_callable=AsyncMock, side_effect=http_err), \
         patch("backend.services.switcher.ac.get_active_email", return_value=None):
        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        await poll_usage_and_switch(mock_ws)

    assert account.stale_reason is not None
    assert "401" in account.stale_reason or "re-login" in account.stale_reason.lower()
    mock_db.commit.assert_called()

    await cache.invalidate(email)


@pytest.mark.asyncio
async def test_successful_probe_clears_stale_flag():
    """When a previously-stale account's probe succeeds, the stale flag clears."""
    from backend.background import poll_usage_and_switch
    from backend.cache import cache

    email = "recovered@example.com"
    await cache.invalidate(email)

    account = _make_account(email)
    account.stale_reason = "Previously stale"  # seeded

    mock_ws = AsyncMock()
    mock_db = _make_db_for_one_account(account)
    mock_db.commit = AsyncMock()

    with patch("backend.background.AsyncSessionLocal") as mock_session_cls, \
         patch("backend.background.ac.get_access_token_from_config_dir", return_value="tok"), \
         patch("backend.background.ac.get_token_info", return_value={}), \
         patch("backend.background.ac.save_refreshed_token"), \
         patch("backend.background.anthropic_api.probe_usage",
               new_callable=AsyncMock, return_value={"five_hour": {"utilization": 10}}), \
         patch("backend.services.switcher.ac.get_active_email", return_value=None):
        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        await poll_usage_and_switch(mock_ws)

    assert account.stale_reason is None
    mock_db.commit.assert_called()

    await cache.invalidate(email)


@pytest.mark.asyncio
async def test_generic_error_sets_error_entry():
    """
    When probe_usage raises a generic (non-rate-limit) error and the cache is
    empty, the cache entry must have an 'error' field and no usage data.
    """
    from backend.background import poll_usage_and_switch
    from backend.cache import cache

    email = "error-account@example.com"

    # Ensure the cache starts empty for this account
    await cache.invalidate(email)

    account = _make_account(email)
    mock_ws = AsyncMock()
    mock_db = _make_db_for_one_account(account)

    generic_exc = RuntimeError("something went wrong internally")

    with patch("backend.background.AsyncSessionLocal") as mock_session_cls, \
         patch("backend.background.ac.get_access_token_from_config_dir", return_value="tok"), \
         patch("backend.background.ac.get_token_info", return_value={}), \
         patch("backend.background.ac.save_refreshed_token"), \
         patch("backend.background.anthropic_api.probe_usage",
               new_callable=AsyncMock, side_effect=generic_exc), \
         patch("backend.services.switcher.ac.get_active_email", return_value=None):
        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        await poll_usage_and_switch(mock_ws)

    entry = await cache.get_usage_async(email)

    assert "error" in entry, "cache entry must have an 'error' key after generic failure"
    assert entry["error"], "error message must be non-empty"
    assert "rate_limited" not in entry, "rate_limited must not appear on a generic error"

    # Cleanup
    await cache.invalidate(email)


@pytest.mark.asyncio
async def test_refresh_token_401_marks_permanently_expired():
    """When refresh_access_token returns 401, the token should be marked as
    permanently expired (expires_at=1) to prevent infinite retry."""
    import httpx
    from backend.background import poll_usage_and_switch

    mock_ws = AsyncMock()

    def make_setting(value):
        s = MagicMock()
        s.value = value
        return s

    # Create a mock account with a token that's about to expire
    mock_account = MagicMock()
    mock_account.id = 1
    mock_account.email = "test@example.com"
    mock_account.config_dir = "/tmp/fake-account"
    mock_account.enabled = True
    mock_account.threshold_pct = 95.0
    mock_account.stale_reason = None

    mock_db = AsyncMock()
    mock_db.commit = AsyncMock()

    call_count = [0]

    def execute_side_effect(query):
        result = MagicMock()
        call_count[0] += 1
        if call_count[0] == 1:  # accounts query (first DB hit post-refactor)
            result.scalars.return_value.all.return_value = [mock_account]
        elif call_count[0] == 2:  # service_enabled inside maybe_auto_switch → "false" → return
            result.scalars.return_value.first.return_value = make_setting("false")
        else:
            result.scalars.return_value.first.return_value = None
            result.scalars.return_value.all.return_value = []
        return result

    mock_db.execute = AsyncMock(side_effect=execute_side_effect)

    mock_401_response = MagicMock()
    mock_401_response.status_code = 401
    mock_401_error = httpx.HTTPStatusError(
        "401 Unauthorized", request=MagicMock(), response=mock_401_response
    )

    # Token expires in 60 s — within the 5-minute refresh buffer
    future_expires = int(time.time() * 1000) + 60_000

    with patch("backend.background.AsyncSessionLocal") as mock_session_cls, \
         patch("backend.background.ac.get_access_token_from_config_dir",
               return_value="old-access-token"), \
         patch("backend.background.ac.get_token_info",
               return_value={"token_expires_at": future_expires}), \
         patch("backend.background.ac.get_refresh_token_from_config_dir",
               return_value="old-refresh-token"), \
         patch("backend.background.anthropic_api.refresh_access_token",
               new_callable=AsyncMock, side_effect=mock_401_error) as mock_refresh, \
         patch("backend.background.ac.save_refreshed_token") as mock_save, \
         patch("backend.services.switcher.ac.get_active_email", return_value=None):

        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        await poll_usage_and_switch(mock_ws)

    # Verify refresh was attempted with the stored refresh token
    mock_refresh.assert_called_once_with("old-refresh-token")

    # Verify token was marked as permanently expired (expires_at=1).
    # Accept either positional (arg[2]) or keyword form — background.py wraps
    # the call in asyncio.to_thread which passes args positionally.
    mock_save.assert_called_once()
    args, kwargs = mock_save.call_args
    expires_at = kwargs.get("expires_at", args[2] if len(args) >= 3 else None)
    assert expires_at == 1, (
        f"save_refreshed_token should be called with expires_at=1, got: {mock_save.call_args}"
    )


# ── Auto-switch tests (_maybe_auto_switch exercised via poll_usage_and_switch) ──


def _make_db_for_auto_switch(account, current_account=None, next_account=None,
                             tmux_nudge_enabled="false"):
    """Return a mock async DB for auto-switch scenarios.

    Post-refactor call order:
      1. accounts query (the one account to poll)
      2. service_enabled (inside maybe_auto_switch) → "true"
      3. get_account_by_email (current active account)
      4. get_next_account
      5. (if switch fires) tmux_nudge_enabled setting
    """
    mock_db = AsyncMock()
    mock_db.commit = AsyncMock()

    def make_setting(value):
        s = MagicMock()
        s.value = value
        return s

    call_count = [0]

    def execute_side_effect(query):
        result = MagicMock()
        call_count[0] += 1
        if call_count[0] == 1:          # accounts
            result.scalars.return_value.all.return_value = [account]
        elif call_count[0] == 2:        # service_enabled (inside maybe_auto_switch)
            result.scalars.return_value.first.return_value = make_setting("true")
        elif call_count[0] == 3:        # get_account_by_email (current)
            result.scalars.return_value.first.return_value = current_account
        elif call_count[0] == 4:        # get_next_account (uses .all() + iterate)
            result.scalars.return_value.all.return_value = (
                [next_account] if next_account is not None else []
            )
        elif call_count[0] == 5:        # tmux_nudge_enabled
            result.scalars.return_value.first.return_value = make_setting(tmux_nudge_enabled)
        else:
            result.scalars.return_value.first.return_value = None
            result.scalars.return_value.all.return_value = []
        return result

    mock_db.execute = AsyncMock(side_effect=execute_side_effect)
    return mock_db


@pytest.mark.asyncio
async def test_auto_switch_triggered_when_threshold_exceeded():
    """When usage exceeds the account's threshold_pct, perform_switch is called."""
    from backend.background import poll_usage_and_switch
    from backend.cache import cache

    active_email = "active@example.com"
    next_email = "next@example.com"

    active_account = _make_account(active_email)
    active_account.threshold_pct = 80.0
    active_account.stale_reason = None

    next_account = _make_account(next_email, config_dir="/tmp/next", priority=1)
    next_account.id = 2

    # Pre-populate cache with usage above threshold (85% > 80%)
    await cache.set_usage(active_email, {
        "five_hour": {"utilization": 85.0, "resets_at": "2099-01-01T00:00:00Z"},
    })

    mock_ws = AsyncMock()
    mock_db = _make_db_for_auto_switch(
        account=active_account,
        current_account=active_account,
        next_account=next_account,
    )

    with patch("backend.background.AsyncSessionLocal") as mock_session_cls, \
         patch("backend.background.ac.get_access_token_from_config_dir", return_value="tok"), \
         patch("backend.background.ac.get_token_info", return_value={}), \
         patch("backend.background.ac.save_refreshed_token"), \
         patch("backend.background.anthropic_api.probe_usage",
               new_callable=AsyncMock, return_value={
                   "five_hour": {"utilization": 85.0, "resets_at": "2099-01-01T00:00:00Z"},
               }), \
         patch("backend.services.switcher.ac.get_active_email", return_value=active_email), \
         patch("backend.services.switcher.perform_switch", new_callable=AsyncMock) as mock_switch, \
         patch("backend.services.switcher.tmux_service.fire_nudge"):
        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        await poll_usage_and_switch(mock_ws)

    mock_switch.assert_called_once()
    call_args = mock_switch.call_args
    assert call_args[0][0] is next_account, "perform_switch should be called with the next account"
    assert call_args[0][1] == "threshold", "perform_switch reason should be 'threshold'"

    await cache.invalidate(active_email)


@pytest.mark.asyncio
async def test_auto_switch_not_triggered_below_threshold():
    """When usage is below the account's threshold_pct, perform_switch is NOT called."""
    from backend.background import poll_usage_and_switch
    from backend.cache import cache

    active_email = "below-threshold@example.com"

    active_account = _make_account(active_email)
    active_account.threshold_pct = 80.0
    active_account.stale_reason = None

    # Pre-populate cache with usage below threshold (70% < 80%)
    await cache.set_usage(active_email, {
        "five_hour": {"utilization": 70.0, "resets_at": "2099-01-01T00:00:00Z"},
    })

    mock_ws = AsyncMock()
    mock_db = _make_db_for_auto_switch(
        account=active_account,
        current_account=active_account,
    )

    with patch("backend.background.AsyncSessionLocal") as mock_session_cls, \
         patch("backend.background.ac.get_access_token_from_config_dir", return_value="tok"), \
         patch("backend.background.ac.get_token_info", return_value={}), \
         patch("backend.background.ac.save_refreshed_token"), \
         patch("backend.background.anthropic_api.probe_usage",
               new_callable=AsyncMock, return_value={
                   "five_hour": {"utilization": 70.0, "resets_at": "2099-01-01T00:00:00Z"},
               }), \
         patch("backend.services.switcher.ac.get_active_email", return_value=active_email), \
         patch("backend.services.switcher.perform_switch", new_callable=AsyncMock) as mock_switch:
        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        await poll_usage_and_switch(mock_ws)

    mock_switch.assert_not_called()

    await cache.invalidate(active_email)


@pytest.mark.asyncio
async def test_auto_switch_no_eligible_next_account():
    """When usage exceeds threshold but no next account is available,
    perform_switch is NOT called and an error is broadcast."""
    from backend.background import poll_usage_and_switch
    from backend.cache import cache

    active_email = "no-next@example.com"

    active_account = _make_account(active_email)
    active_account.threshold_pct = 80.0
    active_account.stale_reason = None

    # Usage above threshold but no next account
    await cache.set_usage(active_email, {
        "five_hour": {"utilization": 90.0, "resets_at": "2099-01-01T00:00:00Z"},
    })

    mock_ws = AsyncMock()
    mock_db = _make_db_for_auto_switch(
        account=active_account,
        current_account=active_account,
        next_account=None,  # no eligible account
    )

    with patch("backend.background.AsyncSessionLocal") as mock_session_cls, \
         patch("backend.background.ac.get_access_token_from_config_dir", return_value="tok"), \
         patch("backend.background.ac.get_token_info", return_value={}), \
         patch("backend.background.ac.save_refreshed_token"), \
         patch("backend.background.anthropic_api.probe_usage",
               new_callable=AsyncMock, return_value={
                   "five_hour": {"utilization": 90.0, "resets_at": "2099-01-01T00:00:00Z"},
               }), \
         patch("backend.services.switcher.ac.get_active_email", return_value=active_email), \
         patch("backend.services.switcher.perform_switch", new_callable=AsyncMock) as mock_switch:
        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        await poll_usage_and_switch(mock_ws)

    mock_switch.assert_not_called()

    # Should have broadcast an error message (second broadcast after usage_updated)
    assert mock_ws.broadcast.call_count >= 2, "Expected at least 2 broadcasts (usage_updated + error)"
    error_call = mock_ws.broadcast.call_args_list[-1]
    error_data = error_call[0][0]
    assert error_data["type"] == "error"
    assert "no eligible" in error_data["message"].lower()

    await cache.invalidate(active_email)


# ── Regression: stale accounts must NOT trigger refresh_access_token ─────────


@pytest.mark.asyncio
async def test_stale_account_skips_token_refresh():
    """
    Regression guard for backend/background.py::_process_single_account.

    When an account already has a non-null ``stale_reason``, its refresh token
    is known to be revoked. Calling ``refresh_access_token`` on it again would
    just produce a 401 on every poll cycle, flooding logs and wasting API calls.
    The ``if not account.stale_reason:`` guard around the refresh block prevents
    this — if that guard is accidentally removed in a future refactor, this
    test should fail.
    """
    import httpx
    from backend.background import poll_usage_and_switch
    from backend.cache import cache

    email = "already-stale@example.com"
    await cache.invalidate(email)

    account = _make_account(email)
    account.stale_reason = "Refresh token revoked — re-login required"  # already stale

    mock_ws = AsyncMock()
    mock_db = _make_db_for_one_account(account)
    mock_db.commit = AsyncMock()

    # Token info would normally trigger a refresh (expires in 60 s — inside
    # the 5-minute buffer). We want to prove the guard skips the refresh
    # anyway because the account is already stale.
    future_expires = int(time.time() * 1000) + 60_000

    # Probe raises 401 (account stays stale), so the control flow lands in
    # the exception handler without ever needing a real probe response.
    probe_resp = MagicMock()
    probe_resp.status_code = 401
    probe_resp.json = MagicMock(return_value={"error": {"message": "invalid token"}})
    probe_401 = httpx.HTTPStatusError("401", request=MagicMock(), response=probe_resp)

    with patch("backend.background.AsyncSessionLocal") as mock_session_cls, \
         patch("backend.background.ac.get_access_token_from_config_dir",
               return_value="old-access-token"), \
         patch("backend.background.ac.get_token_info",
               return_value={"token_expires_at": future_expires}), \
         patch("backend.background.ac.get_refresh_token_from_config_dir",
               return_value="old-refresh-token"), \
         patch("backend.background.ac.save_refreshed_token"), \
         patch("backend.background.anthropic_api.refresh_access_token",
               new_callable=AsyncMock) as mock_refresh, \
         patch("backend.background.anthropic_api.probe_usage",
               new_callable=AsyncMock, side_effect=probe_401), \
         patch("backend.services.switcher.ac.get_active_email", return_value=None):
        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        await poll_usage_and_switch(mock_ws)

    # The guard must have skipped the refresh call entirely.
    mock_refresh.assert_not_called()

    await cache.invalidate(email)


@pytest.mark.asyncio
async def test_non_stale_account_triggers_token_refresh():
    """
    Control case for the stale-skip guard: when ``stale_reason is None`` and
    the token is about to expire, ``refresh_access_token`` MUST be called.
    This makes the companion ``test_stale_account_skips_token_refresh``
    meaningful — without this pair, a bug that disables refresh entirely
    would still pass the negative assertion.
    """
    import httpx
    from backend.background import poll_usage_and_switch
    from backend.cache import cache

    email = "healthy@example.com"
    await cache.invalidate(email)

    account = _make_account(email)
    account.stale_reason = None  # healthy

    mock_ws = AsyncMock()
    mock_db = _make_db_for_one_account(account)
    mock_db.commit = AsyncMock()

    future_expires = int(time.time() * 1000) + 60_000  # expires in 60 s

    # A 401 from the probe keeps the test footprint small — the refresh
    # path still executes before the probe is attempted.
    probe_resp = MagicMock()
    probe_resp.status_code = 401
    probe_resp.json = MagicMock(return_value={"error": {"message": "invalid token"}})
    probe_401 = httpx.HTTPStatusError("401", request=MagicMock(), response=probe_resp)

    with patch("backend.background.AsyncSessionLocal") as mock_session_cls, \
         patch("backend.background.ac.get_access_token_from_config_dir",
               return_value="old-access-token"), \
         patch("backend.background.ac.get_token_info",
               return_value={"token_expires_at": future_expires}), \
         patch("backend.background.ac.get_refresh_token_from_config_dir",
               return_value="old-refresh-token"), \
         patch("backend.background.ac.save_refreshed_token"), \
         patch("backend.background.anthropic_api.refresh_access_token",
               new_callable=AsyncMock,
               return_value={"access_token": "new-token", "refresh_token": "new-rt", "expires_in": 3600}) as mock_refresh, \
         patch("backend.background.anthropic_api.probe_usage",
               new_callable=AsyncMock, side_effect=probe_401), \
         patch("backend.services.switcher.ac.get_active_email", return_value=None):
        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        await poll_usage_and_switch(mock_ws)

    # The healthy account must have attempted a refresh with its stored token.
    mock_refresh.assert_called_once_with("old-refresh-token")

    await cache.invalidate(email)


# ── Active-ownership refresh model (E2) ───────────────────────────────────────
#
# The poll loop must not refresh the access token of the account whose
# config_dir matches ``~/.ccswitch/active`` — that refresh lifecycle belongs
# to Claude Code CLI.  When the active account's access token has expired
# and no CLI is running, a probe returns 401 and the poll loop enters a
# "soft waiting" state (no stale_reason, no error broadcast) until either
# Claude Code refreshes via its own lifecycle or the user clicks Force
# refresh in the dashboard.  These tests protect that invariant and the
# waiting-flag bookkeeping that drives the UI.


@pytest.mark.asyncio
async def test_active_account_skipped_from_refresh():
    """The active account's token refresh is OWNED by Claude Code CLI — the
    poll loop must skip calling refresh_access_token even when the stored
    token is about to expire.  Instead the probe runs directly with the
    existing access token and whatever outcome that yields is what the UI
    sees for that cycle."""
    import httpx
    from backend.background import poll_usage_and_switch
    from backend.cache import cache

    email = "active-no-refresh@example.com"
    await cache.invalidate(email)

    account = _make_account(email, config_dir="/tmp/active-dir")
    account.stale_reason = None

    mock_ws = AsyncMock()
    mock_db = _make_db_for_one_account(account)
    mock_db.commit = AsyncMock()

    future_expires = int(time.time() * 1000) + 60_000  # 60 s — inside refresh buffer

    # The active_cfg_dir pointer resolves to THIS account's config dir, so the
    # poll loop classifies this account as active and must skip refresh.
    with patch("backend.background.AsyncSessionLocal") as mock_session_cls, \
         patch("backend.background.ac.get_active_config_dir_pointer",
               return_value="/tmp/active-dir"), \
         patch("backend.background.ac.get_access_token_from_config_dir",
               return_value="existing-token"), \
         patch("backend.background.ac.get_token_info",
               return_value={"token_expires_at": future_expires}), \
         patch("backend.background.ac.get_refresh_token_from_config_dir",
               return_value="rt"), \
         patch("backend.background.ac.save_refreshed_token"), \
         patch("backend.background.anthropic_api.refresh_access_token",
               new_callable=AsyncMock) as mock_refresh, \
         patch("backend.background.anthropic_api.probe_usage",
               new_callable=AsyncMock,
               return_value={"five_hour": {"utilization": 10}}), \
         patch("backend.services.switcher.ac.get_active_email", return_value=None):
        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        await poll_usage_and_switch(mock_ws)

    # The active-ownership invariant: the poll loop must NOT refresh the
    # active account — that is Claude Code CLI's job.
    mock_refresh.assert_not_called()

    await cache.invalidate(email)


@pytest.mark.asyncio
async def test_active_account_401_enters_waiting_state():
    """Active account + expired access token + no CLI: the probe 401s, the
    Keychain re-read retry also 401s, and the poll loop must enter the soft
    waiting state — NOT mark the account stale.  The broadcast usage_entry
    must have waiting_for_cli=True, and cache.is_waiting_async(email) must
    return True so a subsequent GET /api/accounts also sees waiting."""
    import httpx
    from backend.background import poll_usage_and_switch
    from backend.cache import cache

    email = "active-waiting@example.com"
    await cache.invalidate(email)

    account = _make_account(email, config_dir="/tmp/waiting-dir")
    account.stale_reason = None

    mock_ws = AsyncMock()
    mock_db = _make_db_for_one_account(account)
    mock_db.commit = AsyncMock()

    probe_resp = MagicMock()
    probe_resp.status_code = 401
    probe_resp.json = MagicMock(return_value={"error": {"message": "expired"}})
    probe_401 = httpx.HTTPStatusError("401", request=MagicMock(), response=probe_resp)

    with patch("backend.background.AsyncSessionLocal") as mock_session_cls, \
         patch("backend.background.ac.get_active_config_dir_pointer",
               return_value="/tmp/waiting-dir"), \
         patch("backend.background.ac.get_access_token_from_config_dir",
               return_value="stale-token"), \
         patch("backend.background.ac.get_token_info",
               return_value={}), \
         patch("backend.background.anthropic_api.probe_usage",
               new_callable=AsyncMock, side_effect=probe_401), \
         patch("backend.services.switcher.ac.get_active_email", return_value=email):
        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        await poll_usage_and_switch(mock_ws)

    # Soft waiting: the account must NOT be marked stale.
    assert account.stale_reason is None, (
        "active account with 401 probe must stay non-stale — stale_reason "
        "would prevent re-probe on the next cycle"
    )
    # The cache-backed flag must be set so GET /api/accounts returns True.
    assert await cache.is_waiting_async(email) is True, (
        "cache waiting flag should be set by the active-401 branch"
    )
    # The WS broadcast must carry waiting_for_cli=True for the active account.
    broadcast_args = mock_ws.broadcast.call_args_list[0][0][0]
    assert broadcast_args["type"] == "usage_updated"
    entry = next(
        e for e in broadcast_args["accounts"] if e["email"] == email
    )
    assert entry["waiting_for_cli"] is True

    await cache.invalidate(email)


@pytest.mark.asyncio
async def test_inactive_account_401_still_marks_stale():
    """Active-ownership only defers refresh for the ACTIVE account.  An
    inactive account that returns 401 from the probe must still be marked
    stale (CCSwitch is the sole refresh consumer for inactive accounts —
    a persistent 401 means the refresh token is dead)."""
    import httpx
    from backend.background import poll_usage_and_switch
    from backend.cache import cache

    email = "inactive-stale@example.com"
    await cache.invalidate(email)

    account = _make_account(email, config_dir="/tmp/inactive-dir")
    account.stale_reason = None

    mock_ws = AsyncMock()
    mock_db = _make_db_for_one_account(account)
    mock_db.commit = AsyncMock()

    probe_resp = MagicMock()
    probe_resp.status_code = 401
    probe_resp.json = MagicMock(return_value={"error": {"message": "expired"}})
    probe_401 = httpx.HTTPStatusError("401", request=MagicMock(), response=probe_resp)

    # Active pointer resolves to a DIFFERENT config dir, so this account is
    # NOT active → the waiting branch must not fire.
    with patch("backend.background.AsyncSessionLocal") as mock_session_cls, \
         patch("backend.background.ac.get_active_config_dir_pointer",
               return_value="/tmp/some-other-active-dir"), \
         patch("backend.background.ac.get_access_token_from_config_dir",
               return_value="stale-token"), \
         patch("backend.background.ac.get_token_info",
               return_value={}), \
         patch("backend.background.ac.get_refresh_token_from_config_dir",
               return_value="rt"), \
         patch("backend.background.ac.save_refreshed_token"), \
         patch("backend.background.anthropic_api.probe_usage",
               new_callable=AsyncMock, side_effect=probe_401), \
         patch("backend.services.switcher.ac.get_active_email", return_value=None):
        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        await poll_usage_and_switch(mock_ws)

    # The inactive-401 path must set stale_reason.
    assert account.stale_reason is not None
    # And must NOT set the waiting flag — waiting is reserved for active.
    assert await cache.is_waiting_async(email) is False

    await cache.invalidate(email)


@pytest.mark.asyncio
async def test_successful_probe_clears_waiting_flag():
    """When a previously-waiting account's next probe succeeds, the cache's
    waiting flag must be cleared (otherwise the card would show both a
    green usage bar AND a yellow Waiting pill on the same cycle)."""
    from backend.background import poll_usage_and_switch
    from backend.cache import cache

    email = "recovered-from-waiting@example.com"
    await cache.invalidate(email)
    # Seed the waiting flag so we can verify it clears.
    await cache.set_waiting(email)
    assert await cache.is_waiting_async(email) is True

    account = _make_account(email, config_dir="/tmp/recovered-dir")
    account.stale_reason = None

    mock_ws = AsyncMock()
    mock_db = _make_db_for_one_account(account)
    mock_db.commit = AsyncMock()

    with patch("backend.background.AsyncSessionLocal") as mock_session_cls, \
         patch("backend.background.ac.get_active_config_dir_pointer",
               return_value="/tmp/recovered-dir"), \
         patch("backend.background.ac.get_access_token_from_config_dir",
               return_value="fresh-token"), \
         patch("backend.background.ac.get_token_info",
               return_value={}), \
         patch("backend.background.ac.save_refreshed_token"), \
         patch("backend.background.anthropic_api.probe_usage",
               new_callable=AsyncMock,
               return_value={"five_hour": {"utilization": 10}}), \
         patch("backend.services.switcher.ac.get_active_email", return_value=None):
        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        await poll_usage_and_switch(mock_ws)

    # Waiting flag must be cleared on the success path.
    assert await cache.is_waiting_async(email) is False

    await cache.invalidate(email)


@pytest.mark.asyncio
async def test_top_level_exception_clears_waiting_flag():
    """Regression: if ``_process_single_account`` raises before its own
    inner except clause can clean up, the top-level ``isinstance(result,
    Exception)`` branch in ``poll_usage_and_switch`` must also clear the
    waiting flag — otherwise a subsequent ``GET /api/accounts`` would see
    a stale True while the WS broadcast said False, and the two surfaces
    would disagree."""
    from backend.background import poll_usage_and_switch
    from backend.cache import cache

    email = "waiting-then-crash@example.com"
    await cache.invalidate(email)
    # Seed the waiting flag as if a prior cycle had set it.
    await cache.set_waiting(email)
    assert await cache.is_waiting_async(email) is True

    account = _make_account(email, config_dir="/tmp/crash-dir")
    account.stale_reason = None

    mock_ws = AsyncMock()
    mock_db = _make_db_for_one_account(account)
    mock_db.commit = AsyncMock()

    # Crash inside _process_single_account BEFORE its inner except can fire:
    # get_access_token_from_config_dir raises a bare-SystemError (escapes
    # every except-Exception handler because SystemError is not Exception).
    #
    # Actually SystemError IS an Exception — pick something even more
    # exotic: asyncio.CancelledError, which most except-Exception blocks do
    # NOT catch post-3.8.  That is a realistic crash during lifespan
    # shutdown and is exactly the scenario the top-level clear guards.
    async def _raise_cancelled():
        raise asyncio.CancelledError("lifespan shutdown mid-probe")

    # Wrap the mock in a coroutine so to_thread fires it correctly.
    import asyncio as _aio

    def _crashy_get_token(_cfg_dir):
        raise _aio.CancelledError("lifespan shutdown")

    with patch("backend.background.AsyncSessionLocal") as mock_session_cls, \
         patch("backend.background.ac.get_active_config_dir_pointer",
               return_value="/tmp/crash-dir"), \
         patch("backend.background.ac.get_access_token_from_config_dir",
               side_effect=_crashy_get_token), \
         patch("backend.services.switcher.ac.get_active_email", return_value=None):
        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        # poll_usage_and_switch catches the gather exceptions — no raise.
        await poll_usage_and_switch(mock_ws)

    # The top-level exception branch must have cleared the waiting flag.
    assert await cache.is_waiting_async(email) is False, (
        "top-level exception branch must clear _waiting so WS and REST agree"
    )

    await cache.invalidate(email)


@pytest.mark.asyncio
async def test_usage_entry_carries_stale_reason_in_broadcast():
    """The poll-loop broadcast must include ``stale_reason`` on every
    usage_entry so other open tabs can flip footer buttons immediately on
    a waiting→stale transition, instead of waiting for a full reload."""
    from backend.background import poll_usage_and_switch
    from backend.cache import cache

    email = "broadcast-stale-reason@example.com"
    await cache.invalidate(email)

    account = _make_account(email, config_dir="/tmp/stale-broadcast-dir")
    account.stale_reason = "Previously stale — re-login required"  # seed

    mock_ws = AsyncMock()
    mock_db = _make_db_for_one_account(account)
    mock_db.commit = AsyncMock()

    # A successful probe clears stale_reason on the DB row; the broadcast
    # must carry the NEW (None) value so other tabs see the transition.
    with patch("backend.background.AsyncSessionLocal") as mock_session_cls, \
         patch("backend.background.ac.get_active_config_dir_pointer",
               return_value=None), \
         patch("backend.background.ac.get_access_token_from_config_dir",
               return_value="tok"), \
         patch("backend.background.ac.get_token_info",
               return_value={}), \
         patch("backend.background.ac.save_refreshed_token"), \
         patch("backend.background.anthropic_api.probe_usage",
               new_callable=AsyncMock,
               return_value={"five_hour": {"utilization": 10}}), \
         patch("backend.services.switcher.ac.get_active_email", return_value=None):
        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        await poll_usage_and_switch(mock_ws)

    broadcast_args = mock_ws.broadcast.call_args_list[0][0][0]
    entry = next(
        e for e in broadcast_args["accounts"] if e["email"] == email
    )
    # stale_reason key must exist on every entry, and the success path must
    # have cleared it to None in the broadcast AND on the DB row.
    assert "stale_reason" in entry
    assert entry["stale_reason"] is None
    assert account.stale_reason is None

    await cache.invalidate(email)


@pytest.mark.asyncio
async def test_mid_cycle_active_flip_skips_refresh():
    """Regression for a coverage gap surfaced in the second-round audit:
    the mid-cycle active-flip re-check at ``background.py:95-108`` is the
    defense against a manual switch that flips ``~/.ccswitch/active`` to
    THIS account AFTER the poll cycle snapped its ``active_cfg_dir`` but
    BEFORE this coroutine calls ``refresh_access_token``.  Without the
    re-check, the poll loop would race Claude Code CLI on the newly active
    account's refresh_token and brick it.

    We simulate this by returning ``None`` on the cycle-start pointer read
    (so the account's ``is_active`` snap = False) and then returning the
    account's config_dir on the re-check (so ownership flipped to us
    mid-cycle).  ``refresh_access_token`` must NOT be called.
    """
    from backend.background import poll_usage_and_switch
    from backend.cache import cache

    email = "mid-cycle-flip@example.com"
    await cache.invalidate(email)

    account = _make_account(email, config_dir="/tmp/mid-flip-dir")
    account.stale_reason = None

    mock_ws = AsyncMock()
    mock_db = _make_db_for_one_account(account)
    mock_db.commit = AsyncMock()

    # Token expires in 60 s so the refresh-eligible branch is entered.
    soon_expires = int(time.time() * 1000) + 60_000

    # Simulate the cycle-start snap returning None (no active account) and
    # the mid-cycle re-check returning THIS config_dir (ownership flipped
    # mid-cycle to this account — CLI now owns its refresh).
    pointer_calls = {"count": 0}

    def _pointer_side_effect():
        pointer_calls["count"] += 1
        if pointer_calls["count"] == 1:
            return None  # cycle-start snap: no active account
        return "/tmp/mid-flip-dir"  # mid-cycle re-check: now active

    with patch("backend.background.AsyncSessionLocal") as mock_session_cls, \
         patch("backend.background.ac.get_active_config_dir_pointer",
               side_effect=_pointer_side_effect), \
         patch("backend.background.ac.get_access_token_from_config_dir",
               return_value="existing-token"), \
         patch("backend.background.ac.get_token_info",
               return_value={"token_expires_at": soon_expires}), \
         patch("backend.background.ac.get_refresh_token_from_config_dir",
               return_value="rt"), \
         patch("backend.background.ac.save_refreshed_token"), \
         patch("backend.background.anthropic_api.refresh_access_token",
               new_callable=AsyncMock) as mock_refresh, \
         patch("backend.background.anthropic_api.probe_usage",
               new_callable=AsyncMock,
               return_value={"five_hour": {"utilization": 10}}), \
         patch("backend.services.switcher.ac.get_active_email",
               return_value=None):
        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        await poll_usage_and_switch(mock_ws)

    # The mid-cycle re-check must have seen the pointer flip and skipped
    # the refresh entirely — refresh_access_token must NOT be called for
    # what is now the active account.
    mock_refresh.assert_not_called()

    # The re-check was wired to the second pointer read; if we count only
    # one pointer read, the re-check branch never executed.
    assert pointer_calls["count"] >= 2, (
        "_process_single_account did not perform the mid-cycle active re-check "
        "— the TOCTOU guard against manual switches mid-cycle is not firing"
    )

    await cache.invalidate(email)


@pytest.mark.asyncio
async def test_refresh_skew_constant_matches_20_minutes():
    """Guard the named refresh-skew constant so a reversion to the pre-E2
    5-minute window (the pre-active-ownership value) does not slip through.
    The active-ownership model specifies 20 minutes for inactive accounts
    since CCSwitch is the sole refresher; reducing this shifts the defense
    margin back into the race-prone range."""
    from backend import background as bg

    assert bg._REFRESH_SKEW_MS_INACTIVE == 20 * 60 * 1000, (
        f"_REFRESH_SKEW_MS_INACTIVE changed from 20 min to "
        f"{bg._REFRESH_SKEW_MS_INACTIVE / 60_000:.1f} min — revisit the "
        f"active-ownership design doc before narrowing this"
    )
