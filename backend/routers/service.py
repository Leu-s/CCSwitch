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
    """Enable the service by setting the service_enabled flag."""
    await _set_setting("service_enabled", "true", db)
    active_email = ac.get_active_email()
    logger.info("Service enabled — current active email: %s", active_email)
    return {"ok": True, "active_email": active_email}


# ── Disable ────────────────────────────────────────────────────────────────────

@router.post("/disable")
async def disable_service(db: AsyncSession = Depends(get_db)):
    """Disable the service by clearing the service_enabled flag."""
    await _set_setting("service_enabled", "false", db)
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

    await _set_setting("default_account_id", str(account_id), db)
    return {"ok": True, "default_account_id": account_id, "email": account.email}
