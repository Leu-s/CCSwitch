"""Accounts router tests — updated for the isolated-config-dir schema."""
import asyncio
import pytest
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from unittest.mock import patch, AsyncMock, MagicMock

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


def test_switch_log_enriches_from_and_to_emails(client):
    """Regression: /api/accounts/log must resolve from_email/to_email on the
    server so the frontend does not have to rely on a live state.accounts
    lookup that can race with a WS-driven reload and render raw '#<id>'."""
    from backend.models import SwitchLog
    from datetime import datetime, timezone

    engine = create_async_engine(TEST_DB_URL, echo=False)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _seed():
        async with SessionLocal() as session:
            acc_a = Account(email="log-from@example.com", config_dir="/tmp/log-from", priority=10)
            acc_b = Account(email="log-to@example.com",   config_dir="/tmp/log-to",   priority=11)
            session.add_all([acc_a, acc_b])
            await session.commit()
            await session.refresh(acc_a)
            await session.refresh(acc_b)
            session.add(SwitchLog(
                from_account_id=acc_a.id,
                to_account_id=acc_b.id,
                reason="threshold",
                triggered_at=datetime.now(timezone.utc),
            ))
            # Second row: from is NULL (first ever switch)
            session.add(SwitchLog(
                from_account_id=None,
                to_account_id=acc_b.id,
                reason="manual",
                triggered_at=datetime.now(timezone.utc),
            ))
            await session.commit()
            return acc_a.id, acc_b.id

    from_id, to_id = asyncio.run(_seed())

    resp = client.get("/api/accounts/log?limit=10")
    assert resp.status_code == 200
    rows = resp.json()

    pair = next(r for r in rows if r["from_account_id"] == from_id and r["to_account_id"] == to_id)
    assert pair["from_email"] == "log-from@example.com"
    assert pair["to_email"] == "log-to@example.com"

    null_from = next(r for r in rows if r["from_account_id"] is None and r["to_account_id"] == to_id)
    assert null_from["from_email"] is None
    assert null_from["to_email"] == "log-to@example.com"


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


def test_capture_login_session_returns_output(client):
    """GET /login-sessions/{sid}/capture returns terminal output from the pane."""
    import backend.services.login_session_service as ls
    session_id = "capt0001"
    ls._active_login_sessions[session_id] = {
        "created_at": 1.0,
        "pane_target": "add-accounts:1.0",
        "config_dir": "/tmp/capt0001",
        "kind": "add",
    }
    try:
        with patch(
            "backend.routers.accounts.tmux_service.capture_pane",
            new_callable=AsyncMock,
            return_value="hello tmux output",
        ):
            resp = client.get(f"/api/accounts/login-sessions/{session_id}/capture")
        assert resp.status_code == 200
        assert resp.json() == {"output": "hello tmux output"}
    finally:
        ls._active_login_sessions.pop(session_id, None)


def test_capture_login_session_unknown_id_returns_404(client):
    resp = client.get("/api/accounts/login-sessions/nonexistent/capture")
    assert resp.status_code == 404


def test_send_to_login_session_calls_tmux(client):
    """POST /login-sessions/{sid}/send forwards text to tmux_service.send_keys."""
    import backend.services.login_session_service as ls
    session_id = "send0001"
    ls._active_login_sessions[session_id] = {
        "created_at": 1.0,
        "pane_target": "add-accounts:2.0",
        "config_dir": "/tmp/send0001",
        "kind": "add",
    }
    try:
        with patch(
            "backend.routers.accounts.tmux_service.send_keys",
            new_callable=AsyncMock,
        ) as mock_send:
            resp = client.post(
                f"/api/accounts/login-sessions/{session_id}/send",
                json={"text": "hello there"},
            )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        mock_send.assert_called_once()
        args, kwargs = mock_send.call_args
        assert args[0] == "add-accounts:2.0"
        assert args[1] == "hello there"
        assert kwargs.get("press_enter") is True
    finally:
        ls._active_login_sessions.pop(session_id, None)


def test_send_to_login_session_unknown_id_returns_404(client):
    resp = client.post(
        "/api/accounts/login-sessions/nonexistent/send",
        json={"text": "x"},
    )
    assert resp.status_code == 404


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


# ── Re-login flow ──────────────────────────────────────────────────────────────

def _insert_stale_account(email: str, config_dir: str, stale_reason: str) -> int:
    """Helper: drop an account row with stale_reason set and return its id."""
    engine = create_async_engine(TEST_DB_URL, echo=False)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _go():
        async with SessionLocal() as session:
            acc = Account(email=email, config_dir=config_dir, priority=80, stale_reason=stale_reason)
            session.add(acc)
            await session.commit()
            await session.refresh(acc)
            return acc.id

    return asyncio.run(_go())


def test_relogin_endpoint_opens_session(client):
    """POST /api/accounts/{id}/relogin returns a LoginSessionOut and forwards
    the account's existing config_dir to start_relogin_session — not a fresh one."""
    account_id = _insert_stale_account(
        "relogin-open@example.com",
        "/tmp/relogin-open-dir",
        "Refresh token revoked — re-login required",
    )
    mock_info = {
        "session_id": "rel00001",
        "config_dir": "/tmp/relogin-open-dir",
        "instructions": "Re-authenticate in the terminal below. After login completes, click 'Verify & Re-login'.",
    }
    with patch(
        "backend.services.login_session_service.start_relogin_session",
        return_value=mock_info,
    ) as mock_start:
        resp = client.post(f"/api/accounts/{account_id}/relogin")

    assert resp.status_code == 200
    assert resp.json()["session_id"] == "rel00001"
    # Critical: the service was called with the EXISTING config_dir, so the
    # stale slot is reused instead of creating a new throwaway dir.
    mock_start.assert_called_once_with("/tmp/relogin-open-dir")


def test_relogin_endpoint_404_for_unknown_account(client):
    resp = client.post("/api/accounts/999999/relogin")
    assert resp.status_code == 404


def test_relogin_endpoint_409_when_session_already_active(client):
    """A second concurrent re-login for the same slot must be rejected 409."""
    account_id = _insert_stale_account(
        "relogin-dup@example.com",
        "/tmp/relogin-dup-dir",
        "Anthropic API returned 401 — re-login required",
    )
    with patch(
        "backend.services.login_session_service.start_relogin_session",
        side_effect=ValueError("A re-login session is already active for this account"),
    ):
        resp = client.post(f"/api/accounts/{account_id}/relogin")
    assert resp.status_code == 409
    assert "already active" in resp.json()["detail"]


def test_relogin_verify_email_match_clears_stale(client):
    """Happy path: verify returns success, stale_reason goes away."""
    account_id = _insert_stale_account(
        "relogin-match@example.com",
        "/tmp/relogin-match-dir",
        "Anthropic API returned 401 — re-login required",
    )
    mock_result = {
        "success": True,
        "email": "relogin-match@example.com",
        "config_dir": "/tmp/relogin-match-dir",
        "kind": "relogin",
    }
    with patch(
        "backend.services.login_session_service.verify_login_session",
        return_value=mock_result,
    ), patch(
        "backend.services.account_service.get_active_email",
        return_value="someone-else@example.com",
    ), patch(
        "backend.services.account_service.get_token_info",
        return_value={},
    ), patch(
        "backend.ws.ws_manager.broadcast",
        new_callable=AsyncMock,
    ):
        resp = client.post(
            f"/api/accounts/{account_id}/relogin/verify?session_id=rel00002"
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["email"] == "relogin-match@example.com"

    # Re-fetch the row to confirm stale_reason was cleared in the DB.
    with patch("backend.services.account_service.get_active_email", return_value=None), \
         patch("backend.services.account_service.get_token_info", return_value={}):
        list_resp = client.get("/api/accounts")
    row = next(a for a in list_resp.json() if a["id"] == account_id)
    assert row["stale_reason"] is None


def test_relogin_verify_email_mismatch_wipes_and_keeps_stale(client):
    """If the user logs in as a different email, the new creds must be
    wiped via credential_provider.wipe_credentials_for_config_dir AND the
    account's stale_reason must NOT be cleared."""
    account_id = _insert_stale_account(
        "relogin-mismatch@example.com",
        "/tmp/relogin-mismatch-dir",
        "Refresh token revoked — re-login required",
    )
    mock_result = {
        "success": True,
        "email": "someone-else@example.com",  # user logged in wrong
        "config_dir": "/tmp/relogin-mismatch-dir",
        "kind": "relogin",
    }
    with patch(
        "backend.services.login_session_service.verify_login_session",
        return_value=mock_result,
    ), patch(
        "backend.routers.accounts.credential_provider.wipe_credentials_for_config_dir",
    ) as mock_wipe, patch(
        "backend.services.account_service.get_active_email",
        return_value=None,
    ), patch(
        "backend.services.account_service.get_token_info",
        return_value={},
    ):
        resp = client.post(
            f"/api/accounts/{account_id}/relogin/verify?session_id=rel00003"
        )

    assert resp.status_code == 200  # Pydantic response, not HTTP error
    body = resp.json()
    assert body["success"] is False
    assert "someone-else@example.com" in body["error"]
    assert "relogin-mismatch@example.com" in body["error"]

    # Wipe was called with the stale slot's config dir.
    mock_wipe.assert_called_once_with("/tmp/relogin-mismatch-dir")

    # stale_reason must still be set (we did not heal the slot).
    with patch("backend.services.account_service.get_active_email", return_value=None), \
         patch("backend.services.account_service.get_token_info", return_value={}):
        list_resp = client.get("/api/accounts")
    row = next(a for a in list_resp.json() if a["id"] == account_id)
    assert row["stale_reason"] is not None


def test_relogin_verify_404_when_account_deleted_midflow(client):
    """If the account was deleted between /relogin and /relogin/verify, the
    router must 404 AND clean up the orphaned session in the tracking dict."""
    with patch(
        "backend.services.login_session_service.cleanup_login_session",
    ) as mock_cleanup:
        resp = client.post(
            "/api/accounts/987654/relogin/verify?session_id=rel00099"
        )

    assert resp.status_code == 404
    mock_cleanup.assert_called_once_with("rel00099")


def test_relogin_verify_propagates_service_error(client):
    """verify_login_session returning success=False (e.g. token not detected
    yet) must be bubbled through as a LoginVerifyResult without raising."""
    account_id = _insert_stale_account(
        "relogin-pending@example.com",
        "/tmp/relogin-pending-dir",
        "Refresh token revoked — re-login required",
    )
    mock_result = {
        "success": False,
        "error": "Login not detected yet — .claude.json not found or missing email",
    }
    with patch(
        "backend.services.login_session_service.verify_login_session",
        return_value=mock_result,
    ), patch(
        "backend.services.account_service.get_active_email",
        return_value=None,
    ), patch(
        "backend.services.account_service.get_token_info",
        return_value={},
    ):
        resp = client.post(
            f"/api/accounts/{account_id}/relogin/verify?session_id=rel00004"
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is False
    assert "Login not detected" in body["error"]


def test_relogin_verify_active_account_triggers_sync(client):
    """When the revived account is the currently active one, the router must
    re-run the mirror pipeline (perform_sync_to_targets) so legacy Keychain
    + ~/.claude/ + credential targets pick up the new tokens."""
    account_id = _insert_stale_account(
        "relogin-active@example.com",
        "/tmp/relogin-active-dir",
        "Refresh token revoked — re-login required",
    )
    mock_result = {
        "success": True,
        "email": "relogin-active@example.com",
        "config_dir": "/tmp/relogin-active-dir",
        "kind": "relogin",
    }
    with patch(
        "backend.services.login_session_service.verify_login_session",
        return_value=mock_result,
    ), patch(
        "backend.services.account_service.get_active_email",
        return_value="relogin-active@example.com",  # this IS the active account
    ), patch(
        "backend.services.account_service.get_token_info",
        return_value={},
    ), patch(
        "backend.routers.accounts.sw.perform_sync_to_targets",
        new_callable=AsyncMock,
    ) as mock_sync:
        resp = client.post(
            f"/api/accounts/{account_id}/relogin/verify?session_id=rel00005"
        )

    assert resp.status_code == 200
    assert resp.json()["success"] is True
    mock_sync.assert_awaited_once()


# ── Force-refresh endpoint (E2 / Phase 2) ─────────────────────────────────────

def _insert_healthy_account(email: str, config_dir: str) -> int:
    """Helper: drop a non-stale account row and return its id."""
    engine = create_async_engine(TEST_DB_URL, echo=False)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _go():
        async with SessionLocal() as session:
            acc = Account(email=email, config_dir=config_dir, priority=85)
            session.add(acc)
            await session.commit()
            await session.refresh(acc)
            return acc.id

    return asyncio.run(_go())


def test_force_refresh_success_clears_waiting_and_broadcasts(client):
    """Happy path: POST /force-refresh returns 200, clears the cache waiting
    flag, and fires a single-account usage_updated broadcast so the UI flips
    out of waiting state immediately."""
    from backend.cache import cache

    account_id = _insert_healthy_account(
        "force-refresh-happy@example.com",
        "/tmp/force-refresh-happy-dir",
    )

    # Seed the waiting flag so we can verify it is cleared by the success path.
    asyncio.run(cache.set_waiting("force-refresh-happy@example.com"))

    fresh_token_info = {
        "token_expires_at": 9999999999999,
        "subscription_type": "max",
    }

    with patch(
        "backend.routers.accounts.ac.force_refresh_config_dir",
        new_callable=AsyncMock,
        return_value=fresh_token_info,
    ) as mock_force, patch(
        "backend.ws.ws_manager.broadcast",
        new_callable=AsyncMock,
    ) as mock_broadcast:
        resp = client.post(f"/api/accounts/{account_id}/force-refresh")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    mock_force.assert_awaited_once_with("/tmp/force-refresh-happy-dir")

    # The cache's waiting flag must be cleared so a follow-up GET /api/accounts
    # does NOT still say waiting=True.
    assert asyncio.run(
        cache.is_waiting_async("force-refresh-happy@example.com")
    ) is False

    # A single-account WS broadcast must have fired so other tabs flip out
    # of the waiting state without waiting for the next poll cycle.
    assert mock_broadcast.await_count >= 1
    payload = mock_broadcast.await_args_list[0][0][0]
    assert payload["type"] == "usage_updated"
    # Single-account payload — exactly one entry for this account.
    assert len(payload["accounts"]) == 1
    entry = payload["accounts"][0]
    assert entry["email"] == "force-refresh-happy@example.com"
    assert entry["waiting_for_cli"] is False
    assert entry["stale_reason"] is None

    asyncio.run(cache.invalidate("force-refresh-happy@example.com"))


def test_force_refresh_401_marks_stale_and_returns_409(client):
    """Upstream 401 on refresh means the refresh_token is revoked.  The
    router must mark the account stale with the same wording the poll loop
    uses for the same status code, fire a WS broadcast so other tabs see
    the transition, and return 409 to the caller."""
    import httpx
    from backend.cache import cache

    account_id = _insert_healthy_account(
        "force-refresh-401@example.com",
        "/tmp/force-refresh-401-dir",
    )

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
        "backend.ws.ws_manager.broadcast",
        new_callable=AsyncMock,
    ) as mock_broadcast:
        resp = client.post(f"/api/accounts/{account_id}/force-refresh")

    assert resp.status_code == 409
    # Must match the poll-loop wording so the UI shows a consistent message
    # regardless of which code path marked the account stale.
    assert resp.json()["detail"] == "Refresh token revoked — re-login required"

    # The account row must now carry the stale reason.
    with patch("backend.services.account_service.get_active_email", return_value=None), \
         patch("backend.services.account_service.get_token_info", return_value={}):
        list_resp = client.get("/api/accounts")
    row = next(a for a in list_resp.json() if a["id"] == account_id)
    assert row["stale_reason"] == "Refresh token revoked — re-login required"

    # A broadcast must have fired on the stale-transition path so other tabs
    # flip from Force-refresh button to Re-login button immediately.
    assert mock_broadcast.await_count >= 1
    payload = mock_broadcast.await_args_list[0][0][0]
    assert payload["type"] == "usage_updated"
    entry = payload["accounts"][0]
    assert entry["stale_reason"] == "Refresh token revoked — re-login required"

    asyncio.run(cache.invalidate("force-refresh-401@example.com"))


def test_force_refresh_already_stale_returns_409_without_upstream_call(client):
    """Double-click / retry protection: if the account row already has a
    stale_reason set (e.g. a prior force-refresh just failed), the router
    must 409 WITHOUT calling the upstream refresh helper at all — otherwise
    a second click could burn a second refresh token."""
    account_id = _insert_stale_account(
        "force-refresh-already-stale@example.com",
        "/tmp/force-refresh-already-stale-dir",
        "Refresh token rejected (400) — re-login required",
    )

    with patch(
        "backend.routers.accounts.ac.force_refresh_config_dir",
        new_callable=AsyncMock,
    ) as mock_force:
        resp = client.post(f"/api/accounts/{account_id}/force-refresh")

    assert resp.status_code == 409
    # The pre-check must fire BEFORE force_refresh_config_dir is called.
    mock_force.assert_not_called()
    # Detail should surface the existing stale_reason so the UI can toast it.
    assert "Refresh token rejected (400)" in resp.json()["detail"]


# ── E2 audit-round-2 regression tests ────────────────────────────────────────
#
# The tests below guard invariants that were added during the second-round
# audit pass: the is_active gate on the single-account broadcast, the
# JSON-parse-error → 502 mapping in anthropic_api, the commit-failure
# ordering in the 400/401 bookkeeping block, concurrency of the per-cfg-dir
# force-refresh lock, and the is_active gate in build_ws_snapshot.


def test_force_refresh_malformed_json_response_returns_502(client):
    """Regression for the second-round audit finding: when Anthropic returns
    200 with an unparseable body, ``refresh_access_token`` must raise
    ``RuntimeError`` (not the raw ``json.JSONDecodeError``/``ValueError``) so
    the router maps it to 502 "upstream malformed" — NOT 409 "re-login
    required", which would be the semantically wrong answer for a transient
    upstream hiccup."""
    import json
    import httpx
    from backend.cache import cache
    from backend.services import anthropic_api

    account_id = _insert_healthy_account(
        "force-refresh-bad-json@example.com",
        "/tmp/force-refresh-bad-json-dir",
    )

    async def _fake_post(*_a, **_kw):
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock(return_value=None)
        # json() raises json.JSONDecodeError (a ValueError subclass) on bad
        # bodies — same as httpx's actual behaviour for an empty response.
        resp.json = MagicMock(
            side_effect=json.JSONDecodeError("Expecting value", "", 0)
        )
        return resp

    class _FakeClient:
        def __init__(self, *_a, **_kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            return False
        post = AsyncMock(side_effect=_fake_post)

    with patch.object(anthropic_api.httpx, "AsyncClient", _FakeClient), \
         patch(
             "backend.services.account_service.get_refresh_token_from_config_dir",
             return_value="rt-bad-json",
         ), \
         patch(
             "backend.services.account_service.save_refreshed_token",
         ), \
         patch(
             "backend.services.account_service.get_token_info",
             return_value={},
         ), \
         patch(
             "backend.ws.ws_manager.broadcast",
             new_callable=AsyncMock,
         ):
        resp = client.post(f"/api/accounts/{account_id}/force-refresh")

    # Malformed upstream → 502, NOT 409.  The whole point of the fix is that
    # a raw JSONDecodeError does NOT leak to the router as a ValueError and
    # mis-route as "re-login required".
    assert resp.status_code == 502
    assert "not valid JSON" in resp.json()["detail"]

    asyncio.run(cache.invalidate("force-refresh-bad-json@example.com"))


def test_force_refresh_401_commit_failure_still_clears_cache_waiting():
    """Regression for the second-round audit finding: in the 400/401
    bookkeeping block, cache ops must run BEFORE ``db.commit`` so a commit
    failure (e.g. SQLite 'database is locked') cannot leave a ghost waiting
    flag in the cache pointing at an uncommitted DB row.  Without this
    ordering, the next ``GET /api/accounts`` sees ``stale_reason=None`` in
    the DB but ``_waiting=True`` in the cache, and the card renders the
    waiting pill forever.

    Invokes the async router handler directly with a mock DB so we can
    make ``commit`` raise without upsetting SQLAlchemy's greenlet machinery.
    """
    import httpx
    from fastapi import HTTPException
    from backend.cache import cache
    from backend.routers.accounts import force_refresh_account

    email = "force-refresh-commit-fail@example.com"

    # Seed the waiting flag — this is the state the user saw before clicking.
    asyncio.run(cache.set_waiting(email))
    assert asyncio.run(cache.is_waiting_async(email)) is True

    account = MagicMock()
    account.id = 42
    account.email = email
    account.stale_reason = None
    account.config_dir = "/tmp/force-refresh-commit-fail-dir"

    # DB that fails on commit but succeeds on rollback (the except-branch
    # fallback).  execute is not called because we patch the account lookup.
    mock_db = MagicMock()
    mock_db.commit = AsyncMock(side_effect=RuntimeError("database is locked"))
    mock_db.rollback = AsyncMock()

    resp_mock = MagicMock()
    resp_mock.status_code = 401
    http_err = httpx.HTTPStatusError(
        "401", request=MagicMock(), response=resp_mock
    )

    with patch(
        "backend.routers.accounts.aq.get_account_by_id",
        new_callable=AsyncMock,
        return_value=account,
    ), patch(
        "backend.routers.accounts.ac.force_refresh_config_dir",
        new_callable=AsyncMock,
        side_effect=http_err,
    ), patch(
        "backend.routers.accounts.ws_manager.broadcast",
        new_callable=AsyncMock,
    ):
        raised = None
        try:
            asyncio.run(force_refresh_account(42, mock_db))
        except HTTPException as e:
            raised = e

    # The 409 must still surface to the caller — a commit hiccup does not
    # downgrade the terminal refresh error to 500.
    assert raised is not None, "force_refresh_account must raise HTTPException on 401"
    assert raised.status_code == 409

    # The router MUST have attempted a rollback on the commit failure so
    # the dirty in-memory attribute does not leak into a subsequent request.
    mock_db.rollback.assert_awaited_once()

    # CRITICAL: the cache waiting flag must be cleared even though commit
    # failed, or the next GET /api/accounts will render the waiting pill
    # against a clean DB row.
    assert asyncio.run(cache.is_waiting_async(email)) is False, (
        "cache _waiting flag leaked past the 400/401 branch when db.commit "
        "raised — reorder ops so cache clear runs BEFORE commit"
    )

    asyncio.run(cache.invalidate(email))


def test_force_refresh_401_clear_waiting_survives_invalidate_crash():
    """Regression for a concern surfaced in the re-audit of FIX-3: the two
    cache ops in the 400/401 bookkeeping block must be INDEPENDENT — a
    failure inside ``invalidate_token_info`` must NOT skip
    ``clear_waiting``, or the ghost-waiting leak returns by a different
    path (cache lock cancelled mid-acquire during shutdown, etc.)."""
    import httpx
    from fastapi import HTTPException
    from backend.cache import cache
    from backend.routers.accounts import force_refresh_account

    email = "force-refresh-chain-break@example.com"
    # Seed the waiting flag — the point of the test is verifying this gets
    # cleared even when its sibling op crashes.
    asyncio.run(cache.set_waiting(email))
    assert asyncio.run(cache.is_waiting_async(email)) is True

    account = MagicMock()
    account.id = 77
    account.email = email
    account.stale_reason = None
    account.config_dir = "/tmp/force-refresh-chain-break-dir"

    mock_db = MagicMock()
    mock_db.commit = AsyncMock()
    mock_db.rollback = AsyncMock()

    resp_mock = MagicMock()
    resp_mock.status_code = 401
    http_err = httpx.HTTPStatusError(
        "401", request=MagicMock(), response=resp_mock
    )

    # Make invalidate_token_info crash with a synthetic error.  The fix
    # requires clear_waiting to still run afterward in its own try block.
    async def _crash(_email):
        raise RuntimeError("synthetic cache-internal failure")

    with patch(
        "backend.routers.accounts.aq.get_account_by_id",
        new_callable=AsyncMock,
        return_value=account,
    ), patch(
        "backend.routers.accounts.ac.force_refresh_config_dir",
        new_callable=AsyncMock,
        side_effect=http_err,
    ), patch(
        "backend.routers.accounts.cache.invalidate_token_info",
        side_effect=_crash,
    ), patch(
        "backend.routers.accounts.ws_manager.broadcast",
        new_callable=AsyncMock,
    ):
        raised = None
        try:
            asyncio.run(force_refresh_account(77, mock_db))
        except HTTPException as e:
            raised = e

    assert raised is not None
    assert raised.status_code == 409

    # The critical assertion: clear_waiting ran in its own try even though
    # its sibling invalidate_token_info raised.  Coupling these would leak
    # the very ghost flag this fix is meant to prevent.
    assert asyncio.run(cache.is_waiting_async(email)) is False, (
        "clear_waiting was skipped because invalidate_token_info raised — "
        "the cache-clear ops in the 401 branch are coupled when they must "
        "be independent"
    )

    asyncio.run(cache.invalidate(email))


def test_broadcast_single_account_gates_waiting_by_is_active():
    """Regression for the second-round audit finding: _broadcast_single_account
    must gate ``waiting_for_cli`` by ``email == active_email`` just like
    ``list_accounts`` and ``build_ws_snapshot`` do.  Without this gate, a
    future caller that invokes the helper for a non-active account whose
    cache entry still carries a stale waiting flag would broadcast a
    contradictory payload to every connected tab."""
    from backend.routers.accounts import _broadcast_single_account
    from backend.cache import cache

    email = "broadcast-non-active@example.com"

    # Simulate leaked waiting flag for a non-active account.
    asyncio.run(cache.set_waiting(email))

    account = MagicMock()
    account.id = 99999
    account.email = email
    account.stale_reason = None

    captured: list[dict] = []

    class _FakeWsManager:
        async def broadcast(self, payload):
            captured.append(payload)

    with patch("backend.routers.accounts.ws_manager", _FakeWsManager()), \
         patch(
             "backend.routers.accounts.ac.get_active_email_async",
             new_callable=AsyncMock,
             # A DIFFERENT account is active — ours is not.
             return_value="someone-else@example.com",
         ):
        asyncio.run(_broadcast_single_account(account))

    assert len(captured) == 1
    entry = captured[0]["accounts"][0]
    assert entry["email"] == email
    # The critical assertion: defense-in-depth — even though cache._waiting
    # carries a stale True for this email, the broadcast MUST gate on is_active
    # and emit False, matching list_accounts / build_ws_snapshot.
    assert entry["waiting_for_cli"] is False

    asyncio.run(cache.invalidate(email))


def test_force_refresh_locks_serialize_concurrent_same_cfg_dir():
    """Regression for a coverage gap surfaced in the second-round audit:
    ``_force_refresh_locks`` serializes two coroutines racing on the same
    config_dir so they cannot both burn Anthropic's single-use refresh
    token simultaneously.  This is the single most important concurrency
    guarantee in the force-refresh feature and had no direct test before.
    """
    import time as _time
    import asyncio as _asyncio
    from backend.services import account_service as _ac

    call_log: list[tuple[str, float]] = []

    async def _slow_refresh(_rt):
        call_log.append(("start", _time.monotonic()))
        await _asyncio.sleep(0.05)
        call_log.append(("end", _time.monotonic()))
        return {
            "access_token": "new-token",
            "expires_in": 3600,
            "refresh_token": "rotated-rt",
        }

    async def _run():
        # Reset the lock dict so a stale entry from a previous test does not
        # interfere with this one's timing.
        _ac._force_refresh_locks.clear()
        await _asyncio.gather(
            _ac.force_refresh_config_dir("/tmp/serialize-cfg-a"),
            _ac.force_refresh_config_dir("/tmp/serialize-cfg-a"),
        )

    with patch(
        "backend.services.account_service.get_refresh_token_from_config_dir",
        return_value="rt",
    ), patch(
        "backend.services.account_service.save_refreshed_token",
    ), patch(
        "backend.services.account_service.get_token_info",
        return_value={"token_expires_at": 9999999999999},
    ), patch(
        "backend.services.anthropic_api.refresh_access_token",
        side_effect=_slow_refresh,
    ):
        asyncio.run(_run())

    # Two refreshes = two (start, end) pairs.
    starts = [t for ev, t in call_log if ev == "start"]
    ends = [t for ev, t in call_log if ev == "end"]
    assert len(starts) == 2
    assert len(ends) == 2

    # Serialization: the second coroutine's start must be >= the first's end.
    first_end = min(ends)
    second_start = max(starts)
    assert second_start >= first_end - 1e-6, (
        "force_refresh_config_dir failed to serialize concurrent calls on "
        "the same config_dir — _force_refresh_locks is not working and two "
        "refreshes could burn the same single-use refresh_token"
    )


def test_force_refresh_locks_parallelize_different_cfg_dirs():
    """Counterpart to the serialization test: force-refresh calls for
    DIFFERENT config_dirs must NOT serialize (otherwise a slow Keychain on
    one account would block every other account's refresh)."""
    import time as _time
    import asyncio as _asyncio
    from backend.services import account_service as _ac

    call_log: list[tuple[str, str, float]] = []

    async def _slow_refresh(_rt):
        call_log.append(("start", _rt, _time.monotonic()))
        await _asyncio.sleep(0.05)
        call_log.append(("end", _rt, _time.monotonic()))
        return {
            "access_token": "new-token",
            "expires_in": 3600,
            "refresh_token": "rotated",
        }

    # Distinguish the refresh tokens so we can track which call is which.
    def _get_rt(cfg_dir):
        return f"rt-for-{cfg_dir.rsplit('-', 1)[-1]}"

    async def _run():
        _ac._force_refresh_locks.clear()
        await _asyncio.gather(
            _ac.force_refresh_config_dir("/tmp/parallel-cfg-a"),
            _ac.force_refresh_config_dir("/tmp/parallel-cfg-b"),
        )

    with patch(
        "backend.services.account_service.get_refresh_token_from_config_dir",
        side_effect=_get_rt,
    ), patch(
        "backend.services.account_service.save_refreshed_token",
    ), patch(
        "backend.services.account_service.get_token_info",
        return_value={"token_expires_at": 9999999999999},
    ), patch(
        "backend.services.anthropic_api.refresh_access_token",
        side_effect=_slow_refresh,
    ):
        asyncio.run(_run())

    # Both starts must appear BEFORE either end — they ran in parallel.
    starts = [t for ev, _rt, t in call_log if ev == "start"]
    ends = [t for ev, _rt, t in call_log if ev == "end"]
    assert len(starts) == 2 and len(ends) == 2
    # max(starts) < min(ends) means both started before either finished.
    assert max(starts) < min(ends), (
        "force_refresh_config_dir serialized two different config_dirs — "
        "the per-cfg-dir lock is over-scoped and one slow Keychain would "
        "block every account's refresh"
    )


def test_build_ws_snapshot_gates_waiting_by_is_active():
    """Regression for a coverage gap surfaced in the second-round audit:
    ``build_ws_snapshot`` gates ``waiting_for_cli`` by ``email ==
    active_email`` so a reconnecting tab cannot render a waiting banner on
    a card that is no longer the active one.  Without this test, a revert
    to the pre-gate code path ships silently."""
    from backend.services.account_service import build_ws_snapshot
    from backend.cache import cache

    active_email = "snap-active@example.com"
    inactive_email = "snap-inactive@example.com"

    # Seed cache: both accounts have usage data, both have waiting flags.
    # Only the active one should surface waiting=True in the snapshot.
    asyncio.run(cache.set_usage(active_email, {"five_hour": {"utilization": 10}}))
    asyncio.run(cache.set_usage(inactive_email, {"five_hour": {"utilization": 20}}))
    asyncio.run(cache.set_waiting(active_email))
    asyncio.run(cache.set_waiting(inactive_email))  # ← the leaked stale flag

    # Mock DB: email→id map has both; stale_reason query returns both.
    mock_db = AsyncMock()

    def _make_result_for(query_str):
        if "Account" in query_str and "stale_reason" in query_str:
            r = MagicMock()
            r.all.return_value = [(active_email, None), (inactive_email, None)]
            return r
        r = MagicMock()
        r.scalars.return_value.all.return_value = []
        return r

    async def _execute_side_effect(query):
        return _make_result_for(str(query))

    mock_db.execute = AsyncMock(side_effect=_execute_side_effect)

    with patch(
        "backend.services.account_queries.get_email_to_id_map",
        new_callable=AsyncMock,
        return_value={active_email: 1, inactive_email: 2},
    ), patch(
        "backend.services.account_service.get_active_email_async",
        new_callable=AsyncMock,
        return_value=active_email,
    ):
        snapshot = asyncio.run(build_ws_snapshot(mock_db))

    by_email = {e["email"]: e for e in snapshot}
    assert by_email[active_email]["waiting_for_cli"] is True, (
        "active account with cache _waiting flag must surface waiting=True"
    )
    assert by_email[inactive_email]["waiting_for_cli"] is False, (
        "non-active account with cache _waiting flag must surface waiting=False "
        "— the is_active gate is missing from build_ws_snapshot"
    )

    asyncio.run(cache.invalidate(active_email))
    asyncio.run(cache.invalidate(inactive_email))
