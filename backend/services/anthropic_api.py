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

# Codes that indicate the refresh_token / access_token is dead in a way
# that will NOT self-heal — only a re-login can fix it.  Two flavours:
#
#   RFC 6749 §5.2 (standard OAuth2 flat shape)
#     invalid_grant           — token expired, revoked, or reused
#     invalid_client          — client authentication failed
#     unauthorized_client     — client not allowed to use this grant
#     unsupported_grant_type  — server doesn't understand the grant
#     invalid_scope           — scope invalid / out of range
#
#   Anthropic-specific envelope (empirically verified on their
#   /v1/oauth/token and /v1/messages endpoints):
#     invalid_request_error   — Anthropic's actual signal for dead
#                               refresh_token on /oauth/token (despite
#                               the misleading "request format" message)
#     authentication_error    — access-token invalid on /v1/messages.
#                               The poll loop's probe-path handles 401
#                               with a dedicated branch in background.py
#                               that sets a fixed stale_reason string
#                               WITHOUT calling parse_oauth_error, so
#                               this set-entry is belt-and-braces for any
#                               future caller that DOES route probe
#                               errors through parse_oauth_error (e.g.
#                               a revalidate-probe-verify path).
_TERMINAL_OAUTH_ERROR_CODES = frozenset({
    "invalid_grant",
    "invalid_client",
    "unauthorized_client",
    "unsupported_grant_type",
    "invalid_scope",
    "invalid_request_error",
    "authentication_error",
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
    """Return the error code from a failed OAuth/API response, supporting
    both RFC 6749 §5.2 flat shape and Anthropic's nested envelope.

    * RFC 6749:   ``{"error": "invalid_grant", "error_description": "..."}``
    * Anthropic:  ``{"type": "error", "error": {"type": "...", "message": "..."}}``

    Returns ``None`` if the response body is not parseable JSON, not a dict,
    or does not carry an ``error`` field in either recognised shape.
    """
    try:
        body: Any = resp.json()
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    raw = body.get("error")
    if isinstance(raw, str):
        # RFC 6749 flat shape.
        return raw
    if isinstance(raw, dict):
        # Anthropic nested envelope — read error.type.
        inner = raw.get("type")
        return inner if isinstance(inner, str) else None
    return None


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
