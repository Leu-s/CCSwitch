"""
Tests for backend.services.settings_service.ensure_defaults().

Uses a file-backed SQLite DB (dropped/recreated per test) so the async engine
and all coroutines share the same event loop (via asyncio.run()).
"""
import asyncio
import pytest
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import select

TEST_DB_URL = "sqlite+aiosqlite:///./test_settings_service.db"


def _make_engine_and_factory():
    engine = create_async_engine(TEST_DB_URL, echo=False)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine, factory


# ── ensure_defaults ────────────────────────────────────────────────────────────

def test_ensure_defaults_creates_missing_rows():
    """Starting from an empty DB, ensure_defaults seeds all expected rows."""
    from backend.database import Base
    from backend.services.settings_service import ensure_defaults, SETTING_DEFAULTS
    from backend.models import Setting

    async def run():
        engine, factory = _make_engine_and_factory()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        async with factory() as db:
            await ensure_defaults(db)

        async with factory() as db:
            result = await db.execute(select(Setting))
            rows = {s.key: s.value for s in result.scalars().all()}

        await engine.dispose()
        return rows

    rows = asyncio.run(run())

    for key, expected_value in SETTING_DEFAULTS.items():
        assert key in rows, f"Missing setting row: {key}"
        assert rows[key] == expected_value, (
            f"Setting '{key}' has value {rows[key]!r}, expected {expected_value!r}"
        )


def test_ensure_defaults_does_not_overwrite_existing():
    """Pre-existing setting rows are left untouched by ensure_defaults."""
    from backend.database import Base
    from backend.services.settings_service import ensure_defaults
    from backend.models import Setting

    pre_value = "false"

    async def run():
        engine, factory = _make_engine_and_factory()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        # Pre-create the row with a non-default value
        async with factory() as db:
            db.add(Setting(key="auto_switch_enabled", value=pre_value))
            await db.commit()

        # ensure_defaults must not overwrite the customised value
        async with factory() as db:
            await ensure_defaults(db)

        async with factory() as db:
            result = await db.execute(
                select(Setting).where(Setting.key == "auto_switch_enabled")
            )
            row = result.scalars().first()
            value = row.value if row else None

        await engine.dispose()
        return value

    value = asyncio.run(run())

    assert value == pre_value, (
        f"ensure_defaults overwrote existing value; got {value!r}, want {pre_value!r}"
    )
