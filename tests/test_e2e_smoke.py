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
    from backend.routers import accounts, settings

    engine = create_async_engine(TEST_DB_URL, echo=False)
    TestSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def init_db():
        from backend.services.settings_service import ensure_defaults
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        async with TestSessionLocal() as session:
            await ensure_defaults(session)

    asyncio.run(init_db())

    async def override_get_db():
        async with TestSessionLocal() as session:
            yield session

    app = FastAPI()
    app.include_router(accounts.router)
    app.include_router(settings.router)
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
        "config_dir": "/tmp/fake-account-abc12345",
        "instructions": "Authenticate in the terminal below. After login completes, click 'Verify & Save'.",
    }
    with patch("backend.services.login_session_service.start_login_session", return_value=mock_info):
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
    with patch("backend.services.login_session_service.verify_login_session", return_value=mock_result):
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
    with patch("backend.services.login_session_service.verify_login_session", return_value=mock_result):
        resp = client.post("/api/accounts/verify-login?session_id=abc12345")
    assert resp.status_code == 200
    assert resp.json()["success"] is True


def test_cancel_login(client):
    """DELETE /api/accounts/cancel-login should succeed."""
    with patch("backend.services.login_session_service.cleanup_login_session"):
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
    assert "usage_poll_interval_seconds" in keys
    assert "auto_switch_enabled" not in keys


def test_settings_patch(client):
    resp = client.patch("/api/settings/tmux_nudge_message", json={"value": "go"})
    assert resp.status_code == 200
    assert resp.json()["value"] == "go"


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
        "enabled": False,
    })
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False


def test_accounts_manual_switch(client):
    with patch("backend.services.account_service.get_active_email", return_value=None):
        list_resp = client.get("/api/accounts")
    account_id = list_resp.json()[0]["id"]

    with patch("backend.services.switcher.perform_switch", return_value=None):
        resp = client.post(f"/api/accounts/{account_id}/switch")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
