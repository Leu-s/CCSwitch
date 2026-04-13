import pytest
import httpx
from unittest.mock import patch, AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_probe_usage_success():
    """probe_usage extracts rate-limit utilization from response headers."""
    from backend.services.anthropic_api import probe_usage

    mock_response = MagicMock()
    mock_response.headers = {
        "anthropic-ratelimit-unified-5h-utilization": "0.42",
        "anthropic-ratelimit-unified-5h-reset": "1742651200",
        "anthropic-ratelimit-unified-5h-status": "normal",
        "anthropic-ratelimit-unified-7d-utilization": "0.18",
        "anthropic-ratelimit-unified-7d-reset": "1743120000",
    }
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response):
        result = await probe_usage("sk-ant-test")

    assert "five_hour" in result
    assert round(result["five_hour"]["utilization"], 2) == 42.0
    assert result["five_hour"]["resets_at"] == 1742651200
    assert result["five_hour"]["status"] == "normal"
    assert "seven_day" in result
    assert round(result["seven_day"]["utilization"], 2) == 18.0
    assert result["seven_day"]["resets_at"] == 1743120000


@pytest.mark.asyncio
async def test_probe_usage_missing_headers_returns_empty():
    """If rate-limit headers are absent, probe_usage returns empty dict."""
    from backend.services.anthropic_api import probe_usage

    mock_response = MagicMock()
    mock_response.headers = {}  # No rate-limit headers
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response):
        result = await probe_usage("sk-ant-test")

    assert result == {}


@pytest.mark.asyncio
async def test_probe_usage_401_raises():
    """probe_usage raises httpx.HTTPStatusError on 401."""
    from backend.services.anthropic_api import probe_usage

    mock_response = MagicMock()
    mock_response.status_code = 401
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "401", request=MagicMock(), response=mock_response
    )

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response):
        with pytest.raises(httpx.HTTPStatusError):
            await probe_usage("bad-token")


@pytest.mark.asyncio
async def test_refresh_token_success():
    """refresh_access_token returns new token data."""
    from backend.services.anthropic_api import refresh_access_token

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "access_token": "sk-ant-new",
        "refresh_token": "rt-new",
        "expires_in": 3600,
    }
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response):
        result = await refresh_access_token("rt-test")

    assert result["access_token"] == "sk-ant-new"
    assert result["refresh_token"] == "rt-new"
