"""
Tests for backend.database.init_db().

init_db() runs Alembic migrations on the configured database. It must be:
  1. Safe to run multiple times on the same DB (idempotent).
  2. Able to handle pre-Alembic databases (schema already present, but no
     ``alembic_version`` table) by stamping head without re-running migrations
     (no data loss).

Uses a file-backed SQLite DB (per-test tmp_path) because Alembic needs real
connections and running the check against a shared module-level engine.
Runs the async init_db() via asyncio.run() — same pattern as
test_settings_service.py.
"""
import asyncio

import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker


def _patch_db_url(monkeypatch, url: str):
    """Point backend.config.settings + backend.database.engine at ``url``.

    init_db() reads ``settings.database_url`` directly for the Alembic
    bootstrap, then uses the module-level ``engine`` for the trailing
    data-only settings fixup. Both need to match to keep the test isolated
    from the repo-root default DB.
    """
    import backend.config as cfg
    import backend.database as database

    monkeypatch.setattr(cfg.settings, "database_url", url)

    new_engine = create_async_engine(
        url, echo=False, connect_args={"check_same_thread": False}
    )
    new_factory = async_sessionmaker(
        new_engine, class_=AsyncSession, expire_on_commit=False
    )
    monkeypatch.setattr(database, "engine", new_engine)
    monkeypatch.setattr(database, "AsyncSessionLocal", new_factory)
    return new_engine


def _expected_tables() -> set[str]:
    """All app tables + alembic_version after a successful migration."""
    return {"accounts", "tmux_monitors", "switch_log", "settings", "alembic_version"}


def _inspect_tables(sync_url: str) -> set[str]:
    """Return the set of table names in the given SQLite DB using sync engine."""
    from sqlalchemy import create_engine

    engine = create_engine(sync_url)
    try:
        with engine.connect() as conn:
            return set(inspect(conn).get_table_names())
    finally:
        engine.dispose()


def test_init_db_creates_schema_on_fresh_db(tmp_path, monkeypatch):
    """A brand-new DB file gets the full schema + alembic_version stamped."""
    from backend.database import init_db

    db_file = tmp_path / "fresh.db"
    url = f"sqlite+aiosqlite:///{db_file}"
    sync_url = f"sqlite:///{db_file}"

    new_engine = _patch_db_url(monkeypatch, url)

    try:
        asyncio.run(init_db())

        tables = _inspect_tables(sync_url)
        for expected in _expected_tables():
            assert expected in tables, f"missing table after init_db: {expected}"

        # alembic_version must have a tracked revision (not an empty table)
        from sqlalchemy import create_engine, text as _text

        sync_engine = create_engine(sync_url)
        try:
            with sync_engine.connect() as conn:
                rows = conn.execute(_text("SELECT version_num FROM alembic_version")).fetchall()
                assert len(rows) == 1, f"expected 1 alembic_version row, got {len(rows)}"
                assert rows[0][0], "alembic_version row has empty version_num"
        finally:
            sync_engine.dispose()
    finally:
        asyncio.run(new_engine.dispose())


def test_init_db_is_idempotent(tmp_path, monkeypatch):
    """Calling init_db() twice on the same DB must not error or lose data."""
    from backend.database import init_db
    from backend.models import Account

    db_file = tmp_path / "idempotent.db"
    url = f"sqlite+aiosqlite:///{db_file}"
    sync_url = f"sqlite:///{db_file}"

    new_engine = _patch_db_url(monkeypatch, url)

    try:
        # First call — fresh install
        asyncio.run(init_db())
        first_tables = _inspect_tables(sync_url)

        # Insert a test row so we can detect data loss on the second call
        async def _insert_account():
            import backend.database as database

            async with database.AsyncSessionLocal() as db:
                db.add(
                    Account(
                        email="probe@example.com",
                        config_dir="/tmp/probe",
                        threshold_pct=95.0,
                        enabled=True,
                        priority=0,
                    )
                )
                await db.commit()

        asyncio.run(_insert_account())

        # Second call — must be a no-op, no errors, no data loss
        asyncio.run(init_db())
        second_tables = _inspect_tables(sync_url)

        assert second_tables == first_tables, (
            f"schema changed on second init_db call: "
            f"+{second_tables - first_tables} -{first_tables - second_tables}"
        )

        # Verify the seeded row survived
        async def _count_accounts():
            import backend.database as database
            from sqlalchemy import select

            async with database.AsyncSessionLocal() as db:
                result = await db.execute(select(Account))
                return len(result.scalars().all())

        count = asyncio.run(_count_accounts())
        assert count == 1, f"expected 1 account to survive second init_db, got {count}"
    finally:
        asyncio.run(new_engine.dispose())


def test_init_db_stamps_head_on_pre_alembic_db(tmp_path, monkeypatch):
    """
    Simulate a pre-Alembic database: schema tables exist but
    ``alembic_version`` does not. init_db() must detect this and stamp head
    (not re-run migrations), preserving existing data.
    """
    from sqlalchemy import create_engine, text as _text

    from backend.database import init_db
    from backend.models import Account

    db_file = tmp_path / "pre_alembic.db"
    url = f"sqlite+aiosqlite:///{db_file}"
    sync_url = f"sqlite:///{db_file}"

    new_engine = _patch_db_url(monkeypatch, url)

    try:
        # First: let init_db() build the schema normally
        asyncio.run(init_db())

        # Seed a row so we can detect data loss
        async def _insert_account():
            import backend.database as database

            async with database.AsyncSessionLocal() as db:
                db.add(
                    Account(
                        email="pre-alembic@example.com",
                        config_dir="/tmp/pre-alembic",
                        threshold_pct=95.0,
                        enabled=True,
                        priority=0,
                    )
                )
                await db.commit()

        asyncio.run(_insert_account())

        # Manually drop alembic_version to simulate a pre-Alembic database
        sync_engine = create_engine(sync_url)
        try:
            with sync_engine.begin() as conn:
                conn.execute(_text("DROP TABLE alembic_version"))
        finally:
            sync_engine.dispose()

        tables_before = _inspect_tables(sync_url)
        assert "alembic_version" not in tables_before, (
            "sanity check: alembic_version should be dropped"
        )
        assert "accounts" in tables_before, "sanity check: accounts table must still exist"

        # Now call init_db() again — should stamp head, not re-run migrations
        asyncio.run(init_db())

        tables_after = _inspect_tables(sync_url)
        assert "alembic_version" in tables_after, (
            "init_db should have re-created alembic_version"
        )

        # Verify the seeded row survived (proves migrations did NOT re-run —
        # if they had, drop_column('display_name') would have been re-applied
        # to a fresh create_table, but it would have tried to alter an already-
        # migrated schema and could have wiped or corrupted rows).
        async def _count_accounts():
            import backend.database as database
            from sqlalchemy import select

            async with database.AsyncSessionLocal() as db:
                result = await db.execute(select(Account))
                rows = result.scalars().all()
                return [(r.email, r.config_dir) for r in rows]

        rows = asyncio.run(_count_accounts())
        assert rows == [("pre-alembic@example.com", "/tmp/pre-alembic")], (
            f"data lost after stamp-head path: {rows}"
        )

        # The alembic_version table must have a tracked head revision
        sync_engine = create_engine(sync_url)
        try:
            with sync_engine.connect() as conn:
                version_rows = conn.execute(
                    _text("SELECT version_num FROM alembic_version")
                ).fetchall()
                assert len(version_rows) == 1, (
                    f"expected 1 alembic_version row, got {len(version_rows)}"
                )
                assert version_rows[0][0], "alembic_version has empty version_num"
        finally:
            sync_engine.dispose()
    finally:
        asyncio.run(new_engine.dispose())
