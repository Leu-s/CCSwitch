"""
Anthropic API helpers.

probe_usage()  — POST a minimal Haiku message and read rate-limit
                 utilization from the response headers.  This is the
                 correct approach: /api/oauth/usage is rate-limited to
                 ~5 requests per access token and must not be used for
                 periodic polling.
"""

import enum
from typing import Any

import httpx

from ..config import settings

# OAuth2 terminal error codes per RFC 6749 §5.2.  Each one names a condition
# that is NOT self-healing within hours — the refresh_token is dead, the
# client is mis-registered, or the scope is wrong — so the correct response
# is to demand a re-login.  Everything else (400 `invalid_request`, 400 with
# no body, 429, 5xx, network) is transient and eligible for exponential
# retry.
#
# `invalid_grant`          — refresh_token expired, revoked, or reused.
# `invalid_client`         — client authentication failed (we are mis-
#                            registered with the authz server).
# `unauthorized_client`    — client not allowed to use this grant type.
# `unsupported_grant_type` — authz server doesn't understand this grant.
# `invalid_scope`          — scope requested is invalid / out of range.
_TERMINAL_OAUTH_ERROR_CODES = frozenset({
    "invalid_grant",
    "invalid_client",
    "unauthorized_client",
    "unsupported_grant_type",
    "invalid_scope",
})


class OAuthErrorKind(enum.Enum):
    """Classification of a failed refresh request.

    ``TERMINAL_REVOKED``   — refresh token explicitly rejected by the authz
                             server; user must re-login.
    ``TERMINAL_REJECTED``  — client or request config problem the server
                             considers unrecoverable; user must re-login.
    ``TRANSIENT``          — every other failure (edge-proxy WAF challenges,
                             500-series, 429, network, 400 with non-terminal
                             error code, no parseable body).  Retry with
                             exponential backoff.
    """

    TERMINAL_REVOKED = "terminal_revoked"
    TERMINAL_REJECTED = "terminal_rejected"
    TRANSIENT = "transient"


def _extract_oauth_error_code(resp: httpx.Response) -> str | None:
    """Return the OAuth ``error`` field from a response body, or None if it
    is not a parseable OAuth2 error response."""
    try:
        body: Any = resp.json()
    except Exception:
        # httpx versions differ: older raise ValueError, newer raise
        # json.JSONDecodeError (which is a ValueError subclass but also
        # surfaces as-is).  Either way — no parseable body.
        return None
    if not isinstance(body, dict):
        return None
    code = body.get("error")
    return code if isinstance(code, str) else None


def parse_oauth_error(err: httpx.HTTPStatusError) -> OAuthErrorKind:
    """Classify a refresh-endpoint HTTP error into terminal/transient.

    Rule:
    * 401 or 400 whose body carries an ``error`` code in
      ``_TERMINAL_OAUTH_ERROR_CODES`` → TERMINAL_REVOKED (401) or
      TERMINAL_REJECTED (400).  These are RFC 6749 §5.2 terminal conditions.
    * Everything else → TRANSIENT.  Includes bare 401 without a body
      (frequently a Cloudflare / edge-proxy challenge that self-heals),
      bare 400, 400 with non-terminal code, 429, 5xx, malformed body.

    This is deliberately conservative: false-positive transient is a 2-minute
    backoff and a retry; false-positive terminal is a phantom-stale account
    the user cannot clear without the full re-login tmux dance.  The
    motivating production bug was the latter.
    """
    status = err.response.status_code
    code = _extract_oauth_error_code(err.response) if status in (400, 401) else None
    if code not in _TERMINAL_OAUTH_ERROR_CODES:
        return OAuthErrorKind.TRANSIENT
    return (
        OAuthErrorKind.TERMINAL_REVOKED
        if status == 401
        else OAuthErrorKind.TERMINAL_REJECTED
    )


MESSAGES_URL = settings.anthropic_messages_url
REFRESH_URL = settings.anthropic_refresh_url

# Headers that activate the unified rate-limit response headers
_HEADERS = {
    "User-Agent": "claude-code/2.1.104",
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "oauth-2025-04-20",
    "Content-Type": "application/json",
}

# Cheapest possible probe — haiku, 1 output token, single-character input
_PROBE_BODY = {
    "model": settings.haiku_model,
    "max_tokens": 1,
    "messages": [{"role": "user", "content": "."}],
}


async def probe_usage(access_token: str) -> dict:
    """
    POST a minimal message to /v1/messages and extract rate-limit utilization
    from the response headers.

    Returns the same nested structure consumed by the rest of the codebase:
        {
          "five_hour": {"utilization": 0-100, "resets_at": <unix epoch s>},
          "seven_day":  {"utilization": 0-100, "resets_at": <unix epoch s>},
        }

    Raises httpx.HTTPStatusError on 4xx/5xx so the caller can handle it.
    """
    headers = {**_HEADERS, "Authorization": f"Bearer {access_token}"}

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(MESSAGES_URL, headers=headers, json=_PROBE_BODY)
        resp.raise_for_status()

    h = resp.headers

    def _f(key: str) -> float | None:
        v = h.get(key)
        try:
            return float(v) if v is not None else None
        except (ValueError, TypeError):
            return None

    def _i(key: str) -> int | None:
        v = h.get(key)
        try:
            return int(v) if v is not None else None
        except (ValueError, TypeError):
            return None

    result: dict = {}

    five_util = _f("anthropic-ratelimit-unified-5h-utilization")
    if five_util is not None:
        result["five_hour"] = {
            "utilization": round(five_util * 100, 2),
            "resets_at": _i("anthropic-ratelimit-unified-5h-reset"),
            "status": h.get("anthropic-ratelimit-unified-5h-status"),
        }

    seven_util = _f("anthropic-ratelimit-unified-7d-utilization")
    if seven_util is not None:
        result["seven_day"] = {
            "utilization": round(seven_util * 100, 2),
            "resets_at": _i("anthropic-ratelimit-unified-7d-reset"),
        }

    return result


async def refresh_access_token(refresh_token: str) -> dict:
    """POST to Anthropic's OAuth refresh endpoint and return the parsed body.

    Raises:
        httpx.HTTPStatusError — upstream 4xx/5xx.
        httpx.RequestError — network / timeout / connect.
        RuntimeError — upstream 200 but body was not valid JSON.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            REFRESH_URL,
            json={"grant_type": "refresh_token", "refresh_token": refresh_token},
        )
        resp.raise_for_status()
        try:
            return resp.json()
        except ValueError as e:
            raise RuntimeError(
                f"Refresh response body was not valid JSON: {e}"
            ) from e
