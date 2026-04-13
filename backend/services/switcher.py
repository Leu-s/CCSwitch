import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Account, SwitchLog
from ..ws import WebSocketManager
from . import account_service as ac

logger = logging.getLogger(__name__)


async def get_next_account(current_email: str, db: AsyncSession) -> Account | None:
    result = await db.execute(
        select(Account)
        .where(Account.enabled == True)
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
    from . import account_service as ac
    from . import settings_service as ss

    if account.email != ac.get_active_email():
        return
    service_enabled = await ss.get_bool("service_enabled", False, db)
    if not service_enabled:
        return
    next_acc = await get_next_account(account.email, db)
    if next_acc:
        await perform_switch(next_acc, "manual", db, ws)


async def perform_switch(
    target: Account,
    reason: str,
    db: AsyncSession,
    ws: WebSocketManager,
) -> None:
    current_email = ac.get_active_email()

    # Copy the target account's config dir to ~/.claude/
    ac.activate_account_config(target.config_dir)

    # Log the switch
    from_acc = None
    if current_email:
        result = await db.execute(select(Account).where(Account.email == current_email))
        from_acc = result.scalars().first()

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
        })
    except Exception as _bc_err:
        logger.warning("WS broadcast failed: %s", _bc_err)
