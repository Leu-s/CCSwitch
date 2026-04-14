"""
Login session lifecycle helpers.

Two kinds of sessions live in the same tracking dict:

* ``kind="add"`` — brand-new account enrollment.  ``start_login_session``
  creates a throwaway isolated config directory under ``accounts_base``,
  launches a tmux window running ``claude`` with ``CLAUDE_CONFIG_DIR``
  pointing at that dir, and on cleanup / expiry the throwaway dir is
  rm-r-ed.

* ``kind="relogin"`` — re-authentication of an existing account whose
  credentials have gone stale.  ``start_relogin_session`` reuses the
  account's existing config directory (so the email, priority, threshold,
  and credential-target mappings all stay intact), launches the same kind
  of tmux window, and on cleanup / expiry the config dir is LEFT ALONE.
  The slot is preserved.

Sessions that are never verified are reaped automatically after
``_SESSION_TIMEOUT`` seconds.
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

# session_id → {
#   "created_at": float,
#   "pane_target": str,
#   "config_dir": str,
#   "kind": str,          # "add" | "relogin"
# }
_active_login_sessions: dict[str, dict] = {}
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


# ── Tmux helper (shared by add + relogin) ─────────────────────────────────────

def _open_claude_tmux_window(config_dir: str) -> str:
    """Ensure tmux is running and launch a new window with ``claude`` inside
    it, with ``CLAUDE_CONFIG_DIR`` pointing at ``config_dir``.  Returns the
    new pane's target string (``session:window.pane``).

    Shared by both enrollment and re-login so the exact same tmux setup,
    error handling, and timeouts apply to both flows.  The caller is
    responsible for recording the returned pane_target in
    ``_active_login_sessions`` under the right kind.
    """
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
            "-n", "add-acct",
        ],
        capture_output=True, text=True, check=True, timeout=10,
    )
    pane_target = result.stdout.strip()

    # Launch claude with the isolated config dir.
    subprocess.run(
        ["tmux", "send-keys", "-t", pane_target,
         f"CLAUDE_CONFIG_DIR={config_dir} claude", "Enter"],
        check=True, capture_output=True, timeout=10,
    )

    return pane_target


# ── Session lifecycle ─────────────────────────────────────────────────────────

def _cleanup_expired_sessions() -> None:
    """Remove login session dirs for sessions that have exceeded _SESSION_TIMEOUT."""
    now = time.time()
    with _sessions_lock:
        expired = [sid for sid, data in _active_login_sessions.items()
                   if now - data["created_at"] > _SESSION_TIMEOUT]
    for sid in expired:
        cleanup_login_session(sid)
        logger.debug("Expired login session cleaned up: %s", sid)


def get_pane_target(session_id: str) -> str | None:
    """Return the tmux pane target for an active login session, or None."""
    with _sessions_lock:
        data = _active_login_sessions.get(session_id)
    return data["pane_target"] if data else None


def start_login_session() -> dict:
    """
    Create a fresh isolated config directory and open a tmux window where the
    user can run `claude` (with CLAUDE_CONFIG_DIR set) to authenticate.

    Returns session metadata including the pane target.
    """
    # Reap any abandoned sessions before creating a new one so the in-memory
    # registry and the on-disk account dirs do not grow without bound.
    _cleanup_expired_sessions()

    session_id = str(uuid.uuid4())[:8]
    config_dir = _make_account_config_dir(session_id)

    pane_target = _open_claude_tmux_window(config_dir)

    with _sessions_lock:
        _active_login_sessions[session_id] = {
            "created_at": time.time(),
            "pane_target": pane_target,
            "config_dir": config_dir,
            "kind": "add",
        }

    return {
        "session_id": session_id,
        "config_dir": config_dir,
        "instructions": (
            "Authenticate in the terminal below. "
            "After login completes, click 'Verify & Save'."
        ),
    }


def start_relogin_session(config_dir: str) -> dict:
    """
    Open an interactive tmux login for an already-enrolled account's
    existing ``config_dir``.

    Used when the account's credentials have gone stale (refresh token
    revoked, 401, missing token).  The slot's email, priority, threshold,
    and credential-target mappings all stay intact — only the OAuth
    material inside the isolated config dir is replaced when the user
    finishes the interactive login.

    Raises ``ValueError`` if another re-login session is already active
    for the same ``config_dir``, so two tmux windows cannot fight over the
    same Keychain entry concurrently.
    """
    _cleanup_expired_sessions()

    # Duplicate guard — reject rather than spawn a second tmux window that
    # would race the first one writing to the same hashed Keychain entry.
    with _sessions_lock:
        for data in _active_login_sessions.values():
            if data.get("kind") == "relogin" and data.get("config_dir") == config_dir:
                raise ValueError("A re-login session is already active for this account")

    if not os.path.isdir(config_dir):
        raise ValueError(f"Account config directory does not exist: {config_dir}")

    session_id = str(uuid.uuid4())[:8]
    pane_target = _open_claude_tmux_window(config_dir)

    with _sessions_lock:
        _active_login_sessions[session_id] = {
            "created_at": time.time(),
            "pane_target": pane_target,
            "config_dir": config_dir,
            "kind": "relogin",
        }

    return {
        "session_id": session_id,
        "config_dir": config_dir,
        "instructions": (
            "Re-authenticate in the terminal below. "
            "After login completes, click 'Verify & Re-login'."
        ),
    }


def verify_login_session(session_id: str) -> dict:
    """
    Verify that a login session produced usable credentials.

    Returns ``{"success": True, "email": str, "config_dir": str, "kind": str}``
    on success — the ``kind`` lets the router branch between enrollment
    (save new DB row) and re-login (clear stale_reason) post-processing.

    Returns ``{"success": False, "error": str}`` otherwise.  On success the
    session is popped from the tracking dict; on failure it is left in
    place so the user can finish logging in and retry ``verify`` without
    having to restart the whole flow.
    """
    with _sessions_lock:
        data = _active_login_sessions.get(session_id)
    if not data:
        return {"success": False, "error": "Session not found"}

    config_dir = data["config_dir"]
    kind = data.get("kind", "add")

    # For enrollment we ONLY accept paths that live inside the accounts base
    # directory — defence in depth in case the session id somehow leaked into
    # a place that could point it at an arbitrary directory.  Re-login paths
    # come from DB rows we already trust.
    if kind == "add":
        real = os.path.realpath(config_dir)
        base = os.path.realpath(_accounts_base())
        if not real.startswith(base + os.sep):
            return {"success": False, "error": "Invalid session"}

    if not os.path.isdir(config_dir):
        return {"success": False, "error": "Session config directory missing"}

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
                "Make sure the login completed in the terminal."
            ),
        }

    # Successful verification — stop tracking this session so the registry
    # does not keep a pointer to an already-promoted account dir.
    with _sessions_lock:
        _active_login_sessions.pop(session_id, None)
    return {"success": True, "email": email, "config_dir": config_dir, "kind": kind}


def cleanup_login_session(session_id: str) -> None:
    """Remove a session from tracking.

    For ``kind="add"`` the isolated config directory is also deleted —
    it was a throwaway created for this enrollment attempt.

    For ``kind="relogin"`` the config directory belongs to an existing
    account slot and MUST be preserved; only the registry entry is popped.
    """
    with _sessions_lock:
        data = _active_login_sessions.pop(session_id, None)
    if not data:
        return
    if data.get("kind") != "add":
        # Re-login (or any future non-add kind) — never touch the account dir.
        return
    config_dir = data.get("config_dir") or os.path.join(_accounts_base(), f"account-{session_id}")
    real = os.path.realpath(config_dir)
    base = os.path.realpath(_accounts_base())
    if os.path.isdir(real) and real.startswith(base + os.sep):
        shutil.rmtree(real, ignore_errors=True)
