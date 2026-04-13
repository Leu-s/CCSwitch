"""Accounts router tests — updated for the isolated-config-dir schema."""
import asyncio
import pytest
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from unittest.mock import patch, AsyncMock

from backend.models import Account

TEST_DB_URL = "sqlite+aiosqlite:///./test_accounts.db"


@pytest.fixture(scope="module")
def client(make_test_app):
    from backend.routers.accounts import router
    _, c = make_test_app(router, db_name="accounts")
    return c


def test_list_accounts_empty(client):
    with patch("backend.services.account_service.get_active_email", return_value=None):
        resp = client.get("/api/accounts")
    assert resp.status_code == 200
    assert resp.json() == []


def test_switch_log_empty(client):
    resp = client.get("/api/accounts/log")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_delete_account(client):
    """Create an account via POST-equivalent DB insertion, delete it, verify removal."""
    # Insert an account directly so we can test deletion without the full login flow
    engine = create_async_engine(TEST_DB_URL, echo=False)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _insert():
        async with SessionLocal() as session:
            acc = Account(email="delete-test@example.com", config_dir="/tmp/delete-test", priority=99)
            session.add(acc)
            await session.commit()
            await session.refresh(acc)
            return acc.id

    account_id = asyncio.run(_insert())

    with patch("backend.services.account_service.get_active_email", return_value=None), \
         patch("backend.services.account_service.get_token_info", return_value={}), \
         patch("backend.ws.ws_manager.broadcast", new_callable=AsyncMock):
        delete_resp = client.delete(f"/api/accounts/{account_id}")
        assert delete_resp.status_code == 204

        list_resp = client.get("/api/accounts")
        assert list_resp.status_code == 200
        ids = [a["id"] for a in list_resp.json()]
        assert account_id not in ids


def test_update_account_disabled_when_not_active(client):
    """PATCH enabled=False on an account that is not currently active."""
    engine = create_async_engine(TEST_DB_URL, echo=False)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _insert():
        async with SessionLocal() as session:
            acc = Account(email="patch-test@example.com", config_dir="/tmp/patch-test", priority=50)
            session.add(acc)
            await session.commit()
            await session.refresh(acc)
            return acc.id

    account_id = asyncio.run(_insert())

    # get_active_email returns a *different* email so no auto-switch logic fires
    with patch("backend.services.account_service.get_active_email", return_value="other@example.com"), \
         patch("backend.services.account_service.get_token_info", return_value={}), \
         patch("backend.ws.ws_manager.broadcast", new_callable=AsyncMock):
        patch_resp = client.patch(f"/api/accounts/{account_id}", json={"enabled": False})
        assert patch_resp.status_code == 200
        data = patch_resp.json()
        assert data["enabled"] is False
        assert data["id"] == account_id
