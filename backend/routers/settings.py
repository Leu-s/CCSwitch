from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from ..database import get_db
from ..models import Setting
from ..schemas import SettingOut, SettingUpdate

router = APIRouter(prefix="/api/settings", tags=["settings"])

DEFAULTS = {
    "auto_switch_enabled": "true",
    "switch_threshold_percent": "90",
    "usage_poll_interval_seconds": "60",
}

async def ensure_defaults(db: AsyncSession):
    for key, value in DEFAULTS.items():
        result = await db.execute(select(Setting).where(Setting.key == key))
        if not result.scalars().first():
            db.add(Setting(key=key, value=value))
    await db.commit()

@router.get("", response_model=list[SettingOut])
async def get_settings(db: AsyncSession = Depends(get_db)):
    await ensure_defaults(db)
    result = await db.execute(select(Setting))
    return result.scalars().all()

@router.patch("/{key}", response_model=SettingOut)
async def update_setting(key: str, payload: SettingUpdate, db: AsyncSession = Depends(get_db)):
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
