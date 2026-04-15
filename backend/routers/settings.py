from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..database import get_db
from ..models import Setting
from ..schemas import SettingOut, SettingUpdate
from ..services.settings_service import ensure_defaults


router = APIRouter(prefix="/api/settings", tags=["settings"])


ALLOWED_KEYS = {
    "usage_poll_interval_seconds",
    "tmux_nudge_enabled",
    "tmux_nudge_message",
}


@router.get("", response_model=list[SettingOut])
async def get_settings(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Setting))
    return result.scalars().all()


@router.patch("/{key}", response_model=SettingOut)
async def update_setting(
    key: str, payload: SettingUpdate, db: AsyncSession = Depends(get_db)
):
    if key not in ALLOWED_KEYS:
        raise HTTPException(status_code=403, detail="Setting key not allowed")
    await ensure_defaults(db)
    result = await db.execute(select(Setting).where(Setting.key == key))
    setting = result.scalars().first()
    if not setting:
        setting = Setting(key=key, value=payload.value)
        db.add(setting)
    else:
        setting.value = payload.value
    await db.commit()
    await db.refresh(setting)
    return setting
