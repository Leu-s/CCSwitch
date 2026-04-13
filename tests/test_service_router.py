"""Service router tests — enable/disable/status/default-account endpoints."""
import asyncio
import json
import pytest
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from unittest.mock import patch, MagicMock, AsyncMock

TEST_DB_URL = "sqlite+aiosqlite:///./test_service.db"


@pytest.fixture(scope="module")
def client(make_test_app):
    from backend.routers.service import router
    _, c = make_test_app(router, db_name="service")
    return c


# ── Helpers ─────────────────────────────────────────────────────────────────


def _seed_account(client, email="test@example.com", config_dir="/tmp/test_config"):
    """Insert an account directly via the DB by calling the accounts router.

    Since the service router tests use a separate DB we insert the row
    via raw SQLAlchemy instead of going through the accounts router.
    """
    from backend.models import Account

    engine = create_async_engine(TEST_DB_URL, echo=False)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _insert():
        async with SessionLocal() as session:
            account = Account(email=email, config_dir=config_dir)
            session.add(account)
            await session.commit()
            await session.refresh(account)
            return account.id

    return asyncio.run(_insert())


def _set_setting_direct(key, value):
    """Write a Setting row directly into the test DB."""
    from backend.models import Setting

    engine = create_async_engine(TEST_DB_URL, echo=False)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _write():
        async with SessionLocal() as session:
            from sqlalchemy import select
            row = await session.execute(select(Setting).where(Setting.key == key))
            s = row.scalars().first()
            if s:
                s.value = value
            else:
                session.add(Setting(key=key, value=value))
            await session.commit()

    asyncio.run(_write())


# ── Tests ────────────────────────────────────────────────────────────────────


def test_get_status_disabled(client):
    """Fresh DB — no settings row — service should report disabled."""
    with patch("backend.services.account_service.get_active_email", return_value=None):
        resp = client.get("/api/service")
    assert resp.status_code == 200
    data = resp.json()
    assert data["enabled"] is False
    assert data["active_email"] is None
    assert data["default_account_id"] is None


def test_enable_no_accounts_returns_400(client):
    """POST /enable with no accounts in DB must return 400."""
    with patch("backend.services.account_service.backup_active_config", return_value={}), \
         patch("backend.services.account_service.activate_account_config"):
        resp = client.post("/api/service/enable")
    assert resp.status_code == 400


def test_enable_activates_first_account(client):
    """Seed one account; POST /enable should call perform_switch for the seeded account."""
    config_dir = "/tmp/test_config_enable"
    _seed_account(client, email="enable@example.com", config_dir=config_dir)

    with patch("backend.services.account_service.backup_active_config", return_value={}) as mock_backup, \
         patch("backend.services.switcher.perform_switch", new_callable=AsyncMock) as mock_switch, \
         patch("backend.services.account_service.get_active_email", return_value="enable@example.com"):
        resp = client.post("/api/service/enable")

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["active_email"] == "enable@example.com"
    mock_backup.assert_called_once()
    mock_switch.assert_called_once()
    # Verify the call was made with the seeded account (first positional arg)
    switched_account = mock_switch.call_args[0][0]
    assert switched_account.config_dir == config_dir


def test_disable_uses_backup(client):
    """When original_credentials_backup is set, disable should call restore_config_from_backup."""
    backup_payload = {"some": "credentials"}
    _set_setting_direct("original_credentials_backup", json.dumps(backup_payload))
    # Ensure no default_account_id is set so we fall through to the backup path.
    _set_setting_direct("default_account_id", "")

    with patch("backend.services.account_service.restore_config_from_backup") as mock_restore, \
         patch("backend.services.account_service.get_active_email", return_value=None):
        resp = client.post("/api/service/disable")

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    mock_restore.assert_called_once_with(backup_payload)


def test_set_default_account(client):
    """PATCH /default-account sets the default; GET /status reflects it."""
    account_id = _seed_account(client, email="default@example.com", config_dir="/tmp/default_config")

    # Set default account
    resp = client.patch(f"/api/service/default-account?account_id={account_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["default_account_id"] == account_id
    assert data["email"] == "default@example.com"

    # GET /api/service should now show the default_account_id
    with patch("backend.services.account_service.get_active_email", return_value=None):
        resp = client.get("/api/service")
    assert resp.status_code == 200
    assert resp.json()["default_account_id"] == account_id
