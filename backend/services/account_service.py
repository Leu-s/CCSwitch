"""
Account lifecycle service.

Each managed account has an isolated Claude config directory under
~/.claude-multi-accounts/account-{uuid}/.

Claude Code respects the CLAUDE_CONFIG_DIR environment variable; when set to
an empty (fresh) directory it will run a new OAuth flow, storing all credentials
and config in that directory without touching ~/.claude/.
"""

import json
import logging
import os
import shutil
import subprocess
import time
import uuid

from ..config import settings
from .credential_provider import (
    _keychain_service_name,
    _read_keychain_credentials,
    _write_keychain_credentials,
    _write_keychain_credentials_legacy,
    get_access_token_from_config_dir,
    get_refresh_token_from_config_dir,
    get_token_info,
    save_refreshed_token,
)

logger = logging.getLogger(__name__)


# ── Path helpers ───────────────────────────────────────────────────────────────

def accounts_base() -> str:
    return os.path.expanduser(settings.accounts_base_dir)


def active_claude_dir() -> str:
    return os.path.expanduser(settings.active_claude_dir)


def make_account_config_dir(session_id: str) -> str:
    path = os.path.join(accounts_base(), f"account-{session_id}")
    os.makedirs(path, exist_ok=True)
    return path


# ── Reading credentials / email from a config dir ─────────────────────────────

def get_email_from_config_dir(config_dir: str) -> str | None:
    """Return the emailAddress stored in .claude.json inside config_dir."""
    path = os.path.join(config_dir, ".claude.json")
    try:
        with open(path) as f:
            data = json.load(f)
        return data.get("oauthAccount", {}).get("emailAddress")
    except Exception:
        return None


# ── Active (system) config helpers ────────────────────────────────────────────

def get_active_email() -> str | None:
    return get_email_from_config_dir(active_claude_dir())


def backup_active_config() -> dict:
    """Snapshot the current ~/.claude/.claude.json as a string for later restore."""
    path = os.path.join(active_claude_dir(), ".claude.json")
    try:
        with open(path) as f:
            return {"claude_json": f.read()}
    except Exception:
        return {}


def restore_config_from_backup(backup: dict) -> None:
    if not backup.get("claude_json"):
        return
    dst = active_claude_dir()
    os.makedirs(dst, exist_ok=True)
    with open(os.path.join(dst, ".claude.json"), "w") as f:
        f.write(backup["claude_json"])
    # Point the active-config file at ~/.claude/ so new terminals fall back
    # to the restored credentials rather than a now-stale per-account dir.
    _write_active_config_dir(dst)


# ── Activation ────────────────────────────────────────────────────────────────

def _active_state_paths() -> tuple[str, str]:
    """Return (state_dir, active_file_path)."""
    state_dir = os.path.expanduser(settings.state_dir)
    return state_dir, os.path.join(state_dir, "active")


def _write_active_config_dir(config_dir: str) -> None:
    """
    Write the active account's isolated config_dir to ~/.claude-multi/active.
    Users can add to their shell profile:
        _d=$(cat ~/.claude-multi/active 2>/dev/null); [ -n "$_d" ] && export CLAUDE_CONFIG_DIR="$_d"; unset _d
    This ensures new terminal sessions use the correct account without Keychain gymnastics.
    File is written with mode 0o600 (owner-read/write only) since the path is sensitive.
    """
    state_dir, active_path = _active_state_paths()
    tmp_path = f"{active_path}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        os.makedirs(state_dir, exist_ok=True)
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(config_dir + "\n")
        os.replace(tmp_path, active_path)
        logger.debug("Active config dir written to %s", active_path)
    except Exception as e:
        logger.warning("Failed to write active config dir file: %s", e)


def clear_active_config_dir() -> None:
    """Remove ~/.claude-multi/active so new shells fall back to ~/.claude."""
    _, active_path = _active_state_paths()
    try:
        os.remove(active_path)
        logger.debug("Cleared active config dir file %s", active_path)
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning("Failed to clear active config dir file: %s", e)


def activate_account_config(config_dir: str) -> None:
    """
    Activate an account so Claude Code CLI (run without CLAUDE_CONFIG_DIR)
    uses it.

    Four things happen:
    1. Copy credential / config files into ~/.claude/  (file-based fallback)
    2. Copy the Keychain entry to the ~/.claude/ service name  (hashed, macOS)
    3. Update the legacy no-hash 'Claude Code-credentials' entry so that
       Claude Code versions that don't use CLAUDE_CONFIG_DIR isolation pick up
       the switch immediately.
    4. Write the active config_dir path to ~/.claude-multi/active so users can
       set CLAUDE_CONFIG_DIR in their shell profile for guaranteed new-terminal
       isolation.
    """
    dst = active_claude_dir()
    os.makedirs(dst, exist_ok=True)

    # ── 1. File-based credentials (file-based fallback) ───────────────────────
    for filename in [".claude.json", "credentials.json", ".credentials.json"]:
        src = os.path.join(config_dir, filename)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(dst, filename))
            logger.debug("Copied %s → %s", src, os.path.join(dst, filename))

    # ── 2. macOS Keychain (hashed entry for ~/.claude/) ───────────────────────
    kc = _read_keychain_credentials(config_dir)
    if kc:
        ok_hashed = _write_keychain_credentials(dst, kc)
        if ok_hashed:
            logger.debug("Keychain credentials copied %s → %s",
                         _keychain_service_name(config_dir),
                         _keychain_service_name(dst))

        # ── 3. Legacy no-hash entry (picked up by fresh `claude` sessions) ────
        ok_legacy = _write_keychain_credentials_legacy(kc)
        if not ok_hashed and not ok_legacy:
            logger.warning(
                "Both Keychain writes failed for %s — new terminal sessions may "
                "not pick up the account switch until Keychain is accessible again.",
                config_dir,
            )

    # ── 4. Write active config dir for shell CLAUDE_CONFIG_DIR trick ──────────
    _write_active_config_dir(config_dir)


# ── Login session ─────────────────────────────────────────────────────────────

# Tracks in-flight login sessions so orphaned dirs are cleaned up automatically.
# Maps session_id → creation timestamp (time.time()).
_active_login_sessions: dict[str, float] = {}
_SESSION_TIMEOUT = 1800  # 30 min — abandon any session not verified within this window


def _cleanup_expired_sessions() -> None:
    """Remove config dirs for login sessions that were never verified and have timed out."""
    now = time.time()
    expired = [sid for sid, ts in list(_active_login_sessions.items()) if now - ts > _SESSION_TIMEOUT]
    for sid in expired:
        _active_login_sessions.pop(sid, None)
        cleanup_login_session(sid)
        logger.info("Cleaned up orphaned login session dir: account-%s", sid)


def start_login_session() -> dict:
    """
    Create a fresh isolated config directory and open a tmux window where the
    user can run `claude` (with CLAUDE_CONFIG_DIR set) to authenticate.

    Returns session metadata including the tmux pane target.
    """
    _cleanup_expired_sessions()

    session_id = str(uuid.uuid4())[:8]
    config_dir = make_account_config_dir(session_id)
    _active_login_sessions[session_id] = time.time()

    # Ensure at least one tmux server/session is running so new-window works
    sessions = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        capture_output=True, text=True,
    )
    if sessions.returncode != 0 or not sessions.stdout.strip():
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", settings.tmux_session_name],
            capture_output=True, text=True,
        )

    result = subprocess.run(
        [
            "tmux", "new-window",
            "-P", "-F", "#{session_name}:#{window_index}.#{pane_index}",
            "-n", f"add-acct",
        ],
        capture_output=True, text=True, check=True,
    )
    pane_target = result.stdout.strip()

    # Launch claude with the isolated config dir
    subprocess.run(
        ["tmux", "send-keys", "-t", pane_target,
         f"CLAUDE_CONFIG_DIR={config_dir} claude", "Enter"],
        check=True, capture_output=True,
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
    config_dir = os.path.join(accounts_base(), f"account-{session_id}")
    if not os.path.isdir(config_dir):
        return {"success": False, "error": "Session not found"}

    email = get_email_from_config_dir(config_dir)
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

    # Login completed — remove from tracking so it won't be auto-cleaned up
    _active_login_sessions.pop(session_id, None)
    return {"success": True, "email": email, "config_dir": config_dir}


def cleanup_login_session(session_id: str) -> None:
    """Remove a login session's config dir (called on cancel or timeout)."""
    _active_login_sessions.pop(session_id, None)
    config_dir = os.path.join(accounts_base(), f"account-{session_id}")
    if os.path.isdir(config_dir) and config_dir.startswith(accounts_base()):
        shutil.rmtree(config_dir, ignore_errors=True)
