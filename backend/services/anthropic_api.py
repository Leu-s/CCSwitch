"""
Anthropic API helpers.

probe_usage()  — POST a minimal Haiku message and read rate-limit
                 utilization from the response headers.  This is the
                 correct approach: /api/oauth/usage is rate-limited to
                 ~5 requests per access token and must not be used for
                 periodic polling.
"""

from typing import Any

import httpx

from ..config import settings

# Codes that indicate the refresh_token / access_token is dead in a way
# that will NOT self-heal — only a re-login can fix it.  Two flavours:
#
#   RFC 6749 §5.2 (standard OAuth2 flat shape) — Anthropic's actual
#   signal for genuinely dead refresh_tokens on /v1/oauth/token is
#   ``invalid_grant`` under this flat shape:
#     invalid_grant           — token expired, revoked, or reused
#     invalid_client          — client authentication failed
#     unauthorized_client     — client not allowed to use this grant
#     unsupported_grant_type  — server doesn't understand the grant
#     invalid_scope           — scope invalid / out of range
#
#   Anthropic-specific nested envelope:
#     authentication_error    — access-token invalid on /v1/messages.
#                               The poll loop's probe-path handles 401
#                               with a dedicated branch in background.py
#                               that sets a fixed stale_reason string
#                               WITHOUT calling parse_oauth_error, so
#                               this set-entry is belt-and-braces for any
#                               future caller that DOES route probe
#                               errors through parse_oauth_error (e.g.
#                               a revalidate-probe-verify path).
#
# NOTE ABSENCES (April 2026 regression lesson):
#   ``invalid_request_error`` is NOT terminal.  Anthropic emits it when
#   OUR POST body is malformed (e.g. missing ``client_id`` on
#   /v1/oauth/token).  Classifying it terminal caused a phantom-stale
#   cascade across three healthy user accounts — every swap-time refresh
#   persisted stale_reason from a classifier verdict that was actually
#   telling us to fix our request, not that the token was dead.
#   Genuinely dead refresh_tokens return RFC-flat ``invalid_grant``.
#   See docs/superpowers/plans/2026-04-16-oauth-refresh-client-id-fix.md.
_TERMINAL_OAUTH_ERROR_CODES = frozenset({
    "invalid_grant",
    "invalid_client",
    "unauthorized_client",
    "unsupported_grant_type",
    "invalid_scope",
    "authentication_error",
})



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


def is_terminal_oauth_error(err: httpx.HTTPStatusError) -> bool:
    """Return True if a refresh-endpoint error is terminal (re-login required).

    Terminal: 401 or 400 whose body carries an error code in
    _TERMINAL_OAUTH_ERROR_CODES (RFC 6749 §5.2 + Anthropic authentication_error).

    Everything else is transient (retry with backoff): bare 401/400 without
    terminal body, 429, 5xx, malformed body.  Deliberately conservative —
    false-positive transient = 2-minute backoff; false-positive terminal =
    phantom-stale (April 2026 regression).
    """
    status = err.response.status_code
    code = _extract_oauth_error_code(err.response) if status in (400, 401) else None
    return code in _TERMINAL_OAUTH_ERROR_CODES


MESSAGES_URL = settings.anthropic_messages_url
REFRESH_URL = settings.anthropic_refresh_url

# Canonical Claude Code OAuth client_id.  Public identifier (not a secret),
# used by every open-source Claude-multi-account tool (ccflare, ccNexus,
# Kaku, Hermes, wanikua, CLIProxyAPI, oh-my-claudecode, stencila, …).
#
# Anthropic's /v1/oauth/token endpoint rejects POSTs that omit this
# field with HTTP 400 ``invalid_request_error`` — even for healthy
# refresh_tokens.  The April 2026 production bug (phantom-stale
# cascade on three healthy accounts) was caused by its omission.
# See docs/superpowers/plans/2026-04-16-oauth-refresh-client-id-fix.md.
_CLAUDE_CODE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"

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


def parse_rate_limit_headers(headers) -> dict:
    """Extract unified rate-limit fields from Anthropic response headers.

    Returns ``{"five_hour": {...}, "seven_day": {...}}`` with the usual
    ``utilization`` / ``resets_at`` / ``status`` keys.  Each window is
    only included if AT LEAST ONE of its three headers was present — so
    a 429 that ships only ``*-reset`` still produces usable data for the
    UI ("resets in 2d 14h") even without a utilization value.

    Safe to call with httpx.Response.headers from either a 200 probe or
    a 429 rate-limited response — Anthropic ships the unified headers
    on both.
    """
    def _f(k):
        v = headers.get(k)
        try:
            return float(v) if v is not None else None
        except (ValueError, TypeError):
            return None

    def _i(k):
        v = headers.get(k)
        try:
            return int(v) if v is not None else None
        except (ValueError, TypeError):
            return None

    result: dict = {}

    five_util = _f("anthropic-ratelimit-unified-5h-utilization")
    five_reset = _i("anthropic-ratelimit-unified-5h-reset")
    five_status = headers.get("anthropic-ratelimit-unified-5h-status")
    if five_util is not None or five_reset is not None or five_status is not None:
        result["five_hour"] = {
            "utilization": round(five_util * 100, 2) if five_util is not None else None,
            "resets_at": five_reset,
            "status": five_status,
        }

    seven_util = _f("anthropic-ratelimit-unified-7d-utilization")
    seven_reset = _i("anthropic-ratelimit-unified-7d-reset")
    seven_status = headers.get("anthropic-ratelimit-unified-7d-status")
    if seven_util is not None or seven_reset is not None or seven_status is not None:
        result["seven_day"] = {
            "utilization": round(seven_util * 100, 2) if seven_util is not None else None,
            "resets_at": seven_reset,
            "status": seven_status,
        }

    return result


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

    return parse_rate_limit_headers(resp.headers)


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
            json={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": _CLAUDE_CODE_CLIENT_ID,
            },
        )
        resp.raise_for_status()
        try:
            return resp.json()
        except ValueError as e:
            raise RuntimeError(
                f"Refresh response body was not valid JSON: {e}"
            ) from e
