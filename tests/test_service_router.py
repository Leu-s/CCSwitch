"""
Tests for backend.routers.service.

Covers the master-switch toggle (enable / disable) and the
``default-account`` endpoint.  All credential-touching calls are stubbed
out.  The enable endpoint's key behavior is that it preserves the
currently-active account when valid — only activating the default /
first enabled account when there is no valid active email.
"""
import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import Base, get_db
from backend.models import Account
from backend.routers import service as service_router
from backend.services import account_service as ac
from backend.services import settings_service as ss
from backend.services import switcher as sw


@pytest.fixture
def client_and_factory():
    """Create a sync TestClient backed by an isolated async SQLite DB.

    The DB already has the service router mounted AND the
    ``ensure_defaults`` rows seeded, so GET /api/service returns a
    well-formed payload.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    url = "sqlite+aiosqlite:///./test_service_router.db"
    engine = create_async_engine(url, echo=False)

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as session:
            await ss.ensure_defaults(session)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    asyncio.run(_init())

    async def override_get_db():
        async with factory() as session:
            yield session

    app = FastAPI()
    app.include_router(service_router.router)
    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    yield client, factory


def _insert_account(factory, **kwargs) -> int:
    defaults = dict(
        email="a@example.com",
        threshold_pct=95.0,
        enabled=True,
        priority=0,
        stale_reason=None,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kwargs)

    async def _insert():
        async with factory() as session:
            acc = Account(**defaults)
            session.add(acc)
            await session.commit()
            await session.refresh(acc)
            return acc.id

    return asyncio.run(_insert())


# ── GET /api/service ──────────────────────────────────────────────────────


def test_get_service_status_shape(client_and_factory, monkeypatch):
    client, _ = client_and_factory

    async def fake_active():
        return "a@example.com"

    monkeypatch.setattr(ac, "get_active_email_async", fake_active)

    resp = client.get("/api/service")
    assert resp.status_code == 200
    body = resp.json()
    assert "enabled" in body
    assert "active_email" in body
    assert "default_account_id" in body
    assert body["active_email"] == "a@example.com"
    assert body["enabled"] is False  # default


# ── POST /api/service/enable — current active preserved ──────────────────


def test_enable_preserves_current_active_email(client_and_factory, monkeypatch):
    """With ``~/.claude/.claude.json`` naming ``a@example.com`` and that
    account enabled in the DB, enable_service must NOT call perform_switch."""
    client, factory = client_and_factory
    _insert_account(factory, email="a@example.com")
    _insert_account(factory, email="b@example.com", priority=1)

    async def fake_active():
        return "a@example.com"

    monkeypatch.setattr(ac, "get_active_email_async", fake_active)

    switch_calls: list[str] = []

    async def fake_perform_switch(target, reason, db, ws):
        switch_calls.append(target.email)

    monkeypatch.setattr(sw, "perform_switch", fake_perform_switch)

    resp = client.post("/api/service/enable")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["active_email"] == "a@example.com"
    # perform_switch was NOT invoked.
    assert switch_calls == []


# ── POST /api/service/enable — no valid active → swap to default ──────────


def test_enable_swaps_when_no_valid_active(client_and_factory, monkeypatch):
    """With ``~/.claude/.claude.json`` absent (or naming an unknown email),
    enable_service calls perform_switch on the first enabled account."""
    client, factory = client_and_factory
    _insert_account(factory, email="a@example.com", priority=0)
    _insert_account(factory, email="b@example.com", priority=1)

    async def fake_active():
        return None  # No valid active email

    monkeypatch.setattr(ac, "get_active_email_async", fake_active)

    switch_calls: list[str] = []

    async def fake_perform_switch(target, reason, db, ws):
        switch_calls.append(target.email)

    monkeypatch.setattr(sw, "perform_switch", fake_perform_switch)

    resp = client.post("/api/service/enable")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    # First-priority account was activated.
    assert switch_calls == ["a@example.com"]
    assert body["active_email"] == "a@example.com"


# ── POST /api/service/disable ─────────────────────────────────────────────


def test_disable_only_flips_flag(client_and_factory, monkeypatch):
    client, factory = client_and_factory
    _insert_account(factory, email="a@example.com")

    async def fake_active():
        return "a@example.com"

    monkeypatch.setattr(ac, "get_active_email_async", fake_active)

    swap_calls: list = []
    monkeypatch.setattr(
        ac, "swap_to_account",
        lambda email: swap_calls.append(email) or {},
    )
    delete_calls: list = []
    monkeypatch.setattr(
        ac, "delete_account_everywhere",
        lambda email: delete_calls.append(email),
    )

    # First enable so there is something to disable.
    async def fake_perform_switch(*args, **kwargs):
        return None

    monkeypatch.setattr(sw, "perform_switch", fake_perform_switch)

    client.post("/api/service/enable")

    resp = client.post("/api/service/disable")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True

    # No credential manipulation — disable is a pure flag flip.
    assert swap_calls == []
    assert delete_calls == []

    # Flag is false after disable.
    resp2 = client.get("/api/service")
    assert resp2.json()["enabled"] is False
