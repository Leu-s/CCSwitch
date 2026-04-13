import logging
import asyncio
import time
import httpx

from sqlalchemy import select

from .database import AsyncSessionLocal
from .models import Account, TmuxMonitor
from .services import account_service as ac
from .services.account_service import build_usage
from .services import anthropic_api
from .services import settings_service as ss
from .services import switcher as sw
from .services import tmux_service
from .ws import WebSocketManager
from .config import settings
from .cache import cache
from .services import account_queries as aq

logger = logging.getLogger(__name__)

# ── Per-account 429 backoff state ────────────────────────────────────────────
# Maps email → monotonic deadline (seconds); if time.monotonic() < deadline,
# skip the probe and return stale cached data instead.
_backoff_until: dict[str, float] = {}
# Maps email → consecutive 429 count for exponential doubling.
_backoff_count: dict[str, int] = {}

_BACKOFF_INITIAL = 120   # first 429: wait 2 minutes
_BACKOFF_MAX = 3600      # cap at 1 hour


async def _process_single_account(account: Account, db) -> tuple[dict, "str | None"]:
    """Fetch token, optionally refresh, probe usage, and update caches for one account.

    Returns:
        (usage_entry, new_stale_reason) where usage_entry is a dict with keys
        ``id``, ``email``, ``usage``, ``error`` suitable for the WS broadcast,
        and new_stale_reason is the (possibly None) stale reason to persist.
    """
    new_stale_reason: str | None = None
    try:
        token = await asyncio.to_thread(ac.get_access_token_from_config_dir, account.config_dir)
        if not token:
            new_stale_reason = "No access token in config dir — re-login required"
            raise ValueError(new_stale_reason)

        # Hydrate the token_info cache so GET /api/accounts can read
        # expiry + subscription metadata without spawning a Keychain
        # subprocess per account per request.
        token_info = await asyncio.to_thread(ac.get_token_info, account.config_dir)
        await cache.set_token_info(account.email, token_info)

        # Refresh the token if it is about to expire (within 5 minutes).
        # Skip for already-stale accounts: their refresh token is revoked,
        # so retrying would just produce a 401 log entry on every poll cycle.
        # Compute in integer milliseconds throughout to avoid float drift.
        if not account.stale_reason:
            try:
                expires_at_ms = token_info.get("token_expires_at")
                now_ms = int(time.time() * 1000)
                if expires_at_ms and now_ms > expires_at_ms - 300_000:
                    refresh_token = await asyncio.to_thread(ac.get_refresh_token_from_config_dir, account.config_dir)
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
                            await asyncio.to_thread(
                                ac.save_refreshed_token, account.config_dir, new_token, new_expires_at_ms
                            )
                            token = new_token
                            logger.info("Refreshed access token for %s", account.email)
            except httpx.HTTPStatusError as refresh_http_err:
                if refresh_http_err.response.status_code == 401:
                    logger.error(
                        "Refresh token revoked for %s — re-login required.",
                        account.email,
                    )
                    new_stale_reason = "Refresh token revoked — re-login required"
                    # Mark token as permanently expired so we don't retry on every poll.
                    await asyncio.to_thread(ac.save_refreshed_token, account.config_dir, token, expires_at=1)
                else:
                    logger.warning("Token refresh HTTP error for %s: %s", account.email, refresh_http_err)
            except Exception as refresh_err:
                logger.warning("Token refresh failed for %s: %s", account.email, refresh_err)

        # Probe usage; a 401 here also means the credentials are dead.
        # Skip the probe if the account is in a 429 backoff window — return
        # stale cached data instead of hammering the endpoint.
        backoff_deadline = _backoff_until.get(account.email, 0)
        if time.monotonic() < backoff_deadline:
            remaining = int(backoff_deadline - time.monotonic())
            logger.debug(
                "Skipping probe for %s — 429 backoff active (%ds remaining)",
                account.email, remaining,
            )
            cached = await cache.get_usage_async(account.email)
            token_info = cache.get_token_info(account.email) or {}
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
            if probe_err.response.status_code == 401:
                new_stale_reason = "Anthropic API returned 401 — re-login required"
            elif probe_err.response.status_code == 429:
                # Compute exponential backoff: double the previous window, capped.
                count = _backoff_count.get(account.email, 0) + 1
                _backoff_count[account.email] = count
                backoff_seconds = min(_BACKOFF_INITIAL * (2 ** (count - 1)), _BACKOFF_MAX)
                _backoff_until[account.email] = time.monotonic() + backoff_seconds
                logger.warning(
                    "429 for %s (offense #%d) — backing off %ds",
                    account.email, count, backoff_seconds,
                )
            raise

        # Successful probe — clear any backoff state for this account.
        _backoff_until.pop(account.email, None)
        _backoff_count.pop(account.email, None)

        await cache.set_usage(account.email, usage)
        token_info = cache.get_token_info(account.email) or {}
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

        is_rate_limited = "429" in str(e) or "rate_limit" in str(e).lower()
        new_entry, err_str = await cache.set_usage_error(account.email, err_str, is_rate_limited)

        token_info = cache.get_token_info(account.email) or {}
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


async def _maybe_auto_switch(db, ws: WebSocketManager) -> None:
    """Check auto-switch threshold for the active account and switch if needed."""
    auto_enabled = await ss.get_bool("auto_switch_enabled", False, db)
    if not auto_enabled:
        return

    current_email = await asyncio.to_thread(ac.get_active_email)
    if not current_email:
        return

    # Find the current account to get its per-account threshold
    current_account = await aq.get_account_by_email(current_email, db)
    if not current_account:
        return

    current_usage = await cache.get_usage_async(current_email)
    five_hour_pct = (current_usage.get("five_hour") or {}).get("utilization", 0)
    threshold = current_account.threshold_pct

    if five_hour_pct >= threshold:
        next_account = await sw.get_next_account(current_email, db)
        if next_account:
            logger.info(
                "Auto-switching %s → %s (usage %.1f%% ≥ threshold %.1f%%)",
                current_email, next_account.email, five_hour_pct, threshold,
            )
            await sw.perform_switch(next_account, "threshold", db, ws)

            # Notify tmux monitors
            monitors_result = await db.execute(
                select(TmuxMonitor).where(TmuxMonitor.enabled == True)
            )
            monitors = monitors_result.scalars().all()
            await tmux_service.notify_monitors(monitors, ws, settings.haiku_model)
        else:
            logger.warning("No eligible account to switch to")
            try:
                await ws.broadcast({
                    "type": "error",
                    "message": "Rate limit reached — no eligible accounts to switch to",
                })
            except Exception as _bc_err:
                logger.warning("WS broadcast failed: %s", _bc_err)


async def poll_usage_and_switch(ws: WebSocketManager) -> None:
    async with AsyncSessionLocal() as db:
        # ── Check service status FIRST ────────────────────────────────────────
        service_enabled = await ss.get_bool("service_enabled", False, db)
        if not service_enabled:
            return  # service is OFF — exit immediately, no polling, no broadcast

        # ── Poll usage for every account ──────────────────────────────────────
        accounts_result = await db.execute(select(Account))
        accounts = accounts_result.scalars().all()

        updated = []
        stale_changed = False
        for account in accounts:
            usage_entry, new_stale_reason = await _process_single_account(account, db)
            updated.append(usage_entry)

            # Flip stale_reason on the DB row if it changed. Rate-limiting is NOT
            # staleness — we only set stale for 401-class auth failures.
            if new_stale_reason != account.stale_reason:
                account.stale_reason = new_stale_reason
                await cache.invalidate_token_info(account.email)
                stale_changed = True
                if new_stale_reason:
                    logger.warning("Marking %s stale: %s", account.email, new_stale_reason)
                else:
                    logger.info("Cleared stale flag for %s", account.email)

        if stale_changed:
            await db.commit()

        try:
            await ws.broadcast({"type": "usage_updated", "accounts": updated})
        except Exception as _bc_err:
            logger.warning("WS broadcast failed: %s", _bc_err)

        # ── Auto-switch logic ─────────────────────────────────────────────────
        await _maybe_auto_switch(db, ws)
