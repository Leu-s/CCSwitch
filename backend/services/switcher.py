from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime
from ..models import Account, SwitchLog
from ..ws import WebSocketManager
from ..config import settings
from . import keychain as kc

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
    ws: WebSocketManager
) -> None:
    current_email = kc.get_active_email(settings.claude_config_dir)

    # Read target credentials from their dedicated Keychain entry
    creds = kc.read_credentials(target.keychain_suffix)

    # Overwrite the active Keychain slot
    kc.write_active_credentials(creds)

    # Surgically update oauthAccount in .claude.json
    oauth = {
        "emailAddress": target.email,
        "accountUuid": target.account_uuid or "",
        "organizationUuid": target.org_uuid or "",
        "organizationName": target.display_name or target.email,
        "hasExtraUsageEnabled": False,
        "billingType": "stripe_subscription",
    }
    kc.update_oauth_account(settings.claude_config_dir, oauth)

    # Find current account id for the log
    from_acc = None
    if current_email:
        result = await db.execute(select(Account).where(Account.email == current_email))
        from_acc = result.scalars().first()

    log = SwitchLog(
        from_account_id=from_acc.id if from_acc else None,
        to_account_id=target.id,
        reason=reason,
        triggered_at=datetime.utcnow()
    )
    db.add(log)
    await db.commit()

    await ws.broadcast({
        "type": "account_switched",
        "from": current_email,
        "to": target.email,
        "reason": reason
    })
