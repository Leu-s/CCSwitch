import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from unittest.mock import patch
import asyncio

TEST_DB_URL = "sqlite+aiosqlite:///./test_e2e.db"

@pytest.fixture(scope="module")
def test_app():
    from backend.database import Base, get_db
    from backend.routers import accounts, settings, tmux

    engine = create_async_engine(TEST_DB_URL, echo=False)
    TestSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # Initialize DB once at module scope
    async def init_db():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(init_db())

    async def override_get_db():
        async with TestSessionLocal() as session:
            yield session

    app = FastAPI()
    app.include_router(accounts.router)
    app.include_router(settings.router)
    app.include_router(tmux.router)
    app.dependency_overrides[get_db] = override_get_db

    @app.get("/health")
    async def health():
        return {"ok": True}

    return app

@pytest.fixture(scope="module")
def client(test_app):
    return TestClient(test_app)

def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

def test_accounts_list_empty(client):
    with patch("backend.services.keychain.get_active_email", return_value=None):
        resp = client.get("/api/accounts")
    assert resp.status_code == 200
    assert resp.json() == []

def test_accounts_create(client):
    payload = {"email": "smoke@test.com", "keychain_suffix": "smoke001"}
    resp = client.post("/api/accounts", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["email"] == "smoke@test.com"
    assert data["keychain_suffix"] == "smoke001"
    assert data["enabled"] is True

def test_accounts_list_with_item(client):
    with patch("backend.services.keychain.get_active_email", return_value=None):
        resp = client.get("/api/accounts")
    assert resp.status_code == 200
    accounts = resp.json()
    assert len(accounts) >= 1
    assert accounts[0]["email"] == "smoke@test.com"

def test_settings_list(client):
    resp = client.get("/api/settings")
    assert resp.status_code == 200
    keys = {s["key"] for s in resp.json()}
    assert "auto_switch_enabled" in keys
    assert "switch_threshold_percent" in keys
    assert "usage_poll_interval_seconds" in keys

def test_settings_patch(client):
    resp = client.patch("/api/settings/auto_switch_enabled", json={"value": "false"})
    assert resp.status_code == 200
    assert resp.json()["value"] == "false"

def test_tmux_sessions_empty(client):
    with patch("backend.services.tmux_service.list_panes", return_value=[]):
        resp = client.get("/api/tmux/sessions")
    assert resp.status_code == 200
    assert resp.json() == []

def test_tmux_monitors_create(client):
    resp = client.post("/api/tmux/monitors", json={
        "name": "smoke-test-monitor",
        "pattern_type": "manual",
        "pattern": "main:0.0",
        "enabled": True
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "smoke-test-monitor"
    assert data["pattern"] == "main:0.0"
    assert data["enabled"] is True

def test_tmux_monitors_list(client):
    resp = client.get("/api/tmux/monitors")
    assert resp.status_code == 200
    monitors = resp.json()
    assert isinstance(monitors, list)
    assert len(monitors) >= 1
    assert any(m["name"] == "smoke-test-monitor" for m in monitors)

def test_tmux_monitors_delete(client):
    # Create a monitor to delete
    create_resp = client.post("/api/tmux/monitors", json={
        "name": "smoke-delete-test",
        "pattern_type": "manual",
        "pattern": "x:0.0",
        "enabled": True
    })
    mid = create_resp.json()["id"]

    # Delete it
    del_resp = client.delete(f"/api/tmux/monitors/{mid}")
    assert del_resp.status_code == 204

def test_accounts_scan_keychain(client):
    with patch("backend.services.keychain.scan_keychain", return_value=["aaaabbbb", "ccccdddd"]):
        resp = client.post("/api/accounts/scan")
    assert resp.status_code == 200
    results = resp.json()
    assert len(results) >= 2
    suffixes = [r["suffix"] for r in results]
    assert "aaaabbbb" in suffixes
    assert "ccccdddd" in suffixes

def test_accounts_switch_log(client):
    resp = client.get("/api/accounts/log")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)

def test_accounts_update(client):
    with patch("backend.services.keychain.get_active_email", return_value=None):
        list_resp = client.get("/api/accounts")
    account_id = list_resp.json()[0]["id"]

    resp = client.patch(f"/api/accounts/{account_id}", json={
        "display_name": "Smoke Test Account",
        "enabled": False
    })
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "Smoke Test Account"
    assert resp.json()["enabled"] is False

def test_accounts_manual_switch(client):
    with patch("backend.services.keychain.get_active_email", return_value=None):
        list_resp = client.get("/api/accounts")
    account_id = list_resp.json()[0]["id"]

    with patch("backend.services.switcher.perform_switch", return_value=None):
        resp = client.post(f"/api/accounts/{account_id}/switch")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
