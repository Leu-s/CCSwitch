"""
Tests for backend.services.switcher.

The Account model no longer has keychain_suffix / account_uuid / org_uuid.
perform_switch now calls account_service.activate_account_config() instead
of keychain read/write helpers.
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


@pytest.mark.asyncio
async def test_get_next_account_skips_current():
    from backend.services.switcher import get_next_account
    accounts = [make_account(1, "a@x.com", 0), make_account(2, "b@x.com", 1)]
    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = accounts[1]
    mock_db = AsyncMock()
    mock_db.execute.return_value = mock_result
    result = await get_next_account("a@x.com", mock_db)
    assert result.email == "b@x.com"


@pytest.mark.asyncio
async def test_get_next_account_returns_none_when_no_others():
    from backend.services.switcher import get_next_account
    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = None
    mock_db = AsyncMock()
    mock_db.execute.return_value = mock_result
    result = await get_next_account("only@x.com", mock_db)
    assert result is None


@pytest.mark.asyncio
async def test_get_next_account_skips_stale():
    """get_next_account must not return an account that has stale_reason set."""
    from backend.services.switcher import get_next_account
    # The DB query with the stale_reason == None filter returns nothing
    # because the only other account is stale.
    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = None
    mock_db = AsyncMock()
    mock_db.execute.return_value = mock_result
    result = await get_next_account("current@x.com", mock_db)
    assert result is None


@pytest.mark.asyncio
async def test_perform_switch_calls_activate_and_broadcasts():
    """perform_switch should activate the target's config dir and broadcast."""
    from backend.services.switcher import perform_switch

    target = make_account(2, "new@x.com", 1, config_dir="/tmp/fake-account-2")

    mock_db = MagicMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = None
    mock_db.execute = AsyncMock(return_value=mock_result)
    mock_db.commit = AsyncMock()

    mock_ws = AsyncMock()

    with patch("backend.services.account_service.get_active_email", return_value="old@x.com"), \
         patch("backend.services.account_service.activate_account_config") as mock_activate:
        await perform_switch(target, "threshold", mock_db, mock_ws)

    mock_activate.assert_called_once_with("/tmp/fake-account-2")
    mock_ws.broadcast.assert_called_once()
    broadcast_data = mock_ws.broadcast.call_args[0][0]
    assert broadcast_data["type"] == "account_switched"
    assert broadcast_data["to"] == "new@x.com"
    assert broadcast_data["reason"] == "threshold"


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

    # Reset the module-level lock so earlier tests don't affect state.
    sw._switch_lock = asyncio.Lock()

    call_log: list[tuple[str, float]] = []  # (event, timestamp)

    # activate_account_config is called via asyncio.to_thread — it must be a
    # synchronous function.  Use time.sleep() to block the thread and give the
    # event loop a chance to schedule the second coroutine.
    def slow_activate(config_dir: str) -> None:
        call_log.append(("start", time.monotonic()))
        time.sleep(0.05)
        call_log.append(("end", time.monotonic()))

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
         patch("backend.services.account_service.activate_account_config", side_effect=slow_activate):
        await asyncio.gather(
            sw.perform_switch(target_a, "threshold", make_mock_db(), mock_ws),
            sw.perform_switch(target_b, "threshold", make_mock_db(), mock_ws),
        )

    # Both calls completed
    starts = [t for ev, t in call_log if ev == "start"]
    ends = [t for ev, t in call_log if ev == "end"]
    assert len(starts) == 2, "activate_account_config must be called exactly twice"
    assert len(ends) == 2

    # The second activation must not have started before the first finished.
    # Sort by time to be order-agnostic about which ran first.
    first_end = min(ends)
    second_start = max(starts)
    assert second_start >= first_end - 1e-6, (
        "Second perform_switch started before first finished — lock not working"
    )
