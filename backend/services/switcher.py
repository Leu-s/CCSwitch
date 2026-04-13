import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Account, SwitchLog
from ..ws import WebSocketManager
from . import account_service as ac
from . import account_queries as aq
from . import credential_targets as ct
from . import settings_service as ss
from . import tmux_service
from ..cache import cache

logger = logging.getLogger(__name__)

# Serializes every call to perform_switch.  activate_account_config touches
# several shared artefacts (enabled mirror targets, legacy Keychain,
# ~/.claude-multi/active) that must move together — two concurrent switches
# can interleave and leave the system with one account's oauthAccount in one
# file and a different account's Keychain entry.
_switch_lock = asyncio.Lock()


async def get_next_account(current_email: str, db: AsyncSession) -> Account | None:
    result = await db.execute(
        select(Account)
        .where(Account.enabled == True)
        .where(Account.stale_reason == None)
        .where(Account.email != current_email)
        .order_by(Account.priority.asc(), Account.id.asc())
    )
    return result.scalars().first()


async def switch_if_active_disabled(
    account: "Account",
    db: AsyncSession,
    ws: WebSocketManager,
) -> None:
    """
    If the given account is the currently active account, switch away from it.
    Called when an account is disabled via the API.
    """
    if account.email != await ac.get_active_email_async():
        return
    service_enabled = await ss.get_bool("service_enabled", False, db)
    if not service_enabled:
        return
    next_acc = await get_next_account(account.email, db)
    if next_acc:
        await perform_switch(next_acc, "manual", db, ws)


async def perform_sync_to_targets(db: AsyncSession) -> dict:
    """Re-mirror the active account's identity into every enabled credential
    target.  Acquires ``_switch_lock`` so the sync cannot interleave with a
    concurrent ``perform_switch`` and read a half-updated pointer.
    """
    async with _switch_lock:
        enabled = await ct.enabled_canonical_paths(db)
        return await asyncio.to_thread(ac.sync_active_to_targets, enabled)


async def perform_switch(
    target: Account,
    reason: str,
    db: AsyncSession,
    ws: WebSocketManager,
) -> None:
    async with _switch_lock:
        current_email = await ac.get_active_email_async()

        # Fetch the user-chosen mirror targets while we hold the lock so two
        # switches never observe different enabled sets and disagree about
        # which system files to touch.
        enabled_targets = await ct.enabled_canonical_paths(db)

        summary = await asyncio.to_thread(
            ac.activate_account_config, target.config_dir, enabled_targets
        )

        # Log the switch
        from_acc = None
        if current_email:
            from_acc = await aq.get_account_by_email(current_email, db)

        log = SwitchLog(
            from_account_id=from_acc.id if from_acc else None,
            to_account_id=target.id,
            reason=reason,
            triggered_at=datetime.now(timezone.utc),
        )
        db.add(log)
        await db.commit()

        try:
            await ws.broadcast({
                "type": "account_switched",
                "from": current_email,
                "to": target.email,
                "reason": reason,
                "mirror": (summary or {}).get("mirror", {}),
            })
        except Exception as _bc_err:
            logger.warning("WS broadcast failed: %s", _bc_err)


# ── Auto-switch decision logic ────────────────────────────────────────────────


async def maybe_auto_switch(db, ws: WebSocketManager) -> None:
    """Check auto-switch threshold for the active account and switch if needed.

    Moved from background.py to keep all auto-switching decision logic together
    with get_next_account and perform_switch.
    """
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

    # Stale fast-path: if current account is dead, switch immediately regardless of usage
    if current_account.stale_reason:
        next_account = await get_next_account(current_email, db)
        if next_account:
            logger.info(
                "Auto-switching %s → %s (current account stale: %s)",
                current_email, next_account.email,
                next_account.stale_reason or current_account.stale_reason,
            )
            await perform_switch(next_account, "stale", db, ws)
            tmux_service.fire_nudge()
        else:
            logger.warning("Current account stale but no eligible replacement")
            try:
                await ws.broadcast({
                    "type": "error",
                    "message": "Active account credentials expired — no replacement available",
                })
            except Exception as _bc_err:
                logger.warning("WS broadcast failed: %s", _bc_err)
        return

    current_usage = await cache.get_usage_async(current_email)
    five_hour_pct = (current_usage.get("five_hour") or {}).get("utilization", 0)
    threshold = current_account.threshold_pct
    is_rate_limited = bool(current_usage.get("rate_limited"))

    # Two ways to trigger a switch:
    #   1. Last good probe shows utilization above the per-account threshold.
    #   2. Last probe came back 429 (or otherwise rate-limited).  In that
    #      case we may have ZERO recent utilization data — but we still
    #      know the current account is being throttled and we should move
    #      on instead of waiting for backoff to expire.
    if is_rate_limited or five_hour_pct >= threshold:
        next_account = await get_next_account(current_email, db)
        if next_account:
            if is_rate_limited:
                reason_log = "rate_limited"
                logger.info(
                    "Auto-switching %s → %s (rate-limited probe response)",
                    current_email, next_account.email,
                )
            else:
                reason_log = "threshold"
                logger.info(
                    "Auto-switching %s → %s (usage %.1f%% ≥ threshold %.1f%%)",
                    current_email, next_account.email, five_hour_pct, threshold,
                )
            await perform_switch(next_account, reason_log, db, ws)
            tmux_service.fire_nudge()
        else:
            logger.warning("No eligible account to switch to")
            try:
                await ws.broadcast({
                    "type": "error",
                    "message": "Rate limit reached — no eligible accounts to switch to",
                })
            except Exception as _bc_err:
                logger.warning("WS broadcast failed: %s", _bc_err)
