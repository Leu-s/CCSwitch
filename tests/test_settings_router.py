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

    async def override_get_db():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
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
    assert "switch_threshold_percent" in keys
    assert "usage_poll_interval_seconds" in keys

def test_patch_setting(client):
    resp = client.patch("/api/settings/switch_threshold_percent", json={"value": "80"})
    assert resp.status_code == 200
    assert resp.json()["value"] == "80"

def test_patch_custom_key(client):
    resp = client.patch("/api/settings/custom_key", json={"value": "hello"})
    assert resp.status_code == 200
    assert resp.json()["key"] == "custom_key"
