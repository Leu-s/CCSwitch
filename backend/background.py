import asyncio
import logging
import random
import time

import httpx

from sqlalchemy import select

from .cache import cache
from .config import settings
from .database import AsyncSessionLocal
from .models import Account
from .services import account_service as ac
from .services import anthropic_api
from .services import credential_provider as cp
from .services import switcher as sw
from .services import tmux_service
from .services.account_service import build_usage
from .ws import WebSocketManager


logger = logging.getLogger(__name__)


# ── Per-account 429 backoff state ────────────────────────────────────────────
# Maps email → monotonic deadline (seconds); if time.monotonic() < deadline,
# skip the probe and return stale cached data instead.
_backoff_until: dict[str, float] = {}
# Maps email → consecutive 429 count for exponential doubling.
_backoff_count: dict[str, int] = {}

_BACKOFF_INITIAL = settings.rate_limit_backoff_initial
_BACKOFF_MAX = settings.rate_limit_backoff_max


# Refresh window for vault accounts.  CCSwitch is the sole consumer of vault
# entries, so widening the pre-expiry window past the CLI's own 5-minute
# default is free — no race partner.  The active account is never refreshed
# by CCSwitch, so this constant does not apply to it.
_REFRESH_SKEW_MS = 20 * 60 * 1000


# ── Active-probe nudge rate limit ────────────────────────────────────────────
# Maps email → monotonic deadline before the next nudge is allowed.  Prevents
# a chatty 401 loop from firing tmux-send-keys every 15 seconds.
_NUDGE_COOLDOWN_SECONDS = 30
_last_nudge_at: dict[str, float] = {}


# ── Post-sleep stagger ───────────────────────────────────────────────────────
# When the event loop wall-clock jumps by more than this much between
# iterations, treat it as a sleep/wake event and add a random 0..30s stagger
# before firing N concurrent /oauth/token refreshes.  Prevents an N-account
# thundering herd on /oauth/token which could trip Anthropic rate limits.
_SLEEP_DETECTION_THRESHOLD_SECONDS = 300.0
_last_poll_monotonic: float | None = None


class _RefreshTerminal(Exception):
    """Raised when a refresh attempt returned a terminal status (400/401),
    meaning the stored refresh_token is dead.  Skips the subsequent probe."""


def _maybe_nudge_active(email: str) -> None:
    """Fire a single tmux nudge for an active-account probe 401, subject to
    a per-account cooldown.  Runs the blocking fire_nudge call via an
    asyncio thread so it does not block the event loop."""
    now = time.monotonic()
    deadline = _last_nudge_at.get(email, 0.0)
    if now < deadline:
        return
    _last_nudge_at[email] = now + _NUDGE_COOLDOWN_SECONDS
    try:
        tmux_service.fire_nudge()
    except Exception as e:  # pragma: no cover — tmux errors logged inside
        logger.debug("fire_nudge raised: %s", e)


async def _process_single_account(
    account: Account,
    active_email: str | None,
) -> tuple[dict, str | None]:
    """Fetch token, optionally refresh, probe usage, and update caches for
    one account.

    Returns ``(usage_entry, new_stale_reason)`` — ``new_stale_reason`` is
    the updated DB column value, or ``None`` if unchanged.
    """
    new_stale_reason: str | None = None
    is_active = active_email is not None and account.email == active_email

    try:
        # Read credentials from the right Keychain namespace.
        credentials = ac.read_credentials_for_email(account.email, active_email)
        if not credentials:
            new_stale_reason = "No access token in vault — re-login required"
            raise ValueError(new_stale_reason)

        token = cp.access_token_of(credentials)
        if not token:
            new_stale_reason = "No access token in vault — re-login required"
            raise ValueError(new_stale_reason)

        # Hydrate the token-info cache so GET /api/accounts can read expiry
        # + subscription metadata without spawning a Keychain subprocess
        # per row.
        token_info = cp.token_info_of(credentials)
        await cache.set_token_info(account.email, token_info)

        # Refresh the token if near expiry — ONLY for vault accounts.
        # The active account's refresh lifecycle is owned by Claude Code;
        # CCSwitch refreshing it would race with the CLI.
        if not account.stale_reason and not is_active:
            try:
                expires_at_ms = token_info.get("token_expires_at")
                now_ms = int(time.time() * 1000)
                if expires_at_ms and now_ms > expires_at_ms - _REFRESH_SKEW_MS:
                    refresh_token = cp.refresh_token_of(credentials)
                    if refresh_token:
                        resp = await anthropic_api.refresh_access_token(refresh_token)
                        new_token = resp.get("access_token")
                        if new_token:
                            expires_in = resp.get("expires_in")
                            new_expires_at_ms = (
                                now_ms + int(expires_in) * 1000
                                if expires_in
                                else None
                            )
                            new_refresh = resp.get("refresh_token")
                            await asyncio.to_thread(
                                cp.save_refreshed_vault_token,
                                account.email, new_token,
                                new_expires_at_ms, new_refresh,
                            )
                            token = new_token
                            logger.info("Refreshed vault token for %s", account.email)
            except httpx.HTTPStatusError as refresh_http_err:
                status = refresh_http_err.response.status_code
                # Anthropic returns 400 when the refresh_token has been
                # rotated or invalidated and 401 when it is explicitly
                # revoked.  Both are terminal — the follow-up probe would
                # just fail with 401 — so we raise a marker that skips the
                # probe and preserves the precise stale_reason.
                if status in (400, 401):
                    reason_detail = "revoked" if status == 401 else "rejected (400)"
                    logger.error(
                        "Refresh token %s for %s — re-login required.",
                        reason_detail, account.email,
                    )
                    new_stale_reason = f"Refresh token {reason_detail} — re-login required"
                    raise _RefreshTerminal()
                logger.warning(
                    "Token refresh HTTP error for %s: %s",
                    account.email, refresh_http_err,
                )
            except _RefreshTerminal:
                raise
            except Exception as refresh_err:
                logger.warning(
                    "Token refresh failed for %s: %s", account.email, refresh_err
                )

        # Probe usage — skip if in 429 backoff window.
        backoff_deadline = _backoff_until.get(account.email, 0)
        if time.monotonic() < backoff_deadline:
            remaining = int(backoff_deadline - time.monotonic())
            logger.debug(
                "Skipping probe for %s — 429 backoff active (%ds remaining)",
                account.email, remaining,
            )
            cached = await cache.get_usage_async(account.email)
            token_info = await cache.get_token_info_async(account.email) or {}
            try:
                flat = build_usage(cached, token_info)
                flat_dict = flat.model_dump() if flat else {}
            except Exception as _bu_err:
                logger.warning("build_usage failed for %s: %s", account.email, _bu_err)
                flat_dict = {}
            return {
                "id": account.id,
                "email": account.email,
                "usage": flat_dict,
                "error": cached.get("error"),
            }, new_stale_reason

        try:
            usage = await anthropic_api.probe_usage(token)
        except httpx.HTTPStatusError as probe_err:
            status = probe_err.response.status_code
            if status == 401:
                if is_active:
                    # Active-account 401 is NOT stale_reason — the stored
                    # access token is stale because CCSwitch does not
                    # refresh it (the CLI owns that lifecycle).  Nudge
                    # any sleeping claude pane to wake up and force a
                    # refresh on its next call, and return last-known
                    # cached usage so the UI does not show a red error.
                    _maybe_nudge_active(account.email)
                    cached = await cache.get_usage_async(account.email)
                    cached_dict = cached if isinstance(cached, dict) else {}
                    cached_ti = await cache.get_token_info_async(account.email) or {}
                    try:
                        flat = build_usage(cached_dict, cached_ti)
                        flat_dict = flat.model_dump() if flat else {}
                    except Exception as _bu_err:
                        logger.warning(
                            "build_usage failed for %s: %s",
                            account.email, _bu_err,
                        )
                        flat_dict = {}
                    return {
                        "id": account.id,
                        "email": account.email,
                        "usage": flat_dict,
                        "error": cached_dict.get("error"),
                    }, account.stale_reason
                new_stale_reason = "Anthropic API returned 401 — re-login required"
                raise
            elif status == 429:
                count = _backoff_count.get(account.email, 0) + 1
                _backoff_count[account.email] = count
                backoff_seconds = min(
                    _BACKOFF_INITIAL * (2 ** (count - 1)), _BACKOFF_MAX
                )
                _backoff_until[account.email] = time.monotonic() + backoff_seconds
                logger.warning(
                    "429 for %s (offense #%d) — backing off %ds",
                    account.email, count, backoff_seconds,
                )
                raise
            else:
                raise

        # Successful probe — clear backoff.
        _backoff_until.pop(account.email, None)
        _backoff_count.pop(account.email, None)

        await cache.set_usage(account.email, usage)
        token_info = await cache.get_token_info_async(account.email) or {}
        try:
            flat = build_usage(usage, token_info)
            flat_dict = flat.model_dump() if flat else {}
        except Exception as _bu_err:
            logger.warning("build_usage failed for %s: %s", account.email, _bu_err)
            flat_dict = {}
        usage_entry: dict = {
            "id": account.id,
            "email": account.email,
            "usage": flat_dict,
            "error": None,
        }
    except _RefreshTerminal:
        err_str = new_stale_reason or "Refresh token invalid"
        new_entry, err_str = await cache.set_usage_error(account.email, err_str, False)
        token_info = await cache.get_token_info_async(account.email) or {}
        try:
            flat = build_usage(new_entry, token_info)
            flat_dict = flat.model_dump() if flat else {"error": err_str}
        except Exception as _bu_err:
            logger.warning("build_usage failed for %s: %s", account.email, _bu_err)
            flat_dict = {"error": err_str}
        usage_entry = {
            "id": account.id,
            "email": account.email,
            "usage": flat_dict,
            "error": err_str,
        }
    except Exception as e:
        err_str = str(e)
        if isinstance(e, httpx.HTTPStatusError):
            err_str = f"HTTP {e.response.status_code}"
            try:
                body = e.response.json()
                msg = (body.get("error") or {}).get("message") or body.get("message")
                if msg:
                    err_str = msg
            except Exception:
                pass
        logger.warning("Usage fetch failed for %s: %s", account.email, err_str)

        if isinstance(e, httpx.HTTPStatusError):
            is_rate_limited = e.response.status_code == 429
        else:
            is_rate_limited = "429" in str(e) or "rate_limit" in str(e).lower()
        new_entry, err_str = await cache.set_usage_error(
            account.email, err_str, is_rate_limited
        )

        token_info = await cache.get_token_info_async(account.email) or {}
        try:
            flat = build_usage(new_entry, token_info)
            flat_dict = flat.model_dump() if flat else {"error": err_str}
        except Exception as _bu_err:
            logger.warning("build_usage failed for %s: %s", account.email, _bu_err)
            flat_dict = {"error": err_str}
        usage_entry = {
            "id": account.id,
            "email": account.email,
            "usage": flat_dict,
            "error": err_str if "error" in new_entry else None,
        }

    return usage_entry, new_stale_reason


async def poll_usage_and_switch(ws: WebSocketManager) -> None:
    global _last_poll_monotonic

    async with AsyncSessionLocal() as db:
        # Detect a sleep/wake boundary.  When the monotonic clock has
        # jumped past the threshold since the last poll, every vault
        # account's access_token has likely expired simultaneously — a
        # burst of N concurrent /oauth/token POSTs looks like bot
        # traffic to Anthropic.  A small random stagger removes the
        # burst signature without meaningfully delaying recovery.
        now_monotonic = time.monotonic()
        if (
            _last_poll_monotonic is not None
            and now_monotonic - _last_poll_monotonic > _SLEEP_DETECTION_THRESHOLD_SECONDS
        ):
            stagger = random.uniform(0.0, 30.0)
            logger.info(
                "Sleep/wake detected (gap=%.1fs); staggering refresh burst by %.1fs",
                now_monotonic - _last_poll_monotonic, stagger,
            )
            await asyncio.sleep(stagger)
        _last_poll_monotonic = now_monotonic

        active_email = await ac.get_active_email_async()

        accounts_result = await db.execute(select(Account))
        accounts = accounts_result.scalars().all()

        results = await asyncio.gather(
            *[_process_single_account(account, active_email) for account in accounts],
            return_exceptions=True,
        )

        updated = []
        stale_changed = False
        for account, result in zip(accounts, results):
            # ``asyncio.gather(return_exceptions=True)`` captures both
            # Exception and BaseException subclasses (notably
            # CancelledError during lifespan shutdown).
            if isinstance(result, BaseException):
                logger.exception(
                    "_process_single_account raised for %s: %s",
                    account.email, result,
                )
                usage_entry = {
                    "id": account.id,
                    "email": account.email,
                    "usage": {"error": str(result)},
                    "error": str(result),
                }
                new_stale_reason = account.stale_reason
            else:
                usage_entry, new_stale_reason = result

            usage_entry["stale_reason"] = new_stale_reason
            updated.append(usage_entry)

            if new_stale_reason != account.stale_reason:
                account.stale_reason = new_stale_reason
                await cache.invalidate_token_info(account.email)
                stale_changed = True
                if new_stale_reason:
                    logger.warning(
                        "Marking %s stale: %s", account.email, new_stale_reason
                    )
                else:
                    logger.info("Cleared stale flag for %s", account.email)

        if stale_changed:
            await db.commit()

        try:
            await ws.broadcast({"type": "usage_updated", "accounts": updated})
        except Exception as _bc_err:
            logger.warning("WS broadcast failed: %s", _bc_err)

        # ── Auto-switch logic ─────────────────────────────────────────────────
        await sw.maybe_auto_switch(db, ws)
