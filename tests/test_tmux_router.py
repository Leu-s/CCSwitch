import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from unittest.mock import patch

TEST_DB_URL = "sqlite+aiosqlite:///./test_tmux.db"

@pytest.fixture(scope="module")
def test_app():
    from backend.database import Base, get_db
    from backend.routers.tmux import router

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

def test_list_sessions(client):
    panes = [{"target": "main:0.0", "command": "claude"}]
    with patch("backend.services.tmux_service.list_panes", return_value=panes):
        resp = client.get("/api/tmux/sessions")
    assert resp.status_code == 200
    assert resp.json()[0]["target"] == "main:0.0"

def test_create_monitor(client):
    payload = {"name": "test", "pattern_type": "manual", "pattern": "main:0.0", "enabled": True}
    resp = client.post("/api/tmux/monitors", json=payload)
    assert resp.status_code == 201
    assert resp.json()["name"] == "test"

def test_list_monitors(client):
    resp = client.get("/api/tmux/monitors")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
    assert len(resp.json()) >= 1

def test_delete_monitor(client):
    # Create one to delete
    payload = {"name": "to-delete", "pattern_type": "manual", "pattern": "x:0.0", "enabled": True}
    create_resp = client.post("/api/tmux/monitors", json=payload)
    mid = create_resp.json()["id"]
    del_resp = client.delete(f"/api/tmux/monitors/{mid}")
    assert del_resp.status_code == 204
