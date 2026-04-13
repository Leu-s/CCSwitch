"""
Anthropic API helpers.

probe_usage()  — POST a minimal Haiku message and read rate-limit
                 utilization from the response headers.  This is the
                 correct approach: /api/oauth/usage is rate-limited to
                 ~5 requests per access token and must not be used for
                 periodic polling.
"""

import httpx

from ..config import settings

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
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            REFRESH_URL,
            json={"grant_type": "refresh_token", "refresh_token": refresh_token},
        )
        resp.raise_for_status()
        return resp.json()
