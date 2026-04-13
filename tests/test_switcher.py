import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

def make_account(id, email, priority, enabled=True):
    a = MagicMock()
    a.id = id
    a.email = email
    a.priority = priority
    a.enabled = enabled
    a.keychain_suffix = f"suffix{id}"
    a.account_uuid = f"uuid-{id}"
    a.org_uuid = f"org-{id}"
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
async def test_perform_switch_calls_keychain_and_broadcasts():
    import json
    from backend.services.switcher import perform_switch

    target = make_account(2, "new@x.com", 1)
    creds = {"claudeAiOauth": {"accessToken": "tok", "refreshToken": "rt", "expiresAt": 9999}}

    mock_db = AsyncMock()
    # First execute() call is in perform_switch to find from_acc
    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = None
    mock_db.execute.return_value = mock_result

    mock_ws = AsyncMock()

    with patch("backend.services.keychain.read_credentials", return_value=creds), \
         patch("backend.services.keychain.write_active_credentials") as mock_write, \
         patch("backend.services.keychain.update_oauth_account") as mock_update, \
         patch("backend.services.keychain.get_active_email", return_value="old@x.com"), \
         patch("backend.services.switcher.settings") as mock_settings:
        mock_settings.claude_config_dir = "/tmp/fake"
        await perform_switch(target, "threshold", mock_db, mock_ws)

    mock_write.assert_called_once_with(creds)
    mock_update.assert_called_once()
    mock_ws.broadcast.assert_called_once()
    broadcast_data = mock_ws.broadcast.call_args[0][0]
    assert broadcast_data["type"] == "account_switched"
    assert broadcast_data["to"] == "new@x.com"
    assert broadcast_data["reason"] == "threshold"
