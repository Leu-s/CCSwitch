"""
Service toggle: enable / disable the multi-account manager.

ON  — our system manages ~/.claude/ credentials, auto-switch is active.
OFF — original credentials restored (default account or pre-enable backup).
"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models import Account, Setting
from ..schemas import ServiceStatus
from ..services import account_service as ac

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/service", tags=["service"])


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _get_setting(key: str, default: str, db: AsyncSession) -> str:
    row = await db.execute(select(Setting).where(Setting.key == key))
    s = row.scalars().first()
    return s.value if s else default


async def _set_setting(key: str, value: str, db: AsyncSession) -> None:
    row = await db.execute(select(Setting).where(Setting.key == key))
    s = row.scalars().first()
    if s:
        s.value = value
    else:
        db.add(Setting(key=key, value=value))
    await db.commit()


# ── Status ─────────────────────────────────────────────────────────────────────

@router.get("", response_model=ServiceStatus)
async def get_service_status(db: AsyncSession = Depends(get_db)):
    enabled_raw = await _get_setting("service_enabled", "false", db)
    enabled = json.loads(enabled_raw) if enabled_raw else False

    default_raw = await _get_setting("default_account_id", "", db)
    default_id = int(default_raw) if default_raw.isdigit() else None

    return ServiceStatus(
        enabled=bool(enabled),
        active_email=ac.get_active_email(),
        default_account_id=default_id,
    )


# ── Enable ─────────────────────────────────────────────────────────────────────

@router.post("/enable")
async def enable_service(db: AsyncSession = Depends(get_db)):
    """
    Enable the service:
    1. Backup current ~/.claude/ config.
    2. Activate the default account (if set) or the first enabled account.
    """
    # Don't re-enable if already on (but allow re-activation)
    backup = ac.backup_active_config()
    await _set_setting("original_credentials_backup", json.dumps(backup), db)

    # Find which account to activate
    default_raw = await _get_setting("default_account_id", "", db)
    default_id = int(default_raw) if default_raw.isdigit() else None

    account_to_activate = None
    if default_id:
        r = await db.execute(select(Account).where(Account.id == default_id))
        account_to_activate = r.scalars().first()

    if not account_to_activate:
        r = await db.execute(
            select(Account)
            .where(Account.enabled == True)
            .order_by(Account.priority.asc(), Account.id.asc())
        )
        account_to_activate = r.scalars().first()

    if not account_to_activate:
        raise HTTPException(400, "No accounts available — add at least one account first")

    ac.activate_account_config(account_to_activate.config_dir)
    await _set_setting("service_enabled", "true", db)
    logger.info("Service enabled — active account: %s", account_to_activate.email)

    return {"ok": True, "active_email": account_to_activate.email}


# ── Disable ────────────────────────────────────────────────────────────────────

@router.post("/disable")
async def disable_service(db: AsyncSession = Depends(get_db)):
    """
    Disable the service:
    Restore the default account's config, or fall back to the pre-enable backup.
    """
    default_raw = await _get_setting("default_account_id", "", db)
    default_id = int(default_raw) if default_raw.isdigit() else None

    if default_id:
        r = await db.execute(select(Account).where(Account.id == default_id))
        default_account = r.scalars().first()
        if default_account:
            ac.activate_account_config(default_account.config_dir)
            await _set_setting("service_enabled", "false", db)
            logger.info("Service disabled — restored default account: %s", default_account.email)
            return {"ok": True, "restored_email": default_account.email}

    # No default account — use original backup
    backup_raw = await _get_setting("original_credentials_backup", "{}", db)
    try:
        backup = json.loads(backup_raw)
    except Exception:
        backup = {}

    if backup:
        ac.restore_config_from_backup(backup)
        await _set_setting("service_enabled", "false", db)
        logger.info("Service disabled — original credentials restored")
        return {"ok": True, "restored_email": ac.get_active_email()}

    await _set_setting("service_enabled", "false", db)
    return {"ok": True, "restored_email": None}


# ── Default account ────────────────────────────────────────────────────────────

@router.patch("/default-account")
async def set_default_account(account_id: int, db: AsyncSession = Depends(get_db)):
    """Set which account's credentials are restored when the service is disabled."""
    r = await db.execute(select(Account).where(Account.id == account_id))
    account = r.scalars().first()
    if not account:
        raise HTTPException(404, "Account not found")

    await _set_setting("default_account_id", str(account_id), db)
    return {"ok": True, "default_account_id": account_id, "email": account.email}
