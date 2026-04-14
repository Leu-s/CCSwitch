"""
End-to-end WebSocket broadcast integration tests.

Unlike ``test_ws.py`` which exercises the ``WebSocketManager`` class in
isolation, these tests mount the real ``accounts`` router PLUS a minimal
``/ws`` endpoint and drive them with ``TestClient.websocket_connect`` so
we can prove the full contract:

    HTTP action → router handler → ws_manager.broadcast →
    connected WS clients actually receive the message

Specifically, this file covers the multi-tab fixes from the third audit
round that the pure-unit tests in ``test_accounts_router.py`` cannot
validate:

* ``verify_login`` broadcasts ``account_added`` so Tab B discovers a new
  slot enrolled in Tab A (Wave 2 B3 M1 bug — ``updateUsageLive`` drops
  unknown rows and Tab B would otherwise never see the card).
* ``verify_relogin`` broadcasts on health-flip so sibling tabs see the
  stale → healthy transition without waiting for the next poll cycle.
* ``manual_switch`` / ``force_refresh`` broadcasts reach every connected
  client simultaneously (the multi-tab consistency path).

The key difference from the mocked router tests: here we use REAL
``ws_manager`` + REAL WebSocket round-tripping via TestClient's ASGI
test transport.  A regression that causes a broadcast to be queued but
never flushed, or silently swallowed by the ws layer, would pass the
mocked tests but fail here.
"""
import asyncio
import json

import pytest
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from unittest.mock import AsyncMock, MagicMock, patch

from backend.database import Base, get_db
from backend.models import Account
from backend.routers import accounts as accounts_router
from backend.ws import ws_manager


TEST_DB_URL = "sqlite+aiosqlite:///./test_ws_integration.db"


@pytest.fixture(scope="module")
def app_client():
    """Real FastAPI app wired with the accounts router + a minimal /ws
    endpoint that joins/leaves the shared ``ws_manager``.  We deliberately
    skip the snapshot-on-connect logic from ``main.py`` so the first frame
    a test receives is always the broadcast under test (not an initial
    snapshot we'd have to drain)."""
    engine = create_async_engine(TEST_DB_URL, echo=False)
    SessionLocal = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())

    async def override_get_db():
        async with SessionLocal() as session:
            yield session

    app = FastAPI()
    app.include_router(accounts_router.router)
    app.dependency_overrides[get_db] = override_get_db

    @app.websocket("/ws")
    async def _ws(websocket: WebSocket):
        await ws_manager.connect(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            ws_manager.disconnect(websocket)

    client = TestClient(app)
    return app, client, SessionLocal


def _clear_ws_manager():
    """Flush any lingering connections from prior tests — the ws_manager
    module-level singleton persists across tests in the same process, so
    a crashed test could leave stale entries that would receive our
    broadcasts and cause subscript confusion."""
    ws_manager.active_connections.clear()


def _seed_account(SessionLocal, email: str, config_dir: str) -> int:
    """Insert a non-stale account row and return its id."""
    async def _go():
        async with SessionLocal() as session:
            acc = Account(
                email=email,
                config_dir=config_dir,
                priority=0,
                threshold_pct=95,
                enabled=True,
            )
            session.add(acc)
            await session.commit()
            await session.refresh(acc)
            return acc.id

    return asyncio.run(_go())


# ── verify_login → account_added broadcast (fix #3) ──────────────────────────


def test_verify_login_account_added_reaches_both_ws_clients(app_client):
    """Regression for Wave 2 B3 M1 / audit-round-3 fix #3.

    Two WS clients simulate Tab A and Tab B.  Tab A is the initiating tab
    that POSTs /verify-login (in real life it also dispatches
    ``app:reload-accounts`` locally).  Tab B is a sibling tab that only
    sees events via the WebSocket.  Before the fix, Tab B would never
    learn about the new slot because ``updateUsageLive`` drops unknown
    rows; after the fix, the dedicated ``account_added`` broadcast
    prompts Tab B to reload /api/accounts.
    """
    app, client, _ = app_client
    _clear_ws_manager()

    with patch(
        "backend.routers.accounts.ls.verify_login_session",
        return_value={
            "success": True,
            "email": "tab-b-reaches@example.com",
            "config_dir": "/tmp/tab-b-reaches-dir",
        },
    ), patch(
        # Stub the pointer write so the test doesn't touch real ~/.ccswitch
        "backend.routers.accounts.ac.write_active_config_dir",
    ):
        with client.websocket_connect("/ws") as tab_a, \
             client.websocket_connect("/ws") as tab_b:
            # Both tabs are now subscribed.  Trigger verify-login (Tab A
            # is the initiator but both should receive the broadcast).
            resp = client.post(
                "/api/accounts/verify-login?session_id=multi-tab-test"
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["success"] is True
            assert body["email"] == "tab-b-reaches@example.com"

            # Both tabs must receive exactly one account_added frame.
            msg_a = tab_a.receive_json()
            msg_b = tab_b.receive_json()

    assert msg_a["type"] == "account_added"
    assert msg_a["email"] == "tab-b-reaches@example.com"
    assert "id" in msg_a
    assert "seq" in msg_a  # ws_manager stamps seq on every broadcast

    assert msg_b["type"] == "account_added"
    assert msg_b["email"] == "tab-b-reaches@example.com"
    assert msg_b["id"] == msg_a["id"]
    assert msg_b["seq"] == msg_a["seq"], (
        "Tab A and Tab B must receive the same sequence number — the "
        "broadcast is a single fire-to-all, not per-client"
    )


def test_verify_login_duplicate_does_not_broadcast_account_added(app_client):
    """A duplicate-email verify must NOT fire account_added — otherwise Tab
    B would reload only to find the same card count it already has, and
    in the worst case we'd create a feedback loop between the toast and
    the reload.  The router's ``if saved is None`` short-circuit runs
    BEFORE the broadcast line, and this test pins that ordering."""
    app, client, _ = app_client
    _clear_ws_manager()

    # Enroll once so the second verify hits the UNIQUE email constraint.
    with patch(
        "backend.routers.accounts.ls.verify_login_session",
        return_value={
            "success": True,
            "email": "dup-no-broadcast@example.com",
            "config_dir": "/tmp/dup-no-broadcast-first",
        },
    ), patch(
        "backend.routers.accounts.ac.write_active_config_dir",
    ), patch(
        "backend.ws.ws_manager.broadcast",
        new_callable=AsyncMock,
    ):
        first = client.post(
            "/api/accounts/verify-login?session_id=dup-first"
        )
        assert first.status_code == 200

    # Second verify: same email, should set already_exists=True and
    # short-circuit before the broadcast.
    with patch(
        "backend.routers.accounts.ls.verify_login_session",
        return_value={
            "success": True,
            "email": "dup-no-broadcast@example.com",
            "config_dir": "/tmp/dup-no-broadcast-second",
        },
    ), patch(
        "backend.routers.accounts.ls.cleanup_login_session",
    ):
        with client.websocket_connect("/ws") as ws:
            resp = client.post(
                "/api/accounts/verify-login?session_id=dup-second"
            )
            assert resp.status_code == 200
            assert resp.json()["already_exists"] is True

            # Nothing should have been broadcast to the WS client —
            # starving the second verify's ``account_added`` path is the
            # whole point of the duplicate short-circuit.  We probe by
            # firing a quick side broadcast that we KNOW should arrive,
            # and assert it's the FIRST thing we receive.
            asyncio.run(
                ws_manager.broadcast({"type": "sentinel-probe", "marker": 1})
            )
            first_frame = ws.receive_json()

    assert first_frame["type"] == "sentinel-probe", (
        "A duplicate verify-login must not broadcast account_added — the "
        "first frame the WS sees after a duplicate POST is the sentinel "
        "we fired ourselves, not an account_added from the endpoint"
    )


# ── manual switch → account_switched broadcast (fix #5 contract) ─────────────


def test_manual_switch_account_switched_reaches_both_ws_clients(app_client):
    """Regression for the ``account_switched`` broadcast contract.

    Fix #5 adds ``renderAccounts()`` to the frontend handler for immediate
    flicker-free re-render, but the backend contract (the broadcast
    reaching every connected tab with a consistent payload) must hold
    for the frontend fix to mean anything.  This test pins the backend
    half: two WS clients both receive the same ``account_switched``
    frame with ``from`` / ``to`` / ``reason`` / ``seq``.
    """
    app, client, SessionLocal = app_client
    _clear_ws_manager()

    # Seed two accounts so perform_switch has a target and a source.
    a_id = _seed_account(
        SessionLocal, "switch-from@example.com", "/tmp/switch-from-dir"
    )
    b_id = _seed_account(
        SessionLocal, "switch-to@example.com", "/tmp/switch-to-dir"
    )

    with patch(
        "backend.routers.accounts.ac.get_active_email_async",
        new_callable=AsyncMock,
        return_value="switch-from@example.com",
    ), patch(
        "backend.services.switcher.ac.get_active_email_async",
        new_callable=AsyncMock,
        return_value="switch-from@example.com",
    ), patch(
        "backend.services.credential_targets.enabled_canonical_paths",
        new_callable=AsyncMock,
        return_value=[],
    ), patch(
        "backend.services.account_service.activate_account_config",
        return_value={
            "mirror": {"written": [], "skipped": [], "errors": []},
            "keychain_written": False,
            "system_default_enabled": False,
        },
    ):
        with client.websocket_connect("/ws") as tab_a, \
             client.websocket_connect("/ws") as tab_b:
            resp = client.post(f"/api/accounts/{b_id}/switch")
            assert resp.status_code == 200
            assert resp.json() == {"ok": True, "already_active": False}

            msg_a = tab_a.receive_json()
            msg_b = tab_b.receive_json()

    assert msg_a["type"] == "account_switched"
    assert msg_a["to"] == "switch-to@example.com"
    assert msg_a["from"] == "switch-from@example.com"
    assert msg_a["reason"] == "manual"
    assert msg_b["type"] == "account_switched"
    assert msg_b["seq"] == msg_a["seq"], (
        "Both tabs must see the same account_switched sequence — a fan-out "
        "bug that delivered different seqs to different clients would break "
        "the frontend's eager-mutation guard"
    )


# ── force-refresh success → usage_updated broadcast (fix #1 end-to-end) ──────


def test_force_refresh_success_broadcast_reaches_ws_client(app_client):
    """Regression for audit-round-3 fix #1: the post-success path fires a
    single-account ``usage_updated`` broadcast with ``waiting_for_cli=False``
    so connected tabs flip out of the waiting state immediately.

    Combined with the split-try fix, this test validates that even if one
    of the three post-success ops fails, the broadcast still runs and the
    WS client still observes the flip.
    """
    from backend.cache import cache as _cache

    app, client, SessionLocal = app_client
    _clear_ws_manager()

    email = "force-refresh-e2e@example.com"
    acct_id = _seed_account(SessionLocal, email, "/tmp/force-refresh-e2e-dir")

    # Seed waiting so we can verify the broadcast carries waiting=False.
    asyncio.run(_cache.set_waiting(email))

    fresh_token_info = {
        "token_expires_at": 9999999999999,
        "subscription_type": "max",
    }

    with patch(
        "backend.routers.accounts.ac.force_refresh_config_dir",
        new_callable=AsyncMock,
        return_value=fresh_token_info,
    ), patch(
        "backend.routers.accounts.ac.get_active_email_async",
        new_callable=AsyncMock,
        return_value=email,
    ):
        with client.websocket_connect("/ws") as ws:
            resp = client.post(f"/api/accounts/{acct_id}/force-refresh")
            assert resp.status_code == 200
            msg = ws.receive_json()

    assert msg["type"] == "usage_updated"
    assert len(msg["accounts"]) == 1
    entry = msg["accounts"][0]
    assert entry["email"] == email
    assert entry["waiting_for_cli"] is False, (
        "Post-success broadcast must carry waiting_for_cli=False so the "
        "card flips out of the waiting state without a poll-cycle delay"
    )
    assert entry["stale_reason"] is None

    asyncio.run(_cache.invalidate(email))


def test_force_refresh_401_broadcast_reaches_ws_client(app_client):
    """Regression for audit-round-3 fix #1 (400/401 branch): on upstream
    401 the router sets ``stale_reason`` BEFORE broadcasting, so the WS
    client receives a ``usage_updated`` frame with the stale reason set.
    This is the transition from "Waiting" to "Stale" on sibling tabs.
    """
    import httpx
    from backend.cache import cache as _cache

    app, client, SessionLocal = app_client
    _clear_ws_manager()

    email = "force-refresh-401-e2e@example.com"
    acct_id = _seed_account(SessionLocal, email, "/tmp/force-refresh-401-e2e-dir")
    asyncio.run(_cache.set_waiting(email))

    resp_mock = MagicMock()
    resp_mock.status_code = 401
    http_err = httpx.HTTPStatusError(
        "401", request=MagicMock(), response=resp_mock
    )

    with patch(
        "backend.routers.accounts.ac.force_refresh_config_dir",
        new_callable=AsyncMock,
        side_effect=http_err,
    ), patch(
        "backend.routers.accounts.ac.get_active_email_async",
        new_callable=AsyncMock,
        return_value=email,
    ):
        with client.websocket_connect("/ws") as ws:
            resp = client.post(f"/api/accounts/{acct_id}/force-refresh")
            assert resp.status_code == 409
            msg = ws.receive_json()

    assert msg["type"] == "usage_updated"
    entry = msg["accounts"][0]
    assert entry["email"] == email
    assert entry["stale_reason"] == "Refresh token revoked — re-login required"
    # Waiting must be False because the account is now stale — the P2.1 fix
    # that added the stale gate to _broadcast_single_account matters here:
    # without it the payload would contradict itself (waiting=True AND stale
    # set), and the frontend isWaiting = !isStale && … short-circuit would
    # mask the bug until a client removed that compensator.
    assert entry["waiting_for_cli"] is False

    asyncio.run(_cache.invalidate(email))


# ── verify_relogin → usage_updated broadcast (fix P2.2) ──────────────────────


def test_verify_relogin_success_broadcast_reaches_ws_client(app_client):
    """Regression for audit-round-3 fix P2.2: ``verify_relogin`` now
    broadcasts a ``usage_updated`` frame on the health-flip so sibling tabs
    see the stale → healthy transition immediately instead of waiting up
    to ~15s for the next poll cycle.
    """
    from backend.cache import cache as _cache

    app, client, SessionLocal = app_client
    _clear_ws_manager()

    email = "relogin-broadcast-e2e@example.com"

    # Seed as STALE so verify_relogin's success path clears the stale_reason.
    async def _go():
        async with SessionLocal() as session:
            acc = Account(
                email=email,
                config_dir="/tmp/relogin-broadcast-e2e-dir",
                priority=0,
                threshold_pct=95,
                enabled=True,
                stale_reason="Refresh token revoked — re-login required",
            )
            session.add(acc)
            await session.commit()
            await session.refresh(acc)
            return acc.id

    acct_id = asyncio.run(_go())

    with patch(
        "backend.routers.accounts.ls.verify_login_session",
        return_value={
            "success": True,
            "email": email,
            "config_dir": "/tmp/relogin-broadcast-e2e-dir",
        },
    ), patch(
        "backend.routers.accounts.ac.get_active_email_async",
        new_callable=AsyncMock,
        # A DIFFERENT account is active, so perform_sync_to_targets is not
        # invoked and we don't need to mock the whole mirror pipeline.
        return_value="someone-else@example.com",
    ):
        with client.websocket_connect("/ws") as ws:
            resp = client.post(
                f"/api/accounts/{acct_id}/relogin/verify?session_id=relogin-test"
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["success"] is True
            assert body["email"] == email

            msg = ws.receive_json()

    assert msg["type"] == "usage_updated", (
        "verify_relogin success must broadcast a usage_updated frame so "
        "sibling tabs see the health flip without waiting for the next "
        "poll cycle — audit-round-3 fix P2.2"
    )
    entry = msg["accounts"][0]
    assert entry["email"] == email
    assert entry["stale_reason"] is None, (
        "The broadcast must carry the freshly-cleared stale_reason so the "
        "frontend flips the card from red-stale to healthy immediately"
    )

    asyncio.run(_cache.invalidate(email))


# ── account_deleted broadcast (sanity check for the existing contract) ──────


def test_delete_account_broadcast_reaches_ws_client(app_client):
    """Sanity check for the pre-existing account_deleted broadcast: the
    delete_account handler fires {"type": "account_deleted", "id": …} and
    ws.js filters ``state.accounts`` to remove the card.  This test pins
    the wire format so a refactor of the broadcast helper that drops the
    ``id`` field would be caught immediately."""
    from backend.cache import cache as _cache

    app, client, SessionLocal = app_client
    _clear_ws_manager()

    email = "delete-broadcast-e2e@example.com"
    acct_id = _seed_account(SessionLocal, email, "/tmp/delete-broadcast-e2e-dir")

    with patch(
        "backend.routers.accounts.ac.get_active_email_async",
        new_callable=AsyncMock,
        return_value=None,
    ):
        with client.websocket_connect("/ws") as ws:
            resp = client.delete(f"/api/accounts/{acct_id}")
            assert resp.status_code == 204
            msg = ws.receive_json()

    assert msg["type"] == "account_deleted"
    assert msg["id"] == acct_id

    asyncio.run(_cache.invalidate(email))
