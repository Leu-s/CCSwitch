"""
Service toggle: enable / disable the multi-account manager.

ON  — service_enabled flag is set; credential switching is handled separately.
OFF — service_enabled flag is cleared; credentials are left untouched.
"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models import Account
from ..schemas import ServiceStatus
from ..services import account_service as ac
from ..services import settings_service as ss
from ..services import switcher as sw
from ..ws import ws_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/service", tags=["service"])


# ── Status ─────────────────────────────────────────────────────────────────────

@router.get("", response_model=ServiceStatus)
async def get_service_status(db: AsyncSession = Depends(get_db)):
    enabled = await ss.get_bool("service_enabled", False, db)

    default_raw = await ss.get_setting("default_account_id", "", db)
    default_id = int(default_raw) if default_raw.isdigit() else None

    return ServiceStatus(
        enabled=bool(enabled),
        active_email=ac.get_active_email(),
        default_account_id=default_id,
    )


# ── Enable ─────────────────────────────────────────────────────────────────────

@router.post("/enable")
async def enable_service(db: AsyncSession = Depends(get_db)):
    """Enable the service. Always activates the default or first enabled account."""
    result = await db.execute(
        select(Account).where(Account.enabled == True).order_by(Account.priority.asc(), Account.id.asc())
    )
    enabled_accounts = result.scalars().all()

    if not enabled_accounts:
        raise HTTPException(400, "No enabled accounts available")

    await ss.set_setting("service_enabled", "true", db)

    # Determine which account to activate (default or first)
    default_raw = await ss.get_setting("default_account_id", "", db)
    default_id = int(default_raw) if default_raw.isdigit() else None
    target = None
    if default_id:
        target = next((a for a in enabled_accounts if a.id == default_id), None)
    if not target:
        target = enabled_accounts[0]

    # Backup current credentials before switching
    backup = ac.backup_active_config()
    await ss.set_setting("original_credentials_backup", json.dumps(backup), db)

    # Activate the target account
    await sw.perform_switch(target, "manual", db, ws_manager)
    active_email = target.email

    logger.info("Service enabled — current active email: %s", active_email)
    return {"ok": True, "active_email": active_email}


# ── Disable ────────────────────────────────────────────────────────────────────

@router.post("/disable")
async def disable_service(db: AsyncSession = Depends(get_db)):
    """Disable the service. Restore original credentials if backup exists."""
    await ss.set_setting("service_enabled", "false", db)

    # Restore original credentials from backup if it exists
    backup_raw = await ss.get_setting("original_credentials_backup", "", db)
    if backup_raw:
        try:
            backup = json.loads(backup_raw)
            ac.restore_config_from_backup(backup)
        except Exception as e:
            logger.warning("Failed to restore credentials backup: %s", e)

    logger.info("Service disabled")
    return {"ok": True}


# ── Default account ────────────────────────────────────────────────────────────

@router.patch("/default-account")
async def set_default_account(account_id: int, db: AsyncSession = Depends(get_db)):
    """Set the starting account activated when the service is enabled."""
    r = await db.execute(select(Account).where(Account.id == account_id))
    account = r.scalars().first()
    if not account:
        raise HTTPException(404, "Account not found")

    await ss.set_setting("default_account_id", str(account_id), db)
    return {"ok": True, "default_account_id": account_id, "email": account.email}
