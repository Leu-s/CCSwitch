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


# ── OAuth error parser ────────────────────────────────────────────────

from backend.services.anthropic_api import parse_oauth_error, OAuthErrorKind


def _make_http_status_error(status: int, json_body=None, text_body=""):
    """Build an httpx.HTTPStatusError with a realistic response object."""
    import httpx
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    if json_body is not None:
        resp.json = MagicMock(return_value=json_body)
    else:
        resp.json = MagicMock(side_effect=ValueError("no json"))
    resp.text = text_body
    req = httpx.Request("POST", "https://example.test/oauth2/token")
    return httpx.HTTPStatusError("status", request=req, response=resp)


def test_parse_oauth_error_401_with_invalid_grant_is_terminal():
    err = _make_http_status_error(401, {"error": "invalid_grant"})
    assert parse_oauth_error(err) == OAuthErrorKind.TERMINAL_REVOKED


def test_parse_oauth_error_401_with_invalid_client_is_terminal():
    err = _make_http_status_error(401, {"error": "invalid_client"})
    assert parse_oauth_error(err) == OAuthErrorKind.TERMINAL_REVOKED


def test_parse_oauth_error_bare_401_without_body_is_transient():
    """Bare 401 can be an edge-proxy WAF challenge (Cloudflare etc.) — retry."""
    err = _make_http_status_error(401, None, "Unauthorized")
    assert parse_oauth_error(err) == OAuthErrorKind.TRANSIENT


def test_parse_oauth_error_401_with_unknown_body_is_transient():
    err = _make_http_status_error(401, {"error": "some_edge_proxy_code"})
    assert parse_oauth_error(err) == OAuthErrorKind.TRANSIENT


def test_parse_oauth_error_400_invalid_grant_is_terminal_rejected():
    err = _make_http_status_error(400, {"error": "invalid_grant"})
    assert parse_oauth_error(err) == OAuthErrorKind.TERMINAL_REJECTED


def test_parse_oauth_error_400_invalid_client_is_terminal_rejected():
    err = _make_http_status_error(400, {"error": "invalid_client"})
    assert parse_oauth_error(err) == OAuthErrorKind.TERMINAL_REJECTED


def test_parse_oauth_error_400_unauthorized_client_is_terminal():
    """Per RFC 6749 §5.2, client is not authorised for this grant type —
    not a self-healing condition, so treat as terminal."""
    err = _make_http_status_error(400, {"error": "unauthorized_client"})
    assert parse_oauth_error(err) == OAuthErrorKind.TERMINAL_REJECTED


def test_parse_oauth_error_400_unsupported_grant_type_is_terminal():
    err = _make_http_status_error(400, {"error": "unsupported_grant_type"})
    assert parse_oauth_error(err) == OAuthErrorKind.TERMINAL_REJECTED


def test_parse_oauth_error_400_invalid_scope_is_terminal():
    err = _make_http_status_error(400, {"error": "invalid_scope"})
    assert parse_oauth_error(err) == OAuthErrorKind.TERMINAL_REJECTED


def test_parse_oauth_error_400_invalid_request_is_transient():
    err = _make_http_status_error(400, {"error": "invalid_request"})
    assert parse_oauth_error(err) == OAuthErrorKind.TRANSIENT


def test_parse_oauth_error_400_unknown_code_is_transient():
    err = _make_http_status_error(400, {"error": "rate_limited_on_refresh"})
    assert parse_oauth_error(err) == OAuthErrorKind.TRANSIENT


def test_parse_oauth_error_400_without_body_is_transient():
    err = _make_http_status_error(400, None, "Bad Request")
    assert parse_oauth_error(err) == OAuthErrorKind.TRANSIENT


def test_parse_oauth_error_400_with_non_dict_body_is_transient():
    err = _make_http_status_error(400, ["not a dict"])
    assert parse_oauth_error(err) == OAuthErrorKind.TRANSIENT


def test_parse_oauth_error_400_with_json_decode_error_is_transient():
    """Newer httpx versions raise json.JSONDecodeError not ValueError — catch both."""
    import json
    err = _make_http_status_error(400, None, "not json either")
    # Replace the side_effect with JSONDecodeError specifically.
    err.response.json = MagicMock(
        side_effect=json.JSONDecodeError("bad", "body", 0)
    )
    assert parse_oauth_error(err) == OAuthErrorKind.TRANSIENT


def test_parse_oauth_error_429_is_transient():
    err = _make_http_status_error(429, {"error": "rate_limited"})
    assert parse_oauth_error(err) == OAuthErrorKind.TRANSIENT


def test_parse_oauth_error_5xx_is_transient():
    err = _make_http_status_error(503, None, "Service Unavailable")
    assert parse_oauth_error(err) == OAuthErrorKind.TRANSIENT


def test_parse_oauth_error_error_field_null_is_transient():
    """Body has explicit `error: null` — still transient (no terminal code)."""
    err = _make_http_status_error(400, {"error": None})
    assert parse_oauth_error(err) == OAuthErrorKind.TRANSIENT


def test_parse_oauth_error_error_field_non_string_is_transient():
    """Body has `error` as a non-string type — isinstance guard forces transient."""
    err = _make_http_status_error(400, {"error": 400})
    assert parse_oauth_error(err) == OAuthErrorKind.TRANSIENT


def test_parse_oauth_error_403_is_transient():
    """Status 403 must short-circuit to transient before body lookup fires."""
    err = _make_http_status_error(403, {"error": "invalid_grant"})
    assert parse_oauth_error(err) == OAuthErrorKind.TRANSIENT


def test_parse_oauth_error_400_with_error_description_preserves_terminal():
    """A terminal `error` code stays terminal even when `error_description` is
    also present — we read ONLY the `error` field, not the description."""
    err = _make_http_status_error(
        400,
        {"error": "invalid_grant", "error_description": "Token was revoked by user"},
    )
    assert parse_oauth_error(err) == OAuthErrorKind.TERMINAL_REJECTED
