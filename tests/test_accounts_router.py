"""
Tests for backend.routers.accounts.

Uses the ``make_test_app`` conftest fixture to spin up a FastAPI app with
the accounts router and an in-memory SQLite DB.  All Keychain / swap /
login-session machinery is monkeypatched out.
"""
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.cache import cache as _cache
from backend.database import Base, get_db
from backend.models import Account
from backend.routers import accounts as accounts_router
from backend.services import account_queries as aq
from backend.services import account_service as ac
from backend.services import login_session_service as ls
from backend.services import settings_service as ss
from backend.services import switcher as sw


# ── App + DB fixtures ──────────────────────────────────────────────────────


@pytest.fixture
async def app_and_db():
    """Create an app backed by an isolated async SQLite DB.

    Returns (client, session_factory) so tests can seed state directly.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    url = "sqlite+aiosqlite:///./test_accounts_router.db"
    engine = create_async_engine(url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db():
        async with factory() as session:
            yield session

    app = FastAPI()
    app.include_router(accounts_router.router)
    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)

    yield client, factory

    await engine.dispose()


@pytest.fixture(autouse=True)
async def _wipe_cache():
    _cache._usage.clear()
    _cache._token_info.clear()
    yield
    _cache._usage.clear()
    _cache._token_info.clear()


@pytest.fixture(autouse=True)
def _stub_everywhere(monkeypatch):
    """Default stubs — tests override individual callables as needed."""
    async def noop_get_active(): return None
    monkeypatch.setattr(ac, "get_active_email_async", noop_get_active)
    monkeypatch.setattr(ac, "get_active_email", lambda: None)

    def noop_swap(email):
        return {"target_email": email, "previous_email": None, "checkpoint_written": False}

    monkeypatch.setattr(ac, "swap_to_account", noop_swap)
    monkeypatch.setattr(ac, "delete_account_everywhere", lambda email: None)
    monkeypatch.setattr(ac, "get_token_info", lambda email, active_email=None: {})


def _insert_account(factory, **kwargs) -> int:
    """Helper — insert one Account row and return its id."""
    import asyncio
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


# ── GET /api/accounts ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_accounts_has_is_active_no_waiting_field(app_and_db, monkeypatch):
    client, factory = app_and_db
    _insert_account(factory, email="a@example.com")

    async def fake_active(): return "a@example.com"

    monkeypatch.setattr(ac, "get_active_email_async", fake_active)

    resp = client.get("/api/accounts")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["email"] == "a@example.com"
    assert rows[0]["is_active"] is True
    # New architecture drops waiting_for_cli from the response.
    assert "waiting_for_cli" not in rows[0]


# ── POST /api/accounts/verify-login ───────────────────────────────────────


@pytest.mark.asyncio
async def test_verify_login_writes_vault_and_autoswaps_first(app_and_db, monkeypatch):
    client, factory = app_and_db

    async def fake_verify(sid):
        return {
            "success": True,
            "email": "alice@example.com",
            "oauth_account": {"emailAddress": "alice@example.com"},
            "user_id": "uid-alice",
            "oauth_tokens": {"accessToken": "at", "refreshToken": "rt"},
            "kind": "add",
            "expected_email": None,
        }

    # ls.verify_login_session is called via asyncio.to_thread(ls.verify_login_session, sid)
    monkeypatch.setattr(ls, "verify_login_session", fake_verify)
    monkeypatch.setattr(ls, "cleanup_login_session", lambda sid: None)

    save_calls: list = []

    def fake_save_new(email, tokens, oauth, uid):
        save_calls.append((email, tokens, oauth, uid))
        return True

    monkeypatch.setattr(ac, "save_new_vault_account", fake_save_new)

    swap_calls: list[str] = []

    def fake_swap(email):
        swap_calls.append(email)
        return {"target_email": email, "previous_email": None, "checkpoint_written": False}

    monkeypatch.setattr(ac, "swap_to_account", fake_swap)

    # Test: the verify_login endpoint accepts `session_id` as a query param.
    resp = client.post("/api/accounts/verify-login?session_id=abc")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["email"] == "alice@example.com"

    # Vault write happened.
    assert save_calls and save_calls[0][0] == "alice@example.com"

    # First-account auto-swap happened.
    assert swap_calls == ["alice@example.com"]

    # Account row is in DB.
    async with factory() as s:
        rows = (await s.execute(select(Account))).scalars().all()
    assert len(rows) == 1
    assert rows[0].email == "alice@example.com"


# ── DELETE /api/accounts/{id} — non-active ────────────────────────────────


@pytest.mark.asyncio
async def test_delete_non_active_calls_delete_everywhere(app_and_db, monkeypatch):
    client, factory = app_and_db
    acc_id = _insert_account(factory, email="bob@example.com")

    async def fake_active(): return "alice@example.com"  # alice is active, not bob

    monkeypatch.setattr(ac, "get_active_email_async", fake_active)

    delete_calls: list[str] = []

    def fake_delete(email):
        delete_calls.append(email)

    monkeypatch.setattr(ac, "delete_account_everywhere", fake_delete)

    resp = client.delete(f"/api/accounts/{acc_id}")
    assert resp.status_code == 204
    assert delete_calls == ["bob@example.com"]


# ── DELETE /api/accounts/{id} — active with replacement ──────────────────


@pytest.mark.asyncio
async def test_delete_active_with_replacement_calls_perform_switch(app_and_db, monkeypatch):
    client, factory = app_and_db
    a_id = _insert_account(factory, email="alice@example.com", priority=0)
    b_id = _insert_account(factory, email="bob@example.com", priority=1)

    async def fake_active(): return "alice@example.com"

    monkeypatch.setattr(ac, "get_active_email_async", fake_active)

    switch_calls: list[str] = []

    async def fake_perform_switch(target, reason, db, ws):
        switch_calls.append(target.email)

    monkeypatch.setattr(sw, "perform_switch", fake_perform_switch)

    delete_calls: list[str] = []
    monkeypatch.setattr(
        ac, "delete_account_everywhere",
        lambda email: delete_calls.append(email),
    )

    resp = client.delete(f"/api/accounts/{a_id}")
    assert resp.status_code == 204
    # perform_switch was called to activate bob — the replacement.
    assert switch_calls == ["bob@example.com"]
    # delete_account_everywhere was NOT called — the swap handles the cleanup.
    assert delete_calls == []


# ── POST /api/accounts/{id}/switch — already active ──────────────────────


@pytest.mark.asyncio
async def test_manual_switch_already_active(app_and_db, monkeypatch):
    client, factory = app_and_db
    acc_id = _insert_account(factory, email="a@example.com")

    async def fake_active(): return "a@example.com"

    monkeypatch.setattr(ac, "get_active_email_async", fake_active)

    switch_calls: list = []

    async def fake_perform_switch(target, reason, db, ws):
        switch_calls.append(target.email)

    monkeypatch.setattr(sw, "perform_switch", fake_perform_switch)

    resp = client.post(f"/api/accounts/{acc_id}/switch")
    assert resp.status_code == 200
    body = resp.json()
    assert body["already_active"] is True
    # perform_switch was not invoked.
    assert switch_calls == []


# ── Re-login verify — matching email ──────────────────────────────────────


@pytest.mark.asyncio
async def test_relogin_verify_matching_email_clears_stale(app_and_db, monkeypatch):
    client, factory = app_and_db
    acc_id = _insert_account(
        factory, email="alice@example.com", stale_reason="Refresh token revoked",
    )

    async def fake_verify(sid):
        return {
            "success": True,
            "email": "alice@example.com",
            "oauth_account": {"emailAddress": "alice@example.com"},
            "user_id": "uid-alice",
            "oauth_tokens": {"accessToken": "at", "refreshToken": "rt"},
            "kind": "relogin",
            "expected_email": "alice@example.com",
        }

    monkeypatch.setattr(ls, "verify_login_session", fake_verify)
    monkeypatch.setattr(ls, "cleanup_login_session", lambda sid: None)

    save_calls: list = []

    def fake_save(email, tokens, oauth, uid):
        save_calls.append(email)
        return True

    monkeypatch.setattr(ac, "save_new_vault_account", fake_save)

    resp = client.post(f"/api/accounts/{acc_id}/relogin/verify?session_id=xyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    # Vault save was called.
    assert save_calls == ["alice@example.com"]
    # Stale reason is cleared on the row.
    async with factory() as s:
        acc = (await s.execute(
            select(Account).where(Account.id == acc_id)
        )).scalar_one()
        assert acc.stale_reason is None


# ── Re-login verify — wrong email ────────────────────────────────────────


@pytest.mark.asyncio
async def test_relogin_verify_wrong_email_returns_error(app_and_db, monkeypatch):
    client, factory = app_and_db
    acc_id = _insert_account(
        factory, email="alice@example.com", stale_reason="Refresh token revoked",
    )

    async def fake_verify(sid):
        return {
            "success": True,
            "email": "bob@example.com",  # wrong identity!
            "oauth_account": {"emailAddress": "bob@example.com"},
            "user_id": "uid-bob",
            "oauth_tokens": {"accessToken": "at", "refreshToken": "rt"},
            "kind": "relogin",
            "expected_email": "alice@example.com",
        }

    monkeypatch.setattr(ls, "verify_login_session", fake_verify)
    monkeypatch.setattr(ls, "cleanup_login_session", lambda sid: None)

    save_calls: list = []
    monkeypatch.setattr(
        ac, "save_new_vault_account",
        lambda e, t, o, u: save_calls.append(e) or True,
    )

    resp = client.post(f"/api/accounts/{acc_id}/relogin/verify?session_id=xyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is False
    error = body.get("error") or ""
    assert "alice@example.com" in error
    assert "bob@example.com" in error
    # Stale reason NOT cleared.
    async with factory() as s:
        acc = (await s.execute(
            select(Account).where(Account.id == acc_id)
        )).scalar_one()
        assert acc.stale_reason == "Refresh token revoked"
    # save_new_vault_account never called for wrong-identity.
    assert save_calls == []
