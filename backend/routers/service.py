"""
Service toggle: enable / disable the multi-account manager.

ON  — service_enabled flag is set; credential switching is handled separately.
OFF — service_enabled flag is cleared; credentials are left untouched.
"""

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
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
        active_email=await asyncio.to_thread(ac.get_active_email),
        default_account_id=default_id,
    )


# ── Enable ─────────────────────────────────────────────────────────────────────

@router.post("/enable")
async def enable_service(db: AsyncSession = Depends(get_db)):
    """Enable the service. Always activates the default or first enabled account."""
    # Idempotency: if already enabled, no-op
    already_enabled = await ss.get_bool("service_enabled", False, db)
    if already_enabled:
        return {"ok": True, "active_email": await asyncio.to_thread(ac.get_active_email)}

    enabled_accounts = await ac.get_enabled_accounts(db)

    if not enabled_accounts:
        raise HTTPException(400, "No enabled accounts available")

    await ss.set_setting("service_enabled", "true", db)

    # Determine which account to activate (default or first)
    default_raw = await ss.get_setting("default_account_id", "", db)
    default_id = int(default_raw) if default_raw.isdigit() else None
    target = None
    if default_id is not None:
        target = next((a for a in enabled_accounts if a.id == default_id), None)
    if not target:
        target = enabled_accounts[0]

    # Backup current credentials before switching.
    # backup_active_config() returns {} when ~/.claude/.claude.json does not
    # exist (no pre-existing credentials — nothing to restore, which is fine)
    # OR when the file cannot be read (permission error, etc.).  In the latter
    # case restore_config_from_backup() will silently no-op, losing the ability
    # to roll back on disable.  Log a warning so the risk is visible.
    backup = await asyncio.to_thread(ac.backup_active_config)
    if not backup:
        logger.warning(
            "backup_active_config() returned empty — either no active credentials "
            "exist yet (benign) or ~/.claude/.claude.json could not be read "
            "(disable will not restore original credentials)."
        )
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
            await asyncio.to_thread(ac.restore_config_from_backup, backup)
        except Exception as e:
            logger.warning("Failed to restore credentials backup: %s", e)

    logger.info("Service disabled")
    return {"ok": True}


# ── Default account ────────────────────────────────────────────────────────────

@router.patch("/default-account")
async def set_default_account(
    account_id: int = Query(..., ge=1),
    db: AsyncSession = Depends(get_db),
):
    """Set the starting account activated when the service is enabled."""
    account = await ac.get_account_by_id(account_id, db)
    if not account:
        raise HTTPException(404, "Account not found")

    await ss.set_setting("default_account_id", str(account_id), db)
    return {"ok": True, "default_account_id": account_id, "email": account.email}
