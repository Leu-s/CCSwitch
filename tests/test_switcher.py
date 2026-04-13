"""
Tests for backend.services.switcher.

The Account model no longer has keychain_suffix / account_uuid / org_uuid.
perform_switch now calls account_service.activate_account_config() instead
of keychain read/write helpers.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def make_account(id, email, priority, enabled=True, config_dir=None):
    a = MagicMock()
    a.id = id
    a.email = email
    a.priority = priority
    a.enabled = enabled
    a.config_dir = config_dir or f"/tmp/fake-account-{id}"
    a.display_name = None
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
