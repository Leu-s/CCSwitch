import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..cache import cache
from ..models import Account, SwitchLog
from ..ws import WebSocketManager
from . import account_queries as aq
from . import account_service as ac
from . import settings_service as ss
from . import tmux_service


logger = logging.getLogger(__name__)

# Serializes every call to perform_switch.  swap_to_account touches several
# shared Keychain entries + the identity file, so two concurrent switches
# could interleave and leave one account's oauthAccount paired with another
# account's tokens.  The underlying credential lock prevents intra-thread
# interleaving but this asyncio lock also serializes the DB write + broadcast
# so clients see a clean "A → B" event instead of a partial overlap.
_switch_lock = asyncio.Lock()

# Hard deadline on the blocking portion of a swap.  Without step 0.5's HTTP
# refresh, a swap is pure subprocess calls (each timeboxed at 5 s).  Worst
# case: ~6 calls × 5 s = 30 s, but a healthy swap completes in <1 s.  The
# 10 s ceiling catches Keychain hangs (locked keychain, FileVault prompt,
# system sleep mid-swap) without being so tight that a slow-but-healthy swap
# trips it.  The worker thread is NOT terminated on timeout (Python has no
# thread-kill), but ``asyncio.wait_for`` cancels the Future so control
# returns, the lock releases, and the user sees a 409 instead of a hang.
_SWAP_DEADLINE = 10.0


async def get_next_account(current_email: str, db: AsyncSession) -> Account | None:
    """Pick the next enabled, non-stale account that also has available
    rate-limit capacity.

    Filtering order:
      1. DB-level: enabled, not stale, not the current account.
      2. Cache-level: skip candidates whose last probe was rate-limited
         or whose recorded five-hour utilization is already at or above
         their own ``threshold_pct``.
    """
    result = await db.execute(
        select(Account)
        .where(Account.enabled == True)
        .where(Account.stale_reason == None)
        .where(Account.email != current_email)
        .order_by(Account.priority.asc(), Account.id.asc())
    )
    candidates = result.scalars().all()

    for candidate in candidates:
        usage = await cache.get_usage_async(candidate.email)
        if not usage:
            # No probe data yet — keep it in the pool.
            return candidate
        if usage.get("rate_limited"):
            logger.debug(
                "Skipping %s: last probe rate-limited", candidate.email
            )
            continue
        five_hour_pct = (usage.get("five_hour") or {}).get("utilization", 0) or 0
        if five_hour_pct >= candidate.threshold_pct:
            logger.debug(
                "Skipping %s: cached usage %.1f%% ≥ threshold %.1f%%",
                candidate.email, five_hour_pct, candidate.threshold_pct,
            )
            continue
        return candidate

    return None


async def switch_if_active_disabled(
    account: Account,
    db: AsyncSession,
    ws: WebSocketManager,
) -> None:
    """If ``account`` is the currently active one, swap away from it.

    Called when a user explicitly disables the currently-active account
    via the PATCH endpoint.  The ``service_enabled`` master toggle is
    NOT checked here: the user's explicit "disable this" action should
    move them off the account whether or not auto-switching is on.
    When no replacement is eligible the function is a no-op and the
    disabled account remains active until the user picks another one
    manually — better than silently breaking the "disable = move away"
    mental model.
    """
    if account.email != await ac.get_active_email_async():
        return
    next_acc = await get_next_account(account.email, db)
    if next_acc:
        try:
            await perform_switch(next_acc, "manual", db, ws)
        except ac.SwapError as e:
            # The disable action is best-effort: if the only candidate's
            # refresh_token is dead, leave the previous active account in
            # place and rely on the account_updated broadcast (already
            # fired by perform_switch) to surface the stale state to the UI.
            logger.warning("Disable-cascade swap skipped: %s", e)


async def perform_switch(
    target: Account,
    reason: str,
    db: AsyncSession,
    ws: WebSocketManager,
) -> None:
    """Swap active account to ``target`` and record the event.

    Raises ``ac.SwapError`` on any failure so callers can decide how to
    surface it (manual_switch → 409 to user; auto-switch → log + skip).
    """
    async with _switch_lock:
        current_email = await ac.get_active_email_async()

        try:
            summary = await asyncio.wait_for(
                asyncio.to_thread(ac.swap_to_account, target.email),
                timeout=_SWAP_DEADLINE,
            )
        except asyncio.TimeoutError:
            logger.error(
                "Swap to %s exceeded %.0fs deadline — worker thread stuck "
                "(likely stale refresh_lock or Keychain subprocess hang); "
                "releasing _switch_lock so other accounts remain swappable.",
                target.email, _SWAP_DEADLINE,
            )
            # The zombie worker thread still holds refresh_lock(target.email).
            # Discard the poisoned lock so the next swap to the same email
            # creates a fresh one instead of blocking forever on the old one.
            ac.forget_refresh_lock(target.email)
            try:
                await ws.broadcast({
                    "type": "error",
                    "message": (
                        f"Swap to {target.email} timed out — the refresh or "
                        "Keychain write is stuck.  Try again in a minute; "
                        "if it persists, restart the server."
                    ),
                })
            except Exception as _bc_err:
                logger.warning("WS broadcast failed: %s", _bc_err)
            raise ac.SwapError(
                f"Swap to {target.email} timed out after {_SWAP_DEADLINE:.0f}s"
            ) from None
        except ac.SwapError as e:
            logger.error("Swap to %s failed: %s", target.email, e)
            try:
                await ws.broadcast({
                    "type": "error",
                    "message": f"Swap to {target.email} failed: {e}",
                })
            except Exception as _bc_err:
                logger.warning("WS broadcast failed: %s", _bc_err)
            raise

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
                "summary": summary,
            })
        except Exception as _bc_err:
            logger.warning("WS broadcast failed: %s", _bc_err)

        # Nudge every stalled claude tmux pane so running CLI sessions pick
        # up the fresh Keychain credentials.  Fire-and-forget — the nudge
        # helper owns its own DB session and does nothing when the feature
        # is disabled in Settings.  Runs for EVERY switch (manual or auto)
        # so a user-initiated switch from the UI also wakes stalled panes.
        tmux_service.fire_nudge()


# ── Auto-switch decision logic ────────────────────────────────────────────────


async def maybe_auto_switch(db, ws: WebSocketManager) -> None:
    """Check auto-switch threshold for the active account and switch if needed.

    Called on every poll cycle regardless of ``service_enabled`` — but only
    does work when ``service_enabled`` is true.  Usage polling is independent
    of the switch master-toggle: the dashboard always shows live rate-limit
    bars, and flipping the toggle only decides whether threshold / 429
    signals will actually move credentials over to the next account.
    """
    service_enabled = await ss.get_bool("service_enabled", False, db)
    if not service_enabled:
        return

    current_email = await ac.get_active_email_async()
    if not current_email:
        return

    current_account = await aq.get_account_by_email(current_email, db)
    if not current_account:
        return

    # Stale fast-path: if current account is dead, switch immediately
    # regardless of usage.
    if current_account.stale_reason:
        next_account = await get_next_account(current_email, db)
        if next_account:
            logger.info(
                "Auto-switching %s → %s (current account stale: %s)",
                current_email, next_account.email,
                next_account.stale_reason or current_account.stale_reason,
            )
            try:
                await perform_switch(next_account, "stale", db, ws)
            except ac.SwapError as e:
                logger.warning("Auto-switch to %s failed: %s", next_account.email, e)
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
            try:
                await perform_switch(next_account, reason_log, db, ws)
            except ac.SwapError as e:
                logger.warning("Auto-switch to %s failed: %s", next_account.email, e)
        else:
            logger.warning("No eligible account to switch to")
            try:
                await ws.broadcast({
                    "type": "error",
                    "message": "Rate limit reached — no eligible accounts to switch to",
                })
            except Exception as _bc_err:
                logger.warning("WS broadcast failed: %s", _bc_err)
