import logging
import asyncio
import time
import httpx

from sqlalchemy import select

from .database import AsyncSessionLocal
from .models import Account, TmuxMonitor
from .services import account_service as ac
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
        for account in accounts:
            try:
                token = ac.get_access_token_from_config_dir(account.config_dir)
                if not token:
                    raise ValueError("No access token found in config directory")

                # Hydrate the token_info cache so GET /api/accounts can read
                # expiry + subscription metadata without spawning a Keychain
                # subprocess per account per request.
                token_info = ac.get_token_info(account.config_dir)
                async with _cache_lock:
                    token_info_cache[account.email] = token_info

                # Refresh the token if it is about to expire (within 5 minutes).
                # Anthropic's OAuth response returns `expires_in` (seconds, relative);
                # Claude Code stores `expiresAt` in milliseconds (JS convention), so we
                # need to translate before persisting or the next poll will consider
                # the token still expired and refresh again on every cycle.
                try:
                    expires_at_ms = token_info.get("token_expires_at")
                    if expires_at_ms and time.time() * 1000 > expires_at_ms - 300_000:
                        refresh_token = ac.get_refresh_token_from_config_dir(account.config_dir)
                        if refresh_token:
                            resp = await anthropic_api.refresh_access_token(refresh_token)
                            new_token = resp.get("access_token")
                            if new_token:
                                expires_in = resp.get("expires_in")
                                new_expires_at_ms = (
                                    int((time.time() + expires_in) * 1000)
                                    if expires_in
                                    else None
                                )
                                ac.save_refreshed_token(
                                    account.config_dir, new_token, new_expires_at_ms
                                )
                                token = new_token
                                logger.info("Refreshed access token for %s", account.email)
                except Exception as refresh_err:
                    logger.warning("Token refresh failed for %s: %s", account.email, refresh_err)

                usage = await anthropic_api.probe_usage(token)
                async with _cache_lock:
                    usage_cache[account.email] = usage
                updated.append({
                    "id": account.id,
                    "email": account.email,
                    "usage": usage,
                    "error": None,
                })
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
                prev = usage_cache.get(account.email, {})
                if is_rate_limited and prev and "error" not in prev:
                    async with _cache_lock:
                        usage_cache[account.email] = {**prev, "rate_limited": True}
                    err_str = "Rate limited"
                else:
                    async with _cache_lock:
                        usage_cache[account.email] = {"error": err_str}
                updated.append({
                    "id": account.id,
                    "email": account.email,
                    "usage": usage_cache[account.email],
                    "error": err_str if "error" in usage_cache.get(account.email, {}) else None,
                })

        await ws.broadcast({"type": "usage_updated", "accounts": updated})

        # ── Auto-switch logic ─────────────────────────────────────────────────
        auto_enabled = await ss.get_bool("auto_switch_enabled", True, db)

        if not auto_enabled:
            return

        current_email = ac.get_active_email()
        if not current_email:
            return

        # Find the current account to get its per-account threshold
        cur_result = await db.execute(
            select(Account).where(Account.email == current_email)
        )
        current_account = cur_result.scalars().first()
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
                await ws.broadcast({
                    "type": "error",
                    "message": "Rate limit reached — no eligible accounts to switch to",
                })

