"""
Tests for the login-session lifecycle helpers in
backend.services.login_session_service.
"""
import pytest
from unittest.mock import patch


# ── _cleanup_expired_sessions ──────────────────────────────────────────────────

def test_cleanup_removes_expired_sessions():
    """Sessions created more than _SESSION_TIMEOUT seconds ago are cleaned up."""
    import backend.services.login_session_service as svc

    session_id = "expired1"
    creation_time = 0.0  # far in the past

    # The realpath+startswith guard in cleanup_login_session only rmtrees
    # paths that live inside accounts_base, so use a path under it.
    svc._active_login_sessions[session_id] = {
        "created_at": creation_time,
        "pane_target": "add-accounts:1.0",
        "config_dir": f"{svc._accounts_base()}/account-{session_id}",
        "kind": "add",
    }

    # time.time() returns SESSION_TIMEOUT + 1 beyond the creation time → expired
    fake_now = creation_time + svc._SESSION_TIMEOUT + 1

    with patch("backend.services.login_session_service.time") as mock_time, \
         patch("backend.services.login_session_service.shutil.rmtree") as mock_rmtree, \
         patch("backend.services.login_session_service.os.path.isdir", return_value=True):
        mock_time.time.return_value = fake_now

        svc._cleanup_expired_sessions()

    # Session must have been removed from the tracking dict
    assert session_id not in svc._active_login_sessions

    # The config dir must have been deleted
    mock_rmtree.assert_called_once()
    called_path = mock_rmtree.call_args[0][0]
    assert session_id in called_path


def test_cleanup_keeps_fresh_sessions():
    """Sessions created 0 seconds ago (just now) must not be cleaned up."""
    import backend.services.login_session_service as svc

    session_id = "fresh1"
    creation_time = 1000.0

    svc._active_login_sessions[session_id] = {
        "created_at": creation_time,
        "pane_target": "add-accounts:2.0",
        "config_dir": "/tmp/fake-account-fresh1",
        "kind": "add",
    }

    # time.time() is exactly at creation — 0 seconds elapsed → still fresh
    fake_now = creation_time

    with patch("backend.services.login_session_service.time") as mock_time, \
         patch("backend.services.login_session_service.shutil.rmtree") as mock_rmtree, \
         patch("backend.services.login_session_service.os.path.isdir", return_value=True):
        mock_time.time.return_value = fake_now

        svc._cleanup_expired_sessions()

    # Session must still be tracked
    assert session_id in svc._active_login_sessions
    # No filesystem removal should have happened
    mock_rmtree.assert_not_called()

    # Clean up the injected entry so it does not leak into other tests
    svc._active_login_sessions.pop(session_id, None)


def test_get_pane_target_returns_stored_target():
    """get_pane_target returns the pane_target for an active session."""
    import backend.services.login_session_service as svc

    session_id = "pane1"
    svc._active_login_sessions[session_id] = {
        "created_at": 100.0,
        "pane_target": "add-accounts:3.0",
        "config_dir": "/tmp/fake-account-pane1",
        "kind": "add",
    }
    try:
        assert svc.get_pane_target(session_id) == "add-accounts:3.0"
    finally:
        svc._active_login_sessions.pop(session_id, None)


def test_get_pane_target_returns_none_for_unknown_session():
    """get_pane_target returns None when the session is not tracked."""
    import backend.services.login_session_service as svc

    assert svc.get_pane_target("does-not-exist") is None


# ── cleanup_login_session: kind-aware config_dir handling ─────────────────────

def test_cleanup_add_session_deletes_config_dir():
    """kind="add" sessions own a throwaway dir under accounts_base — cleanup
    must rmtree it so abandoned enrolments do not accumulate on disk."""
    import backend.services.login_session_service as svc

    session_id = "addcln01"
    # Use a path inside the real accounts_base so the realpath/startswith
    # guard in cleanup_login_session passes.
    config_dir = f"{svc._accounts_base()}/account-{session_id}"
    svc._active_login_sessions[session_id] = {
        "created_at": 1.0,
        "pane_target": "add-accounts:9.0",
        "config_dir": config_dir,
        "kind": "add",
    }

    with patch("backend.services.login_session_service.shutil.rmtree") as mock_rmtree, \
         patch("backend.services.login_session_service.os.path.isdir", return_value=True):
        svc.cleanup_login_session(session_id)

    assert session_id not in svc._active_login_sessions
    mock_rmtree.assert_called_once()
    called_path = mock_rmtree.call_args[0][0]
    assert session_id in called_path


def test_cleanup_relogin_session_preserves_config_dir():
    """kind="relogin" sessions point at an existing account's config dir
    that MUST NOT be deleted on cleanup — the slot has to stay alive so
    the user can retry re-login."""
    import backend.services.login_session_service as svc

    session_id = "relcln01"
    svc._active_login_sessions[session_id] = {
        "created_at": 1.0,
        "pane_target": "add-accounts:10.0",
        "config_dir": "/Users/test/.ccswitch-accounts/account-real1",
        "kind": "relogin",
    }

    with patch("backend.services.login_session_service.shutil.rmtree") as mock_rmtree, \
         patch("backend.services.login_session_service.os.path.isdir", return_value=True):
        svc.cleanup_login_session(session_id)

    # Session removed from registry …
    assert session_id not in svc._active_login_sessions
    # … but the config dir was never touched.
    mock_rmtree.assert_not_called()


# ── start_relogin_session: duplicate-session guard ──────────────────────────

def test_start_relogin_session_rejects_duplicate_for_same_config_dir():
    """Two concurrent re-login sessions for the same config_dir would race
    the same Keychain entry — the service must reject the second one."""
    import backend.services.login_session_service as svc

    existing_sid = "rel0dup0"
    config_dir = "/Users/test/.ccswitch-accounts/account-dup1"
    svc._active_login_sessions[existing_sid] = {
        "created_at": 99999.0,  # far in the future so _cleanup_expired_sessions does not reap it
        "pane_target": "add-accounts:11.0",
        "config_dir": config_dir,
        "kind": "relogin",
    }

    try:
        with patch("backend.services.login_session_service.time") as mock_time:
            mock_time.time.return_value = 99999.0
            with pytest.raises(ValueError, match="already active"):
                svc.start_relogin_session(config_dir)
    finally:
        svc._active_login_sessions.pop(existing_sid, None)


def test_start_relogin_session_rejects_nonexistent_config_dir():
    """A config_dir that does not exist on disk cannot be re-logged into."""
    import backend.services.login_session_service as svc

    with pytest.raises(ValueError, match="does not exist"):
        svc.start_relogin_session("/tmp/definitely-not-a-real-path-xyz")


# ── verify_login_session: reads config_dir from the session dict ───────────

def test_verify_login_session_returns_kind():
    """verify_login_session must include "kind" in the success result so
    the router can branch between add-account save vs. re-login cleanup."""
    import backend.services.login_session_service as svc

    session_id = "verifyk1"
    # Point at a path that DOES NOT exist so verify returns
    # "config directory missing" early — we only care that the kind field
    # would have been propagated had the config dir been there.
    svc._active_login_sessions[session_id] = {
        "created_at": 1.0,
        "pane_target": "add-accounts:12.0",
        "config_dir": "/tmp/definitely-missing-verify-kind",
        "kind": "add",
    }
    try:
        result = svc.verify_login_session(session_id)
        # Config dir does not exist → should fail before reading email.
        assert result["success"] is False
        assert "missing" in result["error"].lower() or "invalid" in result["error"].lower()
    finally:
        svc._active_login_sessions.pop(session_id, None)


# ── credential_provider.wipe_credentials_for_config_dir ────────────────────

def test_wipe_credentials_removes_keychain_files_and_oauth_keys(tmp_path):
    """wipe_credentials_for_config_dir must:
      * call `security delete-generic-password` for the hashed Keychain entry
      * unlink .credentials.json / credentials.json if present
      * strip oauthAccount + userID from .claude.json, preserving other keys
    """
    import json as _json
    from backend.services import credential_provider as cp

    config_dir = tmp_path
    # Seed .credentials.json and .claude.json with oauthAccount + other keys.
    (config_dir / ".credentials.json").write_text('{"claudeAiOauth": {"accessToken": "bad"}}')
    (config_dir / ".claude.json").write_text(_json.dumps({
        "oauthAccount": {"emailAddress": "bob@bad.com"},
        "userID": "abc",
        "projects": {"foo": {"trust": "yes"}},
        "mcpServers": {},
    }))

    with patch("backend.services.credential_provider.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        cp.wipe_credentials_for_config_dir(str(config_dir))

    # Keychain delete was called at least once with delete-generic-password.
    assert any(
        call.args and call.args[0][:2] == ["security", "delete-generic-password"]
        for call in mock_run.call_args_list
    )

    # Credential file is gone.
    assert not (config_dir / ".credentials.json").exists()

    # .claude.json was rewritten WITHOUT oauthAccount / userID, but
    # every other key survived.
    remaining = _json.loads((config_dir / ".claude.json").read_text())
    assert "oauthAccount" not in remaining
    assert "userID" not in remaining
    assert remaining["projects"] == {"foo": {"trust": "yes"}}
    assert remaining["mcpServers"] == {}


def test_wipe_credentials_is_noop_when_claude_json_has_no_oauth(tmp_path):
    """If .claude.json exists but has no oauthAccount/userID, wipe should
    not rewrite it (nothing to strip) and must not raise."""
    import json as _json
    from backend.services import credential_provider as cp

    config_dir = tmp_path
    payload = {"projects": {"a": 1}}
    (config_dir / ".claude.json").write_text(_json.dumps(payload))

    with patch("backend.services.credential_provider.subprocess.run"):
        cp.wipe_credentials_for_config_dir(str(config_dir))

    # File untouched.
    assert _json.loads((config_dir / ".claude.json").read_text()) == payload
