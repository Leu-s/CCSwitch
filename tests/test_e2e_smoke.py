"""
End-to-end smoke tests using an in-process FastAPI + SQLite database.

Changes from old schema:
- POST /api/accounts (create) is gone — accounts are added via login flow.
- POST /api/accounts/scan is gone.
- GET /api/accounts/detect-email is gone.
- POST /api/accounts/{id}/verify is gone.
- New endpoints: POST /api/accounts/start-login, POST /api/accounts/verify-login,
  DELETE /api/accounts/cancel-login.
- active_email is resolved via backend.services.account_service.get_active_email,
  not backend.services.keychain.get_active_email.
- Account rows now have config_dir / threshold_pct, no keychain_suffix.
"""
import asyncio
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from unittest.mock import patch

TEST_DB_URL = "sqlite+aiosqlite:///./test_e2e.db"


@pytest.fixture(scope="module")
def test_app():
    from backend.database import Base, get_db
    from backend.routers import accounts, settings, tmux

    engine = create_async_engine(TEST_DB_URL, echo=False)
    TestSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

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


# ── Basic health ───────────────────────────────────────────────────────────────

def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


# ── Accounts list (empty DB) ───────────────────────────────────────────────────

def test_accounts_list_empty(client):
    with patch("backend.services.account_service.get_active_email", return_value=None):
        resp = client.get("/api/accounts")
    assert resp.status_code == 200
    assert resp.json() == []


# ── Login flow (mocked) ────────────────────────────────────────────────────────

def test_start_login(client):
    """POST /api/accounts/start-login returns session metadata."""
    mock_info = {
        "session_id": "abc12345",
        "pane_target": "claude-multi:1.0",
        "config_dir": "/tmp/fake-account-abc12345",
        "instructions": "Authenticate in the terminal below. After login completes, click 'Verify & Save'.",
    }
    with patch("backend.services.account_service.start_login_session", return_value=mock_info):
        resp = client.post("/api/accounts/start-login")
    assert resp.status_code == 200
    data = resp.json()
    assert data["session_id"] == "abc12345"
    assert "config_dir" in data


def test_verify_login_saves_account(client):
    """POST /api/accounts/verify-login with a successful mock creates a DB row."""
    mock_result = {
        "success": True,
        "email": "smoke@test.com",
        "config_dir": "/tmp/fake-account-abc12345",
    }
    with patch("backend.services.account_service.verify_login_session", return_value=mock_result):
        resp = client.post("/api/accounts/verify-login?session_id=abc12345")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["email"] == "smoke@test.com"


def test_accounts_list_with_item(client):
    """After verify-login, the account should appear in the list."""
    with patch("backend.services.account_service.get_active_email", return_value=None):
        resp = client.get("/api/accounts")
    assert resp.status_code == 200
    accounts = resp.json()
    assert len(accounts) >= 1
    emails = [a["email"] for a in accounts]
    assert "smoke@test.com" in emails


def test_verify_login_duplicate_returns_success(client):
    """Verifying the same login session again should succeed (idempotent)."""
    mock_result = {
        "success": True,
        "email": "smoke@test.com",
        "config_dir": "/tmp/fake-account-abc12345",
    }
    with patch("backend.services.account_service.verify_login_session", return_value=mock_result):
        resp = client.post("/api/accounts/verify-login?session_id=abc12345")
    assert resp.status_code == 200
    assert resp.json()["success"] is True


def test_cancel_login(client):
    """DELETE /api/accounts/cancel-login should succeed."""
    with patch("backend.services.account_service.cleanup_login_session"):
        resp = client.delete("/api/accounts/cancel-login?session_id=doesnotmatter")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


# ── Old removed endpoints return 404 / 405 ────────────────────────────────────

def test_accounts_create_endpoint_gone(client):
    """POST /api/accounts (old create endpoint) no longer exists."""
    resp = client.post("/api/accounts", json={"email": "x@x.com", "keychain_suffix": "abc"})
    assert resp.status_code in (404, 405)


def test_accounts_scan_endpoint_gone(client):
    """POST /api/accounts/scan no longer exists."""
    resp = client.post("/api/accounts/scan")
    assert resp.status_code in (404, 405)


# ── Settings ───────────────────────────────────────────────────────────────────

def test_settings_list(client):
    resp = client.get("/api/settings")
    assert resp.status_code == 200
    keys = {s["key"] for s in resp.json()}
    assert "auto_switch_enabled" in keys


def test_settings_patch(client):
    resp = client.patch("/api/settings/auto_switch_enabled", json={"value": "false"})
    assert resp.status_code == 200
    assert resp.json()["value"] == "false"


# ── Tmux ───────────────────────────────────────────────────────────────────────

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
        "enabled": True,
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
    assert any(m["name"] == "smoke-test-monitor" for m in monitors)


def test_tmux_monitors_delete(client):
    create_resp = client.post("/api/tmux/monitors", json={
        "name": "smoke-delete-test",
        "pattern_type": "manual",
        "pattern": "x:0.0",
        "enabled": True,
    })
    mid = create_resp.json()["id"]
    del_resp = client.delete(f"/api/tmux/monitors/{mid}")
    assert del_resp.status_code == 204


# ── Switch log ─────────────────────────────────────────────────────────────────

def test_accounts_switch_log(client):
    resp = client.get("/api/accounts/log")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# ── Per-account CRUD ───────────────────────────────────────────────────────────

def test_accounts_update(client):
    with patch("backend.services.account_service.get_active_email", return_value=None):
        list_resp = client.get("/api/accounts")
    account_id = list_resp.json()[0]["id"]

    resp = client.patch(f"/api/accounts/{account_id}", json={
        "display_name": "Smoke Test Account",
        "enabled": False,
    })
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "Smoke Test Account"
    assert resp.json()["enabled"] is False


def test_accounts_manual_switch(client):
    with patch("backend.services.account_service.get_active_email", return_value=None):
        list_resp = client.get("/api/accounts")
    account_id = list_resp.json()[0]["id"]

    with patch("backend.services.switcher.perform_switch", return_value=None):
        resp = client.post(f"/api/accounts/{account_id}/switch")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
