"""
Shared pytest fixtures.

Each test module hard-codes `sqlite+aiosqlite:///./test_*.db` so the SQLite
files would otherwise land at the repo root and pollute the working tree.
We chdir into a session-scoped tmp dir so they end up inside it instead and
are cleaned up automatically when the session ends.
"""
import asyncio
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from backend.database import Base, get_db


@pytest.fixture(scope="session", autouse=True)
def _isolate_test_dbs(tmp_path_factory):
    original_cwd = os.getcwd()
    tmp = tmp_path_factory.mktemp("test_dbs")
    os.chdir(tmp)
    try:
        yield
    finally:
        os.chdir(original_cwd)


@pytest.fixture(scope="module")
def make_test_app():
    """Factory: given a router, returns a (app, TestClient) pair backed by an
    isolated in-memory SQLite database. Each caller gets its own engine so
    test modules don't share DB state."""
    def _factory(router, *, db_name: str = "test"):
        url = f"sqlite+aiosqlite:///./test_{db_name}.db"
        engine = create_async_engine(url, echo=False)
        SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        async def _init():
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.drop_all)
                await conn.run_sync(Base.metadata.create_all)

        asyncio.run(_init())

        async def override_get_db():
            async with SessionLocal() as session:
                yield session

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = override_get_db
        return app, TestClient(app)
    return _factory
