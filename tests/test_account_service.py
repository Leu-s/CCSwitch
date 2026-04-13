"""
Tests for backend.services.account_service.

Focuses on the login-session lifecycle helpers:
  _cleanup_expired_sessions, cleanup_login_session
"""
import pytest
from unittest.mock import patch


# ── _cleanup_expired_sessions ──────────────────────────────────────────────────

def test_cleanup_removes_expired_sessions():
    """Sessions created more than _SESSION_TIMEOUT seconds ago are cleaned up."""
    import backend.services.account_service as svc

    session_id = "expired1"
    creation_time = 0.0  # far in the past

    # Inject directly into the module-level dict
    svc._active_login_sessions[session_id] = creation_time

    # time.time() returns SESSION_TIMEOUT + 1 beyond the creation time → expired
    fake_now = creation_time + svc._SESSION_TIMEOUT + 1

    with patch("backend.services.account_service.time") as mock_time, \
         patch("backend.services.account_service.shutil.rmtree") as mock_rmtree, \
         patch("backend.services.account_service.os.path.isdir", return_value=True):
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
    import backend.services.account_service as svc

    session_id = "fresh1"
    creation_time = 1000.0

    svc._active_login_sessions[session_id] = creation_time

    # time.time() is exactly at creation — 0 seconds elapsed → still fresh
    fake_now = creation_time

    with patch("backend.services.account_service.time") as mock_time, \
         patch("backend.services.account_service.shutil.rmtree") as mock_rmtree, \
         patch("backend.services.account_service.os.path.isdir", return_value=True):
        mock_time.time.return_value = fake_now

        svc._cleanup_expired_sessions()

    # Session must still be tracked
    assert session_id in svc._active_login_sessions
    # No filesystem removal should have happened
    mock_rmtree.assert_not_called()

    # Clean up the injected entry so it does not leak into other tests
    svc._active_login_sessions.pop(session_id, None)
