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

# Default values seeded into the DB on first startup so background tasks always
# have a row to read (avoids relying on in-code defaults only).
SETTING_DEFAULTS: dict[str, str] = {
    "service_enabled": "false",
    "auto_switch_enabled": "false",
    "usage_poll_interval_seconds": "300",
}


async def ensure_defaults(db: AsyncSession) -> None:
    """Upsert default setting rows that are missing from the DB."""
    result = await db.execute(
        select(Setting.key).where(Setting.key.in_(list(SETTING_DEFAULTS.keys())))
    )
    existing_keys = {row[0] for row in result.all()}
    missing = {k: v for k, v in SETTING_DEFAULTS.items() if k not in existing_keys}
    if missing:
        for key, value in missing.items():
            db.add(Setting(key=key, value=value))
        await db.commit()


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


async def get_int_or_none(key: str, db: AsyncSession) -> int | None:
    """Get an integer setting, returning None if missing or malformed.
    Use this for nullable foreign-key-style settings (e.g. default_account_id)."""
    raw = await get_setting(key, "", db)
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


async def get_json(key: str, default, db: AsyncSession):
    """Get a JSON-encoded setting. Returns `default` if missing or unparseable."""
    raw = await get_setting(key, "", db)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return default


async def set_json(key: str, value, db: AsyncSession) -> None:
    """Store any JSON-serializable value as a setting."""
    await set_setting(key, json.dumps(value), db)
