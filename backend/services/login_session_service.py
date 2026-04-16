"""
Login session lifecycle helpers for the vault-swap architecture.

Both the add-account flow and the re-login flow use a transient scratch
directory under ``$TMPDIR/ccswitch-login-<session>/`` as a bootstrap
vehicle for Claude Code's OAuth dance.  The scratch dir is only
``CLAUDE_CONFIG_DIR`` for the child tmux process during the interactive
login; after ``verify_login_session`` succeeds, credentials are promoted
into the vault and the scratch dir (plus its hashed Keychain entry) is
removed.

Neither flow creates or preserves a per-account directory.  The account
identity lives in the DB and the vault Keychain entry; on disk,
``~/.claude/`` is the only Claude Code state location.
"""

import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

from ..config import settings
from . import credential_provider as cp


logger = logging.getLogger(__name__)

# session_id → {
#   "created_at": float,
#   "pane_target": str,
#   "scratch_dir": str,
#   "kind": "add" | "relogin",
#   "expected_email": str | None,   # set for re-login to guard against wrong-identity
# }
_active_login_sessions: dict[str, dict] = {}

# asyncio.to_thread dispatches to a pool with multiple worker threads, so
# start / verify / cleanup can touch the dict concurrently. Re-entrant
# because ``_cleanup_expired_sessions`` iterates the dict and then calls
# ``cleanup_login_session`` which also acquires the lock.
_sessions_lock = threading.RLock()

# Sessions older than this are reaped by ``_cleanup_expired_sessions``.
_SESSION_TIMEOUT: int = settings.login_session_timeout


# ── Scratch directory helpers ─────────────────────────────────────────────


def _make_scratch_dir(session_id: str) -> str:
    """Create a transient directory for an interactive login session.

    Lives under the system TMPDIR so it survives for the duration of the
    login flow and is cleaned up by the OS (and by our own ``rmtree``)
    once the session is verified or abandoned.
    """
    base = os.path.join(tempfile.gettempdir(), "ccswitch-login")
    os.makedirs(base, mode=0o700, exist_ok=True)
    path = os.path.join(base, f"session-{session_id}")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def _read_scratch_identity(scratch_dir: str) -> tuple[dict | None, str | None, str | None]:
    """Read the identity metadata Claude Code wrote to the scratch dir's
    ``.claude.json``.  Returns ``(oauthAccount, userID, emailAddress)``.
    """
    path = os.path.join(scratch_dir, ".claude.json")
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        return None, None, None
    oauth_account = data.get("oauthAccount")
    user_id = data.get("userID")
    email = None
    if isinstance(oauth_account, dict):
        email = oauth_account.get("emailAddress")
    return (
        oauth_account if isinstance(oauth_account, dict) else None,
        user_id if isinstance(user_id, str) else None,
        email if isinstance(email, str) else None,
    )


# ── Tmux helper (shared by add + relogin) ─────────────────────────────────


def _open_claude_tmux_window(scratch_dir: str) -> str:
    """Ensure the dedicated ccswitch tmux session exists and launch a new
    window inside it with ``claude`` running under
    ``CLAUDE_CONFIG_DIR=scratch_dir``.

    The window is explicitly created with ``-t <ccswitch_session>`` so it
    lands in CCSwitch's own session instead of whatever session the user
    is currently attached to — otherwise login panes pollute the user's
    own tmux workspace.

    Returns the new pane's target string (``session:window.pane``).
    """
    session_name = settings.tmux_session_name

    # Idempotent session-create: tmux returns non-zero if the session
    # already exists, which is exactly what we want.  Swallow that.
    try:
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", session_name],
            capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        logger.warning("tmux new-session timed out — falling through")

    result = subprocess.run(
        [
            "tmux", "new-window",
            "-t", session_name,
            "-P", "-F", "#{session_name}:#{window_index}.#{pane_index}",
            "-n", "add-acct",
        ],
        capture_output=True, text=True, check=True, timeout=10,
    )
    pane_target = result.stdout.strip()

    subprocess.run(
        ["tmux", "send-keys", "-t", pane_target,
         f"CLAUDE_CONFIG_DIR={scratch_dir} claude", "Enter"],
        check=True, capture_output=True, timeout=10,
    )

    return pane_target


# ── Session lifecycle ─────────────────────────────────────────────────────


def _cleanup_expired_sessions() -> None:
    """Remove sessions that have exceeded ``_SESSION_TIMEOUT``."""
    now = time.time()
    with _sessions_lock:
        expired = [
            sid for sid, data in _active_login_sessions.items()
            if now - data["created_at"] > _SESSION_TIMEOUT
        ]
    for sid in expired:
        cleanup_login_session(sid)
        logger.debug("Expired login session cleaned up: %s", sid)


def get_pane_target(session_id: str) -> str | None:
    with _sessions_lock:
        data = _active_login_sessions.get(session_id)
    return data["pane_target"] if data else None


def start_login_session() -> dict:
    """Create a fresh scratch directory and open a tmux window where the
    user can run ``claude /login`` to authenticate a new account."""
    _cleanup_expired_sessions()

    session_id = str(uuid.uuid4())[:8]
    scratch_dir = _make_scratch_dir(session_id)
    pane_target = _open_claude_tmux_window(scratch_dir)

    with _sessions_lock:
        _active_login_sessions[session_id] = {
            "created_at": time.time(),
            "pane_target": pane_target,
            "scratch_dir": scratch_dir,
            "kind": "add",
            "expected_email": None,
        }

    return {
        "session_id": session_id,
        "instructions": (
            "Authenticate in the terminal below. "
            "After login completes, click 'Verify & Save'."
        ),
    }


def start_relogin_session(expected_email: str) -> dict:
    """Open a scratch login for an existing account whose credentials have
    gone stale.  The scratch dir is a brand-new temporary directory — same
    mechanism as an add-flow — with ``expected_email`` stored on the
    session so ``verify_login_session`` can refuse wrong-identity logins.

    If a prior re-login session for the same email is still in the
    registry (user closed the tab before verify, browser crashed, etc.),
    tear it down FIRST — that kills the orphan tmux window and rmtree's
    the scratch dir — then start fresh.  Clicking Re-login again is a
    clear signal the user wants to retry, so rejecting them (as the
    pre-fix code did) until the 30-minute session timeout expires is
    user-hostile.
    """
    _cleanup_expired_sessions()

    with _sessions_lock:
        orphan_sids = [
            sid for sid, data in _active_login_sessions.items()
            if (
                data.get("kind") == "relogin"
                and data.get("expected_email") == expected_email
            )
        ]
    for sid in orphan_sids:
        logger.info(
            "Re-login retry for %s — tearing down orphan session %s first",
            expected_email, sid,
        )
        cleanup_login_session(sid)

    session_id = str(uuid.uuid4())[:8]
    scratch_dir = _make_scratch_dir(session_id)
    pane_target = _open_claude_tmux_window(scratch_dir)

    with _sessions_lock:
        _active_login_sessions[session_id] = {
            "created_at": time.time(),
            "pane_target": pane_target,
            "scratch_dir": scratch_dir,
            "kind": "relogin",
            "expected_email": expected_email,
        }

    return {
        "session_id": session_id,
        "instructions": (
            "Re-authenticate in the terminal below. "
            "After login completes, click 'Verify & Re-login'."
        ),
    }


def verify_login_session(session_id: str) -> dict:
    """Verify that a login session produced usable credentials and extract
    them from the scratch dir's hashed Keychain entry.

    On success returns::

        {
            "success": True,
            "email": str,
            "oauth_account": dict,
            "user_id": str | None,
            "oauth_tokens": dict,   # ready to save into vault
            "kind": "add" | "relogin",
            "expected_email": str | None,
        }

    On failure returns ``{"success": False, "error": str}``.

    This function is read-only w.r.t. the session registry: success and
    failure BOTH leave the session in place.  The caller is expected to
    invoke ``cleanup_login_session`` to tear down the scratch dir, the
    tmux window, and the hashed Keychain entry — whether the verify
    succeeded (normal teardown) or needs a clean retry (no-op on the
    stub).  Popping here would defeat the teardown because
    ``cleanup_login_session`` looks the session up by id.
    """
    with _sessions_lock:
        data = _active_login_sessions.get(session_id)
    if not data:
        return {"success": False, "error": "Session not found"}

    scratch_dir = data["scratch_dir"]
    kind = data.get("kind", "add")
    expected_email = data.get("expected_email")

    if not os.path.isdir(scratch_dir):
        return {"success": False, "error": "Session scratch directory missing"}

    oauth_account, user_id, email = _read_scratch_identity(scratch_dir)
    if not email:
        return {
            "success": False,
            "error": "Login not detected yet — .claude.json not found or missing email",
        }

    scratch_blob = cp.read_login_scratch(scratch_dir)
    tokens = scratch_blob.get("claudeAiOauth") if scratch_blob else None
    if not tokens or not tokens.get("refreshToken"):
        return {
            "success": False,
            "error": (
                "Credentials not found for the session. "
                "Make sure the login completed in the terminal."
            ),
        }

    return {
        "success": True,
        "email": email,
        "oauth_account": oauth_account,
        "user_id": user_id,
        "oauth_tokens": tokens,
        "kind": kind,
        "expected_email": expected_email,
    }


def cleanup_login_session(session_id: str) -> None:
    """Remove a session entirely: pop from tracking, kill the tmux window,
    delete the scratch hashed Keychain entry, and ``rmtree`` the scratch
    directory.

    Used by explicit cancel, successful verification post-process, and
    the expiry sweep.  Always safe: individual step failures are logged
    but do not abort the remaining steps.

    Ordering matters: kill the tmux window FIRST so the ``claude``
    process inside the pane is terminated before we pull the rug
    (scratch dir + hashed Keychain entry) out from under it.  Otherwise
    the CLI sees its config dir vanish mid-lifetime and may print a
    startle into the pane just before the kill lands.
    """
    with _sessions_lock:
        data = _active_login_sessions.pop(session_id, None)
    if not data:
        return

    pane_target = data.get("pane_target")
    if pane_target:
        try:
            subprocess.run(
                ["tmux", "kill-window", "-t", pane_target],
                capture_output=True, timeout=5,
            )
        except Exception as e:
            logger.debug("tmux kill-window failed for %s: %s", pane_target, e)

    scratch_dir = data.get("scratch_dir")
    if not scratch_dir:
        return
    try:
        cp.delete_login_scratch(scratch_dir)
    except Exception as e:
        logger.debug("delete_login_scratch failed for %s: %s", scratch_dir, e)
    try:
        shutil.rmtree(scratch_dir, ignore_errors=True)
    except Exception as e:
        logger.debug("rmtree failed for %s: %s", scratch_dir, e)
