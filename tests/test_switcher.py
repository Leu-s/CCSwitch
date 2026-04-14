"""
Tests for backend.services.switcher.

perform_switch fetches the user-chosen mirror targets from the DB and
passes them to account_service.activate_account_config alongside the target
config dir.
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def make_account(id, email, priority, enabled=True, config_dir=None, stale_reason=None):
    a = MagicMock()
    a.id = id
    a.email = email
    a.priority = priority
    a.enabled = enabled
    a.config_dir = config_dir or f"/tmp/fake-account-{id}"
    a.stale_reason = stale_reason
    return a


def _make_account_for_next(id, email, priority, threshold_pct=80.0):
    a = make_account(id, email, priority)
    a.threshold_pct = threshold_pct
    return a


@pytest.mark.asyncio
async def test_get_next_account_skips_current():
    from backend.services.switcher import get_next_account
    accounts = [_make_account_for_next(2, "b@x.com", 1)]
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = accounts
    mock_db = AsyncMock()
    mock_db.execute.return_value = mock_result
    result = await get_next_account("a@x.com", mock_db)
    assert result.email == "b@x.com"


@pytest.mark.asyncio
async def test_get_next_account_returns_none_when_no_others():
    from backend.services.switcher import get_next_account
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_db = AsyncMock()
    mock_db.execute.return_value = mock_result
    result = await get_next_account("only@x.com", mock_db)
    assert result is None


@pytest.mark.asyncio
async def test_get_next_account_skips_stale():
    """get_next_account must not return an account that has stale_reason set."""
    from backend.services.switcher import get_next_account
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_db = AsyncMock()
    mock_db.execute.return_value = mock_result
    result = await get_next_account("current@x.com", mock_db)
    assert result is None


@pytest.mark.asyncio
async def test_get_next_account_skips_rate_limited_candidate():
    """A candidate whose last probe was rate-limited must be skipped, and
    the next-in-priority account returned instead.
    """
    from backend.services.switcher import get_next_account
    from backend.cache import cache

    a = _make_account_for_next(1, "exhausted@x.com", 0, threshold_pct=80.0)
    b = _make_account_for_next(2, "fresh@x.com", 1, threshold_pct=80.0)

    await cache.set_usage("exhausted@x.com", {
        "rate_limited": True,
        "five_hour": {"utilization": 0, "resets_at": "2099-01-01T00:00:00Z"},
    })
    await cache.set_usage("fresh@x.com", {
        "five_hour": {"utilization": 10.0, "resets_at": "2099-01-01T00:00:00Z"},
    })

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [a, b]
    mock_db = AsyncMock()
    mock_db.execute.return_value = mock_result

    try:
        result = await get_next_account("current@x.com", mock_db)
        assert result is b
    finally:
        await cache.invalidate("exhausted@x.com")
        await cache.invalidate("fresh@x.com")


@pytest.mark.asyncio
async def test_get_next_account_skips_candidate_over_threshold():
    """A candidate whose cached utilization is >= its own threshold_pct must
    be skipped (switching to it would immediately bounce back).
    """
    from backend.services.switcher import get_next_account
    from backend.cache import cache

    a = _make_account_for_next(1, "over@x.com", 0, threshold_pct=80.0)
    b = _make_account_for_next(2, "under@x.com", 1, threshold_pct=80.0)

    await cache.set_usage("over@x.com", {
        "five_hour": {"utilization": 90.0, "resets_at": "2099-01-01T00:00:00Z"},
    })
    await cache.set_usage("under@x.com", {
        "five_hour": {"utilization": 20.0, "resets_at": "2099-01-01T00:00:00Z"},
    })

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [a, b]
    mock_db = AsyncMock()
    mock_db.execute.return_value = mock_result

    try:
        result = await get_next_account("current@x.com", mock_db)
        assert result is b
    finally:
        await cache.invalidate("over@x.com")
        await cache.invalidate("under@x.com")


@pytest.mark.asyncio
async def test_get_next_account_returns_none_when_all_exhausted():
    """If every candidate is either rate-limited or over threshold, return None
    so the caller can surface an error to the user."""
    from backend.services.switcher import get_next_account
    from backend.cache import cache

    a = _make_account_for_next(1, "a-ex@x.com", 0, threshold_pct=80.0)
    b = _make_account_for_next(2, "b-ex@x.com", 1, threshold_pct=80.0)

    await cache.set_usage("a-ex@x.com", {
        "rate_limited": True,
        "five_hour": {"utilization": 0, "resets_at": "2099-01-01T00:00:00Z"},
    })
    await cache.set_usage("b-ex@x.com", {
        "five_hour": {"utilization": 95.0, "resets_at": "2099-01-01T00:00:00Z"},
    })

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [a, b]
    mock_db = AsyncMock()
    mock_db.execute.return_value = mock_result

    try:
        result = await get_next_account("current@x.com", mock_db)
        assert result is None
    finally:
        await cache.invalidate("a-ex@x.com")
        await cache.invalidate("b-ex@x.com")


@pytest.mark.asyncio
async def test_get_next_account_includes_unprobed_candidate():
    """A candidate with no cached usage data yet must be kept in the pool
    (benefit of the doubt for newly-added accounts)."""
    from backend.services.switcher import get_next_account
    from backend.cache import cache

    a = _make_account_for_next(1, "new@x.com", 0, threshold_pct=80.0)
    await cache.invalidate("new@x.com")

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [a]
    mock_db = AsyncMock()
    mock_db.execute.return_value = mock_result

    result = await get_next_account("current@x.com", mock_db)
    assert result is a


@pytest.mark.asyncio
async def test_perform_switch_activates_with_enabled_targets_and_broadcasts():
    """perform_switch should fetch enabled credential targets from the DB
    and pass them to activate_account_config alongside the target dir."""
    from backend.services.switcher import perform_switch

    target = make_account(2, "new@x.com", 1, config_dir="/tmp/fake-account-2")

    mock_db = MagicMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = None
    mock_db.execute = AsyncMock(return_value=mock_result)
    mock_db.commit = AsyncMock()

    mock_ws = AsyncMock()

    fake_enabled = ["/Users/me/.claude.json", "/Users/me/.claude-accounts/foo/.claude.json"]

    with patch("backend.services.account_service.get_active_email", return_value="old@x.com"), \
         patch("backend.services.credential_targets.enabled_canonical_paths",
               AsyncMock(return_value=fake_enabled)), \
         patch("backend.services.account_service.activate_account_config",
               return_value={
                   "mirror": {"written": fake_enabled, "skipped": [], "errors": []},
                   "keychain_written": True,
                   "system_default_enabled": True,
               }) as mock_activate:
        await perform_switch(target, "threshold", mock_db, mock_ws)

    mock_activate.assert_called_once_with("/tmp/fake-account-2", fake_enabled)
    mock_ws.broadcast.assert_called_once()
    broadcast_data = mock_ws.broadcast.call_args[0][0]
    assert broadcast_data["type"] == "account_switched"
    assert broadcast_data["to"] == "new@x.com"
    assert broadcast_data["reason"] == "threshold"
    assert broadcast_data["mirror"]["written"] == fake_enabled


@pytest.mark.asyncio
async def test_perform_switch_serialized_by_lock():
    """Two concurrent perform_switch() calls must not overlap.

    The _switch_lock inside switcher.py ensures the second coroutine waits
    for the first to finish.  We verify this by tracking the start/end times
    of activate_account_config calls; the second call must start after the
    first finishes.
    """
    import time
    from backend.services import switcher as sw

    sw._switch_lock = asyncio.Lock()

    call_log: list[tuple[str, float]] = []

    def slow_activate(config_dir, enabled_targets=None):
        call_log.append(("start", time.monotonic()))
        time.sleep(0.05)
        call_log.append(("end", time.monotonic()))
        return {
            "mirror": {"written": [], "skipped": [], "errors": []},
            "keychain_written": False,
            "system_default_enabled": False,
        }

    target_a = make_account(1, "a@x.com", 0, config_dir="/tmp/fake-a")
    target_b = make_account(2, "b@x.com", 1, config_dir="/tmp/fake-b")

    def make_mock_db():
        mock_db = MagicMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = None
        mock_db.execute = AsyncMock(return_value=mock_result)
        mock_db.commit = AsyncMock()
        return mock_db

    mock_ws = AsyncMock()

    with patch("backend.services.account_service.get_active_email", return_value="old@x.com"), \
         patch("backend.services.credential_targets.enabled_canonical_paths",
               AsyncMock(return_value=[])), \
         patch("backend.services.account_service.activate_account_config", side_effect=slow_activate):
        await asyncio.gather(
            sw.perform_switch(target_a, "threshold", make_mock_db(), mock_ws),
            sw.perform_switch(target_b, "threshold", make_mock_db(), mock_ws),
        )

    starts = [t for ev, t in call_log if ev == "start"]
    ends = [t for ev, t in call_log if ev == "end"]
    assert len(starts) == 2, "activate_account_config must be called exactly twice"
    assert len(ends) == 2

    first_end = min(ends)
    second_start = max(starts)
    assert second_start >= first_end - 1e-6, (
        "Second perform_switch started before first finished — lock not working"
    )
