"""
Tests for backend.services.settings_service.ensure_defaults().

Uses a file-backed SQLite DB (dropped/recreated per test) so the async engine
and all coroutines share the same event loop (via asyncio.run()).
"""
import asyncio
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

    pre_value = "true"

    async def run():
        engine, factory = _make_engine_and_factory()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        # Pre-create the row with a non-default value
        async with factory() as db:
            db.add(Setting(key="service_enabled", value=pre_value))
            await db.commit()

        # ensure_defaults must not overwrite the customised value
        async with factory() as db:
            await ensure_defaults(db)

        async with factory() as db:
            result = await db.execute(
                select(Setting).where(Setting.key == "service_enabled")
            )
            row = result.scalars().first()
            value = row.value if row else None

        await engine.dispose()
        return value

    value = asyncio.run(run())

    assert value == pre_value, (
        f"ensure_defaults overwrote existing value; got {value!r}, want {pre_value!r}"
    )


# ── Typed getters: get_int_or_none / get_json / set_json ─────────────────────

def _run_helper_test(async_fn):
    """Helper: set up an isolated DB, run the async fn with a session, return result."""
    from backend.database import Base

    async def run():
        engine, factory = _make_engine_and_factory()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as db:
            result = await async_fn(db)
        await engine.dispose()
        return result

    return asyncio.run(run())


def test_get_int_or_none_returns_none_when_missing_or_empty():
    """No row or empty-string value → None, never raises."""
    from backend.services.settings_service import get_int_or_none, set_setting

    async def body(db):
        missing = await get_int_or_none("nope", db)
        await set_setting("blank", "", db)
        blank = await get_int_or_none("blank", db)
        return missing, blank

    missing, blank = _run_helper_test(body)
    assert missing is None
    assert blank is None


def test_get_int_or_none_parses_valid_and_rejects_malformed():
    """Valid integer string → int; malformed → None (not raise)."""
    from backend.services.settings_service import get_int_or_none, set_setting

    async def body(db):
        await set_setting("good", "42", db)
        await set_setting("bad", "forty-two", db)
        return (
            await get_int_or_none("good", db),
            await get_int_or_none("bad", db),
        )

    good, bad = _run_helper_test(body)
    assert good == 42
    assert bad is None


def test_get_json_set_json_roundtrip_and_defaults():
    """set_json + get_json preserves structure; missing/malformed → default."""
    from backend.services.settings_service import get_json, set_json, set_setting

    payload = {"k": "v", "n": 1, "arr": [1, 2, 3]}

    async def body(db):
        await set_json("payload", payload, db)
        roundtrip = await get_json("payload", None, db)

        # Missing key → default
        missing = await get_json("never_set", {"default": True}, db)

        # Malformed JSON → default (not raise)
        await set_setting("broken", "{not json", db)
        broken = await get_json("broken", "fallback", db)

        return roundtrip, missing, broken

    roundtrip, missing, broken = _run_helper_test(body)
    assert roundtrip == payload
    assert missing == {"default": True}
    assert broken == "fallback"
