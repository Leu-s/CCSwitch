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

def test_list_accounts_empty(client):
    with patch("backend.services.keychain.get_active_email", return_value=None):
        resp = client.get("/api/accounts")
    assert resp.status_code == 200
    assert resp.json() == []

def test_create_account(client):
    payload = {"email": "test@x.com", "keychain_suffix": "abc123"}
    resp = client.post("/api/accounts", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["email"] == "test@x.com"
    assert data["keychain_suffix"] == "abc123"
    assert data["enabled"] is True
    return data["id"]

def test_create_duplicate_account_fails(client):
    payload = {"email": "test@x.com", "keychain_suffix": "abc123"}
    resp = client.post("/api/accounts", json=payload)
    assert resp.status_code == 400

def test_update_account(client):
    # First get account id
    with patch("backend.services.keychain.get_active_email", return_value=None):
        list_resp = client.get("/api/accounts")
    account_id = list_resp.json()[0]["id"]
    resp = client.patch(f"/api/accounts/{account_id}", json={"display_name": "My Account", "enabled": False})
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "My Account"
    assert resp.json()["enabled"] is False

def test_scan_accounts(client):
    with patch("backend.services.keychain.scan_keychain", return_value=["abc123", "def456"]):
        resp = client.post("/api/accounts/scan")
    assert resp.status_code == 200
    suffixes = [r["suffix"] for r in resp.json()]
    assert "abc123" in suffixes
    assert "def456" in suffixes
    # abc123 already imported
    abc = next(r for r in resp.json() if r["suffix"] == "abc123")
    assert abc["already_imported"] is True

def test_switch_log_empty(client):
    resp = client.get("/api/accounts/log")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)

def test_delete_account(client):
    with patch("backend.services.keychain.get_active_email", return_value=None):
        list_resp = client.get("/api/accounts")
    account_id = list_resp.json()[0]["id"]
    resp = client.delete(f"/api/accounts/{account_id}")
    assert resp.status_code == 204
