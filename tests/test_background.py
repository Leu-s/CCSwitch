"""
Tests for backend.background.

background.py now:
- reads per-account threshold from account.threshold_pct instead of a global Setting
- calls account_service.get_access_token_from_config_dir(account.config_dir)
- calls account_service.get_active_email() (no args)
- gates all work behind service_enabled setting (not auto_switch_enabled)
- notify_monitors is now in tmux_service; tests import it from there directly.
"""
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_poll_broadcasts_usage_updated_when_service_enabled():
    """When service_enabled=true and no accounts exist, still broadcasts usage_updated."""
    from backend.background import poll_usage_and_switch, usage_cache

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
        if call_count[0] == 1:  # service_enabled setting
            result.scalars.return_value.first.return_value = make_setting("true")
        elif call_count[0] == 2:  # accounts query
            result.scalars.return_value.all.return_value = []
        elif call_count[0] == 3:  # auto_switch_enabled setting
            result.scalars.return_value.first.return_value = make_setting("false")
        else:
            result.scalars.return_value.first.return_value = None
            result.scalars.return_value.all.return_value = []
        return result

    mock_db.execute = AsyncMock(side_effect=execute_side_effect)

    with patch("backend.background.AsyncSessionLocal") as mock_session_cls:
        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        await poll_usage_and_switch(mock_ws)

    # Should broadcast usage_updated with empty accounts list
    mock_ws.broadcast.assert_called_once()
    call_data = mock_ws.broadcast.call_args[0][0]
    assert call_data["type"] == "usage_updated"


@pytest.mark.asyncio
async def test_poll_does_nothing_when_service_disabled():
    """When service_enabled=false the function returns immediately without broadcasting."""
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
        if call_count[0] == 1:  # service_enabled setting
            result.scalars.return_value.first.return_value = make_setting("false")
        else:
            result.scalars.return_value.first.return_value = None
            result.scalars.return_value.all.return_value = []
        return result

    mock_db.execute = AsyncMock(side_effect=execute_side_effect)

    with patch("backend.background.AsyncSessionLocal") as mock_session_cls:
        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        await poll_usage_and_switch(mock_ws)

    mock_ws.broadcast.assert_not_called()


@pytest.mark.asyncio
async def test_notify_tmux_monitors_manual_pattern():
    from backend.services.tmux_service import notify_monitors

    mock_ws = AsyncMock()
    monitor = MagicMock()
    monitor.id = 1
    monitor.pattern_type = "manual"
    monitor.pattern = "main:0.0"

    with patch("backend.services.tmux_service.list_panes", return_value=[{"target": "main:0.0", "command": "claude"}]), \
         patch("backend.services.tmux_service.send_continue") as mock_send, \
         patch("backend.services.tmux_service.capture_pane", return_value="some output"), \
         patch("backend.services.tmux_service.evaluate_with_haiku", new_callable=AsyncMock,
               return_value={"status": "SUCCESS", "explanation": "All good", "raw": "SUCCESS All good"}):
        import asyncio
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await notify_monitors([monitor], mock_ws, "claude-haiku-4-5-20251001")

    mock_send.assert_called_once_with("main:0.0")
    mock_ws.broadcast.assert_called_once()
    data = mock_ws.broadcast.call_args[0][0]
    assert data["type"] == "tmux_result"
    assert data["status"] == "SUCCESS"


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
    """Return a mock async DB that yields one account and service_enabled=true."""
    mock_db = AsyncMock()

    def make_setting(value):
        s = MagicMock()
        s.value = value
        return s

    call_count = [0]

    def execute_side_effect(query):
        result = MagicMock()
        call_count[0] += 1
        if call_count[0] == 1:          # service_enabled
            result.scalars.return_value.first.return_value = make_setting("true")
        elif call_count[0] == 2:        # accounts
            result.scalars.return_value.all.return_value = [account]
        elif call_count[0] == 3:        # auto_switch_enabled
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
    from backend.background import poll_usage_and_switch, usage_cache, _cache_lock

    email = "rate-limited@example.com"
    previous_data = {
        "five_hour": {"utilization": 50.0, "resets_at": "2099-01-01T00:00:00Z"},
        "seven_day": {"utilization": 20.0, "resets_at": "2099-01-01T00:00:00Z"},
    }

    # Pre-populate the cache with a valid entry
    async with _cache_lock:
        usage_cache[email] = dict(previous_data)

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
               new_callable=AsyncMock, side_effect=rate_limit_exc):
        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        await poll_usage_and_switch(mock_ws)

    async with _cache_lock:
        entry = usage_cache.get(email, {})

    assert entry.get("rate_limited") is True, "cache entry must have rate_limited=True"
    # Original usage data must be preserved
    assert "five_hour" in entry, "five_hour key must be preserved after rate-limit"
    assert "seven_day" in entry, "seven_day key must be preserved after rate-limit"
    assert "error" not in entry, "error key must not be present when rate-limited with prev data"

    # Cleanup
    async with _cache_lock:
        usage_cache.pop(email, None)


@pytest.mark.asyncio
async def test_probe_401_marks_account_stale():
    """A 401 from probe_usage should set account.stale_reason so the UI can
    show a re-login prompt, and db.commit should be called once."""
    import httpx
    from backend.background import poll_usage_and_switch, usage_cache, _cache_lock

    email = "stale@example.com"
    async with _cache_lock:
        usage_cache.pop(email, None)

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
               new_callable=AsyncMock, side_effect=http_err):
        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        await poll_usage_and_switch(mock_ws)

    assert account.stale_reason is not None
    assert "401" in account.stale_reason or "re-login" in account.stale_reason.lower()
    mock_db.commit.assert_called()

    async with _cache_lock:
        usage_cache.pop(email, None)


@pytest.mark.asyncio
async def test_successful_probe_clears_stale_flag():
    """When a previously-stale account's probe succeeds, the stale flag clears."""
    from backend.background import poll_usage_and_switch, usage_cache, _cache_lock

    email = "recovered@example.com"
    async with _cache_lock:
        usage_cache.pop(email, None)

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
               new_callable=AsyncMock, return_value={"five_hour": {"utilization": 10}}):
        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        await poll_usage_and_switch(mock_ws)

    assert account.stale_reason is None
    mock_db.commit.assert_called()

    async with _cache_lock:
        usage_cache.pop(email, None)


@pytest.mark.asyncio
async def test_generic_error_sets_error_entry():
    """
    When probe_usage raises a generic (non-rate-limit) error and the cache is
    empty, the cache entry must have an 'error' field and no usage data.
    """
    from backend.background import poll_usage_and_switch, usage_cache, _cache_lock

    email = "error-account@example.com"

    # Ensure the cache starts empty for this account
    async with _cache_lock:
        usage_cache.pop(email, None)

    account = _make_account(email)
    mock_ws = AsyncMock()
    mock_db = _make_db_for_one_account(account)

    generic_exc = RuntimeError("something went wrong internally")

    with patch("backend.background.AsyncSessionLocal") as mock_session_cls, \
         patch("backend.background.ac.get_access_token_from_config_dir", return_value="tok"), \
         patch("backend.background.ac.get_token_info", return_value={}), \
         patch("backend.background.ac.save_refreshed_token"), \
         patch("backend.background.anthropic_api.probe_usage",
               new_callable=AsyncMock, side_effect=generic_exc):
        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        await poll_usage_and_switch(mock_ws)

    async with _cache_lock:
        entry = usage_cache.get(email, {})

    assert "error" in entry, "cache entry must have an 'error' key after generic failure"
    assert entry["error"], "error message must be non-empty"
    assert "rate_limited" not in entry, "rate_limited must not appear on a generic error"

    # Cleanup
    async with _cache_lock:
        usage_cache.pop(email, None)


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
        if call_count[0] == 1:  # service_enabled
            result.scalars.return_value.first.return_value = make_setting("true")
        elif call_count[0] == 2:  # accounts query
            result.scalars.return_value.all.return_value = [mock_account]
        elif call_count[0] == 3:  # auto_switch_enabled
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
         patch("backend.background.ac.save_refreshed_token") as mock_save:

        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        await poll_usage_and_switch(mock_ws)

    # Verify refresh was attempted with the stored refresh token
    mock_refresh.assert_called_once_with("old-refresh-token")

    # Verify token was marked as permanently expired (expires_at=1)
    mock_save.assert_called_once()
    call_args = mock_save.call_args
    # Called as: ac.save_refreshed_token(config_dir, token, expires_at=1)
    assert call_args[1].get("expires_at") == 1, (
        f"save_refreshed_token should be called with expires_at=1, got: {call_args}"
    )
