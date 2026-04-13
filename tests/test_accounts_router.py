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


def test_delete_sole_active_account_with_service_enabled(client):
    """DELETE the only account when it is both active and the service is enabled.

    The router must:
      - Return 200 / 204 (no crash)
      - Remove the account from the database
      - Call clear_active_config_dir (no replacement exists) rather than leaving
        orphaned state, and succeed gracefully.
    """
    engine = create_async_engine(TEST_DB_URL, echo=False)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    SOLE_EMAIL = "sole-active@example.com"

    async def _insert():
        from backend.models import Setting
        from sqlalchemy import select as sa_select
        async with SessionLocal() as session:
            acc = Account(email=SOLE_EMAIL, config_dir="/tmp/sole-active", priority=10, enabled=True)
            session.add(acc)
            await session.commit()
            await session.refresh(acc)
            # Mark service as enabled in the DB so the router sees it as active.
            row = await session.execute(sa_select(Setting).where(Setting.key == "service_enabled"))
            setting = row.scalars().first()
            if setting:
                setting.value = "true"
            else:
                session.add(Setting(key="service_enabled", value="true"))
            await session.commit()
            return acc.id

    account_id = asyncio.run(_insert())

    with patch("backend.services.account_service.get_active_email", return_value=SOLE_EMAIL), \
         patch("backend.services.account_service.get_token_info", return_value={}), \
         patch("backend.services.account_service.clear_active_config_dir") as mock_clear, \
         patch("backend.ws.ws_manager.broadcast", new_callable=AsyncMock):
        # get_next_account returns None — no other enabled account exists.
        with patch("backend.services.switcher.get_next_account", new_callable=AsyncMock, return_value=None):
            delete_resp = client.delete(f"/api/accounts/{account_id}")

    # Must succeed (204 is the normal status for a successful delete)
    assert delete_resp.status_code == 204

    # Account must be gone from the DB
    with patch("backend.services.account_service.get_active_email", return_value=None), \
         patch("backend.services.account_service.get_token_info", return_value={}):
        list_resp = client.get("/api/accounts")
    assert list_resp.status_code == 200
    ids = [a["id"] for a in list_resp.json()]
    assert account_id not in ids

    # clear_active_config_dir must have been called (no orphaned pointer)
    mock_clear.assert_called_once()
