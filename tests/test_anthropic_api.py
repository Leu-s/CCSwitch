import pytest
import httpx
from unittest.mock import patch, AsyncMock, MagicMock

USAGE_RESPONSE = {
    "five_hour": {"used_percentage": 42.0, "resets_at": 1742651200},
    "seven_day": {"used_percentage": 18.0, "resets_at": 1743120000}
}
REFRESH_RESPONSE = {
    "access_token": "sk-ant-new",
    "refresh_token": "rt-new",
    "expires_in": 3600
}

@pytest.mark.asyncio
async def test_fetch_usage_success():
    from backend.services.anthropic_api import fetch_usage
    mock_response = MagicMock()
    mock_response.json.return_value = USAGE_RESPONSE
    mock_response.raise_for_status = MagicMock()
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response):
        result = await fetch_usage("sk-ant-test")
    assert result["five_hour"]["used_percentage"] == 42.0

@pytest.mark.asyncio
async def test_fetch_usage_401_raises():
    from backend.services.anthropic_api import fetch_usage
    mock_response = MagicMock()
    mock_response.status_code = 401
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "401", request=MagicMock(), response=mock_response
    )
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response):
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_usage("bad-token")

@pytest.mark.asyncio
async def test_refresh_token_success():
    from backend.services.anthropic_api import refresh_access_token
    mock_response = MagicMock()
    mock_response.json.return_value = REFRESH_RESPONSE
    mock_response.raise_for_status = MagicMock()
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response):
        result = await refresh_access_token("rt-test")
    assert result["access_token"] == "sk-ant-new"
