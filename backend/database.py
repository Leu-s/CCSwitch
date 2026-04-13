import logging
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
    Create all tables.  If the schema is from the old design (accounts table
    has a keychain_suffix column) drop everything and start fresh.
    """
    async with engine.begin() as conn:
        try:
            result = await conn.execute(text("PRAGMA table_info(accounts)"))
            columns = [row[1] for row in result.fetchall()]
            if columns and "keychain_suffix" in columns:
                logger.info("Old schema detected — dropping all tables for a clean start")
                from .models import Base as ModelBase  # noqa: F401
                await conn.run_sync(ModelBase.metadata.drop_all)
        except Exception:
            pass

        from .models import Base as ModelBase  # noqa: F401
        await conn.run_sync(ModelBase.metadata.create_all)
        logger.info("Database tables ready")

        # Migrate: bump usage_poll_interval_seconds from the old 60-second default
        # to 300, because the Anthropic usage API rate-limits at that frequency.
        await conn.execute(
            text(
                "UPDATE settings SET value = '300' "
                "WHERE key = 'usage_poll_interval_seconds' AND CAST(value AS INTEGER) < 120"
            )
        )
