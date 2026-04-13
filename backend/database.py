import logging
import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import text
from .config import settings

logger = logging.getLogger(__name__)

engine = create_async_engine(
    settings.database_url,
    echo=False,
    connect_args={"check_same_thread": False},
)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


async def init_db() -> None:
    """
    Run all pending Alembic migrations.

    For fresh installs (no DB file yet) this creates the full schema.
    For existing installs that predate Alembic, we stamp the current head
    so that historical inline migrations are not re-run (the schema is
    already correct; only future migrations need applying).

    The old DROP-ALL-on-stale-schema guard has been removed — Alembic's
    migration chain handles schema evolution safely without data loss.
    """
    import asyncio
    from alembic.config import Config
    from alembic import command

    alembic_cfg = Config(
        os.path.join(os.path.dirname(__file__), "..", "alembic.ini")
    )
    # Point alembic at the same DB the app uses (overrides the ini value)
    alembic_cfg.set_main_option("sqlalchemy.url", settings.database_url)

    def _run_alembic(cfg: Config) -> None:
        """Run alembic in a thread — alembic is not async-native."""
        # Check whether the alembic_version table exists (i.e. this DB has
        # already been managed by Alembic before).
        from alembic.runtime.migration import MigrationContext
        from sqlalchemy import create_engine, inspect

        # Use the synchronous SQLite URL for the alembic bootstrap check
        sync_url = settings.database_url.replace("sqlite+aiosqlite", "sqlite")
        sync_engine = create_engine(sync_url)

        from sqlalchemy import text as _text

        with sync_engine.connect() as conn:
            inspector = inspect(conn)
            tables_set = set(inspector.get_table_names())
            has_existing_tables = bool(tables_set - {"alembic_version"})
            # Check for a recorded version number, not just the table existence.
            # An empty alembic_version table (e.g. from a previous crashed upgrade)
            # is treated the same as no table at all.
            has_tracked_version = False
            if "alembic_version" in tables_set:
                rows = conn.execute(
                    _text("SELECT version_num FROM alembic_version")
                ).fetchall()
                has_tracked_version = bool(rows)

        if not has_tracked_version and has_existing_tables:
            # Pre-Alembic install or empty version table: schema is already
            # up-to-date, just stamp it (no data loss).
            logger.info(
                "Existing pre-Alembic database detected — stamping head "
                "(no data loss, schema already current)"
            )
            command.stamp(cfg, "head")
        else:
            # Fresh install or already managed: apply all pending migrations.
            command.upgrade(cfg, "head")

        sync_engine.dispose()

    await asyncio.to_thread(_run_alembic, alembic_cfg)

    # Bump usage_poll_interval_seconds from the old 60-second default to 300 s
    # (safe data-only fixup that is idempotent and causes no schema change).
    async with engine.begin() as conn:
        try:
            await conn.execute(
                text(
                    "UPDATE settings SET value = '300' "
                    "WHERE key = 'usage_poll_interval_seconds' AND CAST(value AS INTEGER) < 120"
                )
            )
        except Exception as e:
            logger.debug("usage_poll_interval_seconds fixup skipped: %s", e)

    logger.info("Database ready")
