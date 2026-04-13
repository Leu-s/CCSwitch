import asyncio
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

TEST_DB_URL = "sqlite+aiosqlite:///./test_settings.db"

@pytest.fixture(scope="module")
def test_app():
    from backend.database import Base, get_db
    from backend.routers.settings import router

    engine = create_async_engine(TEST_DB_URL, echo=False)
    TestSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

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

def test_get_settings_returns_defaults(client):
    resp = client.get("/api/settings")
    assert resp.status_code == 200
    data = resp.json()
    keys = {s["key"] for s in data}
    assert "auto_switch_enabled" in keys
    assert "usage_poll_interval_seconds" in keys
    # switch_threshold_percent removed — threshold is now per-account
    assert "switch_threshold_percent" not in keys

def test_patch_setting(client):
    resp = client.patch("/api/settings/auto_switch_enabled", json={"value": "false"})
    assert resp.status_code == 200
    assert resp.json()["value"] == "false"

def test_patch_custom_key_rejected(client):
    resp = client.patch("/api/settings/custom_key", json={"value": "hello"})
    assert resp.status_code == 403
