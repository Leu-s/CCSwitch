"""
Login session lifecycle helpers.

Manages the short-lived isolated config directories created when a user
authenticates a new Claude account via the dashboard UI.  Each session gets
a random 8-char ID, a corresponding account-{id}/ directory under the
accounts base dir, and a tmux window where `claude` runs with
CLAUDE_CONFIG_DIR pointing at that directory.

Sessions that are never verified are reaped automatically after
_SESSION_TIMEOUT seconds.
"""

import logging
import os
import shutil
import subprocess
import threading
import time
import uuid

from ..config import settings
from .credential_provider import get_access_token_from_config_dir

logger = logging.getLogger(__name__)

# session_id → creation timestamp (time.time())
_active_login_sessions: dict[str, float] = {}
# asyncio.to_thread dispatches to a pool with multiple worker threads, so
# start / verify / cleanup can touch the dict concurrently. The RLock is
# reentrant because _cleanup_expired_sessions iterates and then calls
# cleanup_login_session, which also needs the lock.
_sessions_lock = threading.RLock()

# Sessions older than this are considered expired — read from config for tunability.
_SESSION_TIMEOUT: int = settings.login_session_timeout


# ── Private path helpers (inlined to avoid circular imports) ──────────────────

def _accounts_base() -> str:
    return os.path.expanduser(settings.accounts_base_dir)


def _make_account_config_dir(session_id: str) -> str:
    path = os.path.join(_accounts_base(), f"account-{session_id}")
    os.makedirs(path, exist_ok=True)
    return path


def _get_email_from_config_dir(config_dir: str) -> str | None:
    """Return the emailAddress stored in .claude.json inside config_dir."""
    import json
    path = os.path.join(config_dir, ".claude.json")
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        data = {}
    return (data.get("oauthAccount") or {}).get("emailAddress")


# ── Session lifecycle ─────────────────────────────────────────────────────────

def _cleanup_expired_sessions() -> None:
    """Remove login session dirs for sessions that have exceeded _SESSION_TIMEOUT."""
    now = time.time()
    with _sessions_lock:
        expired = [sid for sid, created_at in _active_login_sessions.items()
                   if now - created_at > _SESSION_TIMEOUT]
    for sid in expired:
        cleanup_login_session(sid)
        logger.debug("Expired login session cleaned up: %s", sid)


def start_login_session() -> dict:
    """
    Create a fresh isolated config directory and open a tmux window where the
    user can run `claude` (with CLAUDE_CONFIG_DIR set) to authenticate.

    Returns session metadata including the tmux pane target.
    """
    # Reap any abandoned sessions before creating a new one so the in-memory
    # registry and the on-disk account dirs do not grow without bound.
    _cleanup_expired_sessions()

    session_id = str(uuid.uuid4())[:8]
    config_dir = _make_account_config_dir(session_id)

    # Track this session so _cleanup_expired_sessions can reap it if abandoned
    with _sessions_lock:
        _active_login_sessions[session_id] = time.time()

    # Ensure at least one tmux server/session is running so new-window works.
    # These two calls are best-effort setup; a hung tmux server that times out
    # here is logged and we fall through to new-window, which has check=True
    # and will surface any real problem to the caller as a 500.
    try:
        sessions = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True, timeout=10,
        )
        no_sessions = sessions.returncode != 0 or not sessions.stdout.strip()
    except subprocess.TimeoutExpired:
        logger.warning("tmux list-sessions timed out — falling through")
        no_sessions = True

    if no_sessions:
        try:
            subprocess.run(
                ["tmux", "new-session", "-d", "-s", settings.tmux_session_name],
                capture_output=True, text=True, timeout=10,
            )
        except subprocess.TimeoutExpired:
            logger.warning("tmux new-session timed out — falling through")

    result = subprocess.run(
        [
            "tmux", "new-window",
            "-P", "-F", "#{session_name}:#{window_index}.#{pane_index}",
            "-n", f"add-acct",
        ],
        capture_output=True, text=True, check=True, timeout=10,
    )
    pane_target = result.stdout.strip()

    # Launch claude with the isolated config dir
    subprocess.run(
        ["tmux", "send-keys", "-t", pane_target,
         f"CLAUDE_CONFIG_DIR={config_dir} claude", "Enter"],
        check=True, capture_output=True, timeout=10,
    )

    return {
        "session_id": session_id,
        "pane_target": pane_target,
        "config_dir": config_dir,
        "instructions": (
            "Authenticate in the terminal below. "
            "After login completes, click 'Verify & Save'."
        ),
    }


def verify_login_session(session_id: str) -> dict:
    """
    Verify that a login session completed successfully.
    Returns {"success": True, "email": "..."} or {"success": False, "error": "..."}.
    """
    config_dir = os.path.join(_accounts_base(), f"account-{session_id}")
    real = os.path.realpath(config_dir)
    base = os.path.realpath(_accounts_base())
    if not real.startswith(base + os.sep):
        return {"success": False, "error": "Invalid session ID"}
    if not os.path.isdir(config_dir):
        return {"success": False, "error": "Session not found"}

    email = _get_email_from_config_dir(config_dir)
    if not email:
        return {
            "success": False,
            "error": "Login not detected yet — .claude.json not found or missing email",
        }

    token = get_access_token_from_config_dir(config_dir)
    if not token:
        return {
            "success": False,
            "error": (
                "Credentials not found in the config directory. "
                "Make sure CLAUDE_CONFIG_DIR isolation is working."
            ),
        }

    # Successful verification — stop tracking this session so the registry
    # does not keep a pointer to a now-durable account dir.
    with _sessions_lock:
        _active_login_sessions.pop(session_id, None)
    return {"success": True, "email": email, "config_dir": config_dir}


def cleanup_login_session(session_id: str) -> None:
    """Remove a login session's config dir (called on cancel or expiry)."""
    with _sessions_lock:
        _active_login_sessions.pop(session_id, None)
    config_dir = os.path.join(_accounts_base(), f"account-{session_id}")
    real = os.path.realpath(config_dir)
    base = os.path.realpath(_accounts_base())
    if os.path.isdir(real) and real.startswith(base + os.sep):
        shutil.rmtree(real, ignore_errors=True)
