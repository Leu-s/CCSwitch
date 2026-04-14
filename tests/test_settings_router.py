import asyncio
import pytest
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

TEST_DB_URL = "sqlite+aiosqlite:///./test_settings.db"

@pytest.fixture(scope="module")
def client(make_test_app):
    from backend.routers.settings import router
    from backend.services.settings_service import ensure_defaults

    app, c = make_test_app(router, db_name="settings")

    # Seed default settings rows — required by tests that check defaults exist.
    engine = create_async_engine(TEST_DB_URL, echo=False)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _seed():
        async with SessionLocal() as session:
            await ensure_defaults(session)

    asyncio.run(_seed())
    return c

def test_get_settings_returns_defaults(client):
    resp = client.get("/api/settings")
    assert resp.status_code == 200
    data = resp.json()
    keys = {s["key"] for s in data}
    assert "usage_poll_interval_seconds" in keys
    # auto_switch_enabled was merged into service_enabled and is no longer a setting
    assert "auto_switch_enabled" not in keys
    # switch_threshold_percent removed — threshold is now per-account
    assert "switch_threshold_percent" not in keys

def test_patch_setting(client):
    resp = client.patch("/api/settings/tmux_nudge_message", json={"value": "continue"})
    assert resp.status_code == 200
    assert resp.json()["value"] == "continue"

def test_patch_auto_switch_enabled_rejected(client):
    """auto_switch_enabled was removed from ALLOWED_KEYS — PATCH must 403."""
    resp = client.patch("/api/settings/auto_switch_enabled", json={"value": "true"})
    assert resp.status_code == 403

def test_patch_custom_key_rejected(client):
    resp = client.patch("/api/settings/custom_key", json={"value": "hello"})
    assert resp.status_code == 403
