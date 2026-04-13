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

logger = logging.getLogger(__name__)

# In-memory usage cache: {email: usage_dict}
usage_cache: dict[str, dict] = {}
# In-memory token metadata cache: {email: token_info_dict}.  Populated here so
# GET /api/accounts does not need a subprocess Keychain lookup per account on
# every request (N+1).  New accounts fall back to a direct lookup until the
# next poll hydrates this cache.
token_info_cache: dict[str, dict] = {}
_cache_lock = asyncio.Lock()


async def snapshot_usage_cache() -> dict[str, dict]:
    """Shallow copy of usage_cache taken under the lock so callers can
    iterate safely without racing the next poll cycle."""
    async with _cache_lock:
        return dict(usage_cache)


async def forget_account(email: str) -> None:
    """Drop any cached usage and token-info entries for an account that has
    been deleted. Held under _cache_lock so an in-flight poll cycle cannot
    re-insert a stale entry between the two pops."""
    async with _cache_lock:
        usage_cache.pop(email, None)
        token_info_cache.pop(email, None)


async def _poll_one_account(account, db) -> tuple[dict, bool]:
    """Poll usage for a single account and return (update_entry, stale_changed)."""
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
        async with _cache_lock:
            token_info_cache[account.email] = token_info

        # Refresh the token if it is about to expire (within 5 minutes).
        # Compute in integer milliseconds throughout to avoid float drift.
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
                        ac.save_refreshed_token(
                            account.config_dir, new_token, new_expires_at_ms
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
                ac.save_refreshed_token(account.config_dir, token, expires_at=1)
            else:
                logger.warning("Token refresh HTTP error for %s: %s", account.email, refresh_http_err)
        except Exception as refresh_err:
            logger.warning("Token refresh failed for %s: %s", account.email, refresh_err)

        # Probe usage; a 401 here also means the credentials are dead.
        try:
            usage = await anthropic_api.probe_usage(token)
        except httpx.HTTPStatusError as probe_err:
            if probe_err.response.status_code == 401:
                new_stale_reason = "Anthropic API returned 401 — re-login required"
            raise

        async with _cache_lock:
            usage_cache[account.email] = usage
        token_info = token_info_cache.get(account.email, {})
        try:
            flat = build_usage(usage, token_info)
            flat_dict = flat.model_dump() if flat else {}
        except Exception as _bu_err:
            logger.warning("build_usage failed for %s: %s", account.email, _bu_err)
            flat_dict = {}
        update_entry = {
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

        # Determine new cache entry and err_str inside a single lock
        # to avoid stale-read races between check and write.
        is_rate_limited = "429" in str(e) or "rate_limit" in str(e).lower()
        async with _cache_lock:
            prev = usage_cache.get(account.email, {})
            if is_rate_limited and prev and "error" not in prev:
                new_entry = {**prev, "rate_limited": True}
                err_str = "Rate limited"
            else:
                new_entry = {"error": err_str}
            usage_cache[account.email] = new_entry

        token_info = token_info_cache.get(account.email, {})
        try:
            flat = build_usage(new_entry, token_info)
            flat_dict = flat.model_dump() if flat else {"error": err_str}
        except Exception as _bu_err:
            logger.warning("build_usage failed for %s: %s", account.email, _bu_err)
            flat_dict = {"error": err_str}
        update_entry = {
            "id": account.id,
            "email": account.email,
            "usage": flat_dict,
            "error": err_str if "error" in new_entry else None,
        }

    # Flip stale_reason on the DB row if it changed. Rate-limiting is NOT
    # staleness — we only set stale for 401-class auth failures.
    stale_changed = False
    if new_stale_reason != account.stale_reason:
        account.stale_reason = new_stale_reason
        async with _cache_lock:
            token_info_cache.pop(account.email, None)
        stale_changed = True
        if new_stale_reason:
            logger.warning("Marking %s stale: %s", account.email, new_stale_reason)
        else:
            logger.info("Cleared stale flag for %s", account.email)

    return update_entry, stale_changed


async def _broadcast_usage(ws, updated: list) -> None:
    """Broadcast the usage_updated event to all connected WebSocket clients."""
    try:
        await ws.broadcast({"type": "usage_updated", "accounts": updated})
    except Exception as _bc_err:
        logger.warning("WS broadcast failed: %s", _bc_err)


async def _auto_switch_if_needed(ws, db) -> None:
    """Check usage thresholds and perform an auto-switch if needed."""
    # ── Auto-switch logic ─────────────────────────────────────────────────
    auto_enabled = await ss.get_bool("auto_switch_enabled", False, db)

    if not auto_enabled:
        return

    current_email = ac.get_active_email()
    if not current_email:
        return

    # Find the current account to get its per-account threshold
    current_account = await ac.get_account_by_email(current_email, db)
    if not current_account:
        return

    async with _cache_lock:
        current_usage = usage_cache.get(current_email, {})
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
            entry, changed = await _poll_one_account(account, db)
            updated.append(entry)
            stale_changed = stale_changed or changed

        if stale_changed:
            await db.commit()

        await _broadcast_usage(ws, updated)
        await _auto_switch_if_needed(ws, db)
