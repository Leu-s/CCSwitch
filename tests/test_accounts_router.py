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
