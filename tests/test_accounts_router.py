"""Accounts router tests — updated for the isolated-config-dir schema."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from unittest.mock import patch

TEST_DB_URL = "sqlite+aiosqlite:///./test_accounts.db"


@pytest.fixture(scope="module")
def test_app():
    from backend.database import Base, get_db
    from backend.routers.accounts import router

    engine = create_async_engine(TEST_DB_URL, echo=False)
    TestSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

    import asyncio
    asyncio.run(_init())

    async def override_get_db():
        async with TestSessionLocal() as session:
            yield session

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = override_get_db
    return app


@pytest.fixture(scope="module")
def client(test_app):
    return TestClient(test_app)


def test_list_accounts_empty(client):
    with patch("backend.services.account_service.get_active_email", return_value=None):
        resp = client.get("/api/accounts")
    assert resp.status_code == 200
    assert resp.json() == []


def test_switch_log_empty(client):
    resp = client.get("/api/accounts/log")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
