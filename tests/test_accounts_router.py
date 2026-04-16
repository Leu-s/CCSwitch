"""
Tests for backend.routers.accounts.

Uses the ``make_test_app`` conftest fixture to spin up a FastAPI app with
the accounts router and an in-memory SQLite DB.  All Keychain / swap /
login-session machinery is monkeypatched out.

These tests are synchronous — the Starlette TestClient drives the event
loop internally so wrapping them in ``@pytest.mark.asyncio`` breaks the
``asyncio.run()`` calls used to seed the DB.
"""
import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.cache import cache as _cache
from backend.database import Base, get_db
from backend.models import Account
from backend.routers import accounts as accounts_router
from backend.services import account_service as ac
from backend.services import login_session_service as ls
from backend.services import switcher as sw


# ── App + DB fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def client_and_factory():
    """Create a sync TestClient backed by an isolated async SQLite DB.

    Returns (client, session_factory).  The caller can run
    ``asyncio.run()`` to seed the DB because the fixture itself is not
    inside an event loop.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    url = "sqlite+aiosqlite:///./test_accounts_router.db"
    engine = create_async_engine(url, echo=False)

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db():
        async with factory() as session:
            yield session

    app = FastAPI()
    app.include_router(accounts_router.router)
    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)

    yield client, factory


@pytest.fixture(autouse=True)
def _wipe_cache():
    _cache._usage.clear()
    _cache._token_info.clear()
    yield
    _cache._usage.clear()
    _cache._token_info.clear()


@pytest.fixture(autouse=True)
def _stub_everywhere(monkeypatch):
    """Default stubs — tests override individual callables as needed."""
    async def noop_get_active():
        return None

    monkeypatch.setattr(ac, "get_active_email_async", noop_get_active)
    monkeypatch.setattr(ac, "get_active_email", lambda: None)

    def noop_swap(email):
        return {"target_email": email, "previous_email": None, "checkpoint_written": False}

    monkeypatch.setattr(ac, "swap_to_account", noop_swap)
    monkeypatch.setattr(ac, "delete_account_everywhere", lambda email: None)
    monkeypatch.setattr(ac, "get_token_info", lambda email, active_email=None: {})


def _insert_account(factory, **kwargs) -> int:
    """Helper — insert one Account row and return its id.  Called from
    the sync portion of the test body."""
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


def _query_account(factory, account_id: int) -> Account:
    async def _run():
        async with factory() as session:
            row = (await session.execute(
                select(Account).where(Account.id == account_id)
            )).scalar_one()
            # Load attributes before session closes.
            _ = (row.id, row.email, row.stale_reason, row.enabled)
            return row

    return asyncio.run(_run())


# ── GET /api/accounts ─────────────────────────────────────────────────────


def test_list_accounts_has_is_active_no_waiting_field(client_and_factory, monkeypatch):
    client, factory = client_and_factory
    _insert_account(factory, email="a@example.com")

    async def fake_active():
        return "a@example.com"

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


def test_verify_login_writes_vault_and_autoswaps_first(client_and_factory, monkeypatch):
    client, factory = client_and_factory

    def fake_verify(sid):
        return {
            "success": True,
            "email": "alice@example.com",
            "oauth_account": {"emailAddress": "alice@example.com"},
            "user_id": "uid-alice",
            "oauth_tokens": {"accessToken": "at", "refreshToken": "rt"},
            "kind": "add",
            "expected_email": None,
        }

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
    async def _count():
        async with factory() as s:
            return (await s.execute(select(Account))).scalars().all()

    rows = asyncio.run(_count())
    assert len(rows) == 1
    assert rows[0].email == "alice@example.com"


def test_verify_login_duplicate_email_returns_already_exists(
    client_and_factory, monkeypatch
):
    """Second verify-login for an email already in the DB must return
    ``already_exists=True``, still write the (new) vault tokens, and
    NOT broadcast account_added."""
    client, factory = client_and_factory
    # Pre-seed a row for alice so the second save_verified_account sees it.
    _insert_account(factory, email="alice@example.com", priority=0)

    monkeypatch.setattr(ls, "verify_login_session", lambda sid: {
        "success": True,
        "email": "alice@example.com",
        "oauth_account": {"emailAddress": "alice@example.com"},
        "user_id": "uid-alice",
        "oauth_tokens": {"accessToken": "at", "refreshToken": "rt"},
        "kind": "add",
        "expected_email": None,
    })
    monkeypatch.setattr(ls, "cleanup_login_session", lambda sid: None)
    monkeypatch.setattr(ac, "save_new_vault_account",
                        lambda email, tokens, oauth, uid: True)
    swap_calls: list[str] = []
    monkeypatch.setattr(
        ac, "swap_to_account",
        lambda email: swap_calls.append(email) or {},
    )

    resp = client.post("/api/accounts/verify-login?session_id=abc")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["already_exists"] is True
    # First-account auto-activation must NOT fire on a duplicate — the
    # account already existed before this call.
    assert swap_calls == []


# ── DELETE /api/accounts/{id} — non-active ────────────────────────────────


def test_delete_non_active_calls_delete_everywhere(client_and_factory, monkeypatch):
    client, factory = client_and_factory
    acc_id = _insert_account(factory, email="bob@example.com")

    async def fake_active():
        return "alice@example.com"  # alice is active, not bob

    monkeypatch.setattr(ac, "get_active_email_async", fake_active)

    delete_calls: list[str] = []

    def fake_delete(email):
        delete_calls.append(email)

    monkeypatch.setattr(ac, "delete_account_everywhere", fake_delete)

    resp = client.delete(f"/api/accounts/{acc_id}")
    assert resp.status_code == 204
    assert delete_calls == ["bob@example.com"]


def test_delete_account_invokes_state_cleanup_calls(client_and_factory, monkeypatch):
    """The DELETE /api/accounts/{id} handler must clear every module-level
    per-account dict — both the background-module dicts (via
    ``forget_account_state``) and the per-email refresh lock dict in
    account_service (via ``forget_refresh_lock``).  A regression that
    drops either cleanup call would leak dicts forever across churn."""
    from backend import background as bg

    client, factory = client_and_factory
    acc_id = _insert_account(factory, email="alice@example.com")

    async def fake_active():
        return "bob@example.com"  # bob is active, not alice

    monkeypatch.setattr(ac, "get_active_email_async", fake_active)
    monkeypatch.setattr(ac, "delete_account_everywhere", lambda email: None)

    # Seed dicts in both modules for alice — both cleanup calls must fire.
    bg._refresh_backoff_count["alice@example.com"] = 1
    import threading as _threading
    ac._refresh_locks["alice@example.com"] = _threading.Lock()

    try:
        resp = client.delete(f"/api/accounts/{acc_id}")
        assert resp.status_code == 204

        # forget_account_state was called → all background dicts purged.
        assert "alice@example.com" not in bg._refresh_backoff_count
        # forget_refresh_lock was called → per-email lock dict purged.
        assert "alice@example.com" not in ac._refresh_locks
    finally:
        # Defensive: clear anything the test may have left behind even if
        # the DELETE handler regressed.
        bg._refresh_backoff_count.pop("alice@example.com", None)
        ac._refresh_locks.pop("alice@example.com", None)


# ── DELETE /api/accounts/{id} — active with replacement ──────────────────


def test_delete_active_with_replacement_calls_perform_switch(client_and_factory, monkeypatch):
    client, factory = client_and_factory
    a_id = _insert_account(factory, email="alice@example.com", priority=0)
    _insert_account(factory, email="bob@example.com", priority=1)

    async def fake_active():
        return "alice@example.com"

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
    # delete_account_everywhere is called AFTER the swap to wipe the
    # outgoing account's vault entry.  perform_switch checkpoints the
    # outgoing credentials into vault[alice] as part of step 2; without
    # this cleanup call every active-delete would leak a ghost vault
    # entry for the deleted email.
    assert delete_calls == ["alice@example.com"]


# ── POST /api/accounts/{id}/switch — already active ──────────────────────


def test_manual_switch_already_active(client_and_factory, monkeypatch):
    client, factory = client_and_factory
    acc_id = _insert_account(factory, email="a@example.com")

    async def fake_active():
        return "a@example.com"

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


def test_relogin_verify_matching_email_clears_stale(client_and_factory, monkeypatch):
    client, factory = client_and_factory
    acc_id = _insert_account(
        factory, email="alice@example.com", stale_reason="Refresh token revoked",
    )

    def fake_verify(sid):
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
    acc = _query_account(factory, acc_id)
    assert acc.stale_reason is None


# ── Re-login verify — wrong email ────────────────────────────────────────


def test_relogin_verify_wrong_email_returns_error(client_and_factory, monkeypatch):
    client, factory = client_and_factory
    acc_id = _insert_account(
        factory, email="alice@example.com", stale_reason="Refresh token revoked",
    )

    def fake_verify(sid):
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
    acc = _query_account(factory, acc_id)
    assert acc.stale_reason == "Refresh token revoked"
    # save_new_vault_account never called for wrong-identity.
    assert save_calls == []


# ── POST /api/accounts/{id}/revalidate ────────────────────────────────────


def test_revalidate_endpoint_returns_200_on_success(client_and_factory, monkeypatch):
    """Happy path — revalidate_account returns success dict → 200 body."""
    client, _factory = client_and_factory

    async def fake_revalidate(account_id, db):
        return {
            "success": True,
            "stale_reason": None,
            "email": "vault@example.com",
            "active_refused": False,
        }

    monkeypatch.setattr(ac, "revalidate_account", fake_revalidate)

    resp = client.post("/api/accounts/1/revalidate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["stale_reason"] is None
    assert body["email"] == "vault@example.com"
    assert body["active_refused"] is False


def test_revalidate_endpoint_404_for_missing_account(client_and_factory, monkeypatch):
    """revalidate_account returns None → 404."""
    client, _factory = client_and_factory

    async def fake_revalidate(account_id, db):
        return None

    monkeypatch.setattr(ac, "revalidate_account", fake_revalidate)

    resp = client.post("/api/accounts/999/revalidate")
    assert resp.status_code == 404


def test_revalidate_endpoint_409_on_refresh_failure(client_and_factory, monkeypatch):
    """Refresh-failure path: 409 Conflict with the accurate stale_reason in the body.
    Frontend error middleware can handle the 409 uniformly; the body stays readable."""
    client, _factory = client_and_factory

    async def fake_revalidate(account_id, db):
        return {
            "success": False,
            "stale_reason": "Refresh token rejected — re-login required",
            "email": "vault@example.com",
            "active_refused": False,
        }

    monkeypatch.setattr(ac, "revalidate_account", fake_revalidate)

    resp = client.post("/api/accounts/1/revalidate")
    assert resp.status_code == 409
    body = resp.json()
    # FastAPI wraps HTTPException.detail under the "detail" key.
    payload = body.get("detail", body)
    assert payload["success"] is False
    assert "rejected" in payload["stale_reason"].lower()
    assert payload["active_refused"] is False


def test_revalidate_endpoint_409_on_active_account(client_and_factory, monkeypatch):
    """Active-account revalidate → 409 with active_refused=True and a message
    that tells the user to switch first."""
    client, _factory = client_and_factory

    async def fake_revalidate(account_id, db):
        return {
            "success": False,
            "stale_reason": "Refresh token rejected — re-login required",
            "email": "active@example.com",
            "active_refused": True,
        }

    monkeypatch.setattr(ac, "revalidate_account", fake_revalidate)

    resp = client.post("/api/accounts/1/revalidate")
    assert resp.status_code == 409
    payload = resp.json().get("detail", resp.json())
    assert payload["active_refused"] is True


def test_revalidate_endpoint_broadcasts_account_updated_on_success(
    client_and_factory, monkeypatch
):
    """Success broadcasts account_updated with stale_reason=None so connected
    clients update the card immediately instead of waiting for next usage_updated.

    (verify-relogin omits stale_reason in its broadcast, forcing the UI to wait
    for a subsequent usage_updated poll — this endpoint fixes that for its own
    flow.)
    """
    client, _factory = client_and_factory

    async def fake_revalidate(account_id, db):
        return {
            "success": True,
            "stale_reason": None,
            "email": "vault@example.com",
            "active_refused": False,
        }

    monkeypatch.setattr(ac, "revalidate_account", fake_revalidate)

    broadcasts: list = []

    async def fake_broadcast(msg):
        broadcasts.append(msg)

    from backend import ws as ws_mod
    monkeypatch.setattr(ws_mod.ws_manager, "broadcast", fake_broadcast)

    resp = client.post("/api/accounts/1/revalidate")
    assert resp.status_code == 200
    assert any(
        b.get("type") == "account_updated"
        and b.get("email") == "vault@example.com"
        and b.get("stale_reason") is None
        for b in broadcasts
    )
