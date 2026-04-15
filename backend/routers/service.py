"""
Service toggle: enable / disable auto-switching.

ON  — service_enabled flag is set; auto-switch decisions in the poll
       loop will swap credentials when thresholds are crossed.
OFF — service_enabled flag is cleared; credentials are left untouched
       and the poll loop still renders rate-limit bars but never swaps.

The toggle does NOT back up or restore credentials — there is nothing
to restore in the vault-swap architecture.  Toggling ON simply sets
the flag; if no active account exists yet (fresh install, no previous
swap), the first enabled account is activated.  Toggling OFF clears
the flag.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..schemas import ServiceStatus
from ..services import account_queries as aq
from ..services import account_service as ac
from ..services import settings_service as ss
from ..services import switcher as sw
from ..ws import ws_manager


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/service", tags=["service"])


@router.get("", response_model=ServiceStatus)
async def get_service_status(db: AsyncSession = Depends(get_db)):
    return ServiceStatus(
        enabled=await ss.get_bool("service_enabled", False, db),
        active_email=await ac.get_active_email_async(),
        default_account_id=await ss.get_int_or_none("default_account_id", db),
    )


@router.post("/enable")
async def enable_service(db: AsyncSession = Depends(get_db)):
    """Enable auto-switching.

    Preserves the current active account when possible: if
    ``~/.claude/.claude.json`` already names an enabled account, no
    swap is performed.  Only when there is no valid active account —
    cold install, or the user deleted the previously-active account —
    does this endpoint activate the default (or first enabled) account.
    """
    if await ss.get_bool("service_enabled", False, db):
        return {"ok": True, "active_email": await ac.get_active_email_async()}

    enabled_accounts = await aq.get_enabled_accounts(db)
    if not enabled_accounts:
        raise HTTPException(400, "No enabled accounts available")

    await ss.set_setting("service_enabled", "true", db)

    # If the current active email is one of the enabled accounts, no
    # swap needed — preserve the user's session.
    current_email = await ac.get_active_email_async()
    if current_email and any(a.email == current_email for a in enabled_accounts):
        logger.info(
            "Service enabled — preserving current active account: %s",
            current_email,
        )
        return {"ok": True, "active_email": current_email}

    # No valid active account — activate the default or first enabled.
    default_id = await ss.get_int_or_none("default_account_id", db)
    target = None
    if default_id is not None:
        target = next((a for a in enabled_accounts if a.id == default_id), None)
    if not target:
        target = enabled_accounts[0]

    await sw.perform_switch(target, "manual", db, ws_manager)
    logger.info("Service enabled — activated %s", target.email)
    return {"ok": True, "active_email": target.email}


@router.post("/disable")
async def disable_service(db: AsyncSession = Depends(get_db)):
    """Disable auto-switching.  Does NOT touch credentials."""
    await ss.set_setting("service_enabled", "false", db)
    logger.info("Service disabled")
    return {"ok": True}


@router.patch("/default-account")
async def set_default_account(
    account_id: int = Query(..., ge=1),
    db: AsyncSession = Depends(get_db),
):
    """Set the starting account activated on first enable."""
    account = await aq.get_account_by_id(account_id, db)
    if not account:
        raise HTTPException(404, "Account not found")
    await ss.set_setting("default_account_id", str(account_id), db)
    return {"ok": True, "default_account_id": account_id, "email": account.email}
