from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Account, SwitchLog
from ..ws import WebSocketManager
from ..config import settings
from . import account_service as ac


async def get_next_account(current_email: str, db: AsyncSession) -> Account | None:
    result = await db.execute(
        select(Account)
        .where(Account.enabled == True)
        .where(Account.email != current_email)
        .order_by(Account.priority.asc(), Account.id.asc())
    )
    return result.scalars().first()


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

    await ws.broadcast({
        "type": "account_switched",
        "from": current_email,
        "to": target.email,
        "reason": reason,
    })
