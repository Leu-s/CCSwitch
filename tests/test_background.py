"""
Tests for backend.background.

background.py now:
- reads per-account threshold from account.threshold_pct instead of a global Setting
- calls account_service.get_access_token_from_config_dir(account.config_dir)
- calls account_service.get_active_email() (no args)
- gates all work behind service_enabled setting (not auto_switch_enabled)
- _notify_tmux_monitors is a module-private helper; tests reference it via the
  public alias notify_tmux_monitors only for the tmux monitor test.
"""
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
    from backend.background import _notify_tmux_monitors

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
            await _notify_tmux_monitors([monitor], mock_ws, "claude-haiku-4-5-20251001")

    mock_send.assert_called_once_with("main:0.0")
    mock_ws.broadcast.assert_called_once()
    data = mock_ws.broadcast.call_args[0][0]
    assert data["type"] == "tmux_result"
    assert data["status"] == "SUCCESS"
