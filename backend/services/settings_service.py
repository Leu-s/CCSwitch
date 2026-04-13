"""
Typed helpers for reading and writing application settings from the database.
Eliminates repeated json.loads() + select(Setting) patterns across routers and background tasks.
"""
import json
import logging
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Setting

logger = logging.getLogger(__name__)


async def get_setting(key: str, default: str, db: AsyncSession) -> str:
    """Return raw string value for a setting key, or default if not found."""
    row = await db.execute(select(Setting).where(Setting.key == key))
    s = row.scalars().first()
    return s.value if s else default


async def set_setting(key: str, value: str, db: AsyncSession) -> None:
    """Upsert a setting value."""
    row = await db.execute(select(Setting).where(Setting.key == key))
    s = row.scalars().first()
    if s:
        s.value = value
    else:
        db.add(Setting(key=key, value=value))
    await db.commit()


async def get_bool(key: str, default: bool, db: AsyncSession) -> bool:
    """Get a boolean setting stored as JSON 'true'/'false'."""
    raw = await get_setting(key, "true" if default else "false", db)
    try:
        return bool(json.loads(raw))
    except (json.JSONDecodeError, TypeError, ValueError):
        return default


async def get_int(key: str, default: int, db: AsyncSession) -> int:
    """Get an integer setting."""
    raw = await get_setting(key, str(default), db)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default
