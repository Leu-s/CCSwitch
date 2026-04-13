"""
Account lifecycle service.

Each managed account has an isolated Claude config directory under
~/.claude-multi-accounts/account-{uuid}/.

Claude Code respects the CLAUDE_CONFIG_DIR environment variable; when set to
an empty (fresh) directory it will run a new OAuth flow, storing all credentials
and config in that directory without touching the default location.

IMPORTANT — where Claude Code actually reads credentials on macOS
----------------------------------------------------------------
When CLAUDE_CONFIG_DIR is NOT set, Claude Code reads/writes:

    • Config:       $HOME/.claude.json            (file directly in HOME,
                                                   NOT inside $HOME/.claude/)
    • Keychain:     service = "Claude Code-credentials"  (no hash suffix)
                    account = $USER (process.env.USER)

When CLAUDE_CONFIG_DIR *is* set to /some/dir, Claude Code reads/writes:

    • Config:       /some/dir/.claude.json
    • Keychain:     service = "Claude Code-credentials-<sha256(/some/dir)[:8]>"
                    account = $USER

Switching the "global" active account (for new terminals that don't have
CLAUDE_CONFIG_DIR set) therefore requires writing to $HOME/.claude.json AND to
the legacy unhashed Keychain entry — not to $HOME/.claude/.claude.json, which
Claude Code never reads for OAuth state.
"""

import getpass
import json
import logging
import os
import shutil
import subprocess
import threading
import time
import uuid


from ..config import settings
from ..models import Account
from ..schemas import UsageData
from .credential_provider import (
    _read_keychain_credentials,
    _write_keychain_credentials,
    get_access_token_from_config_dir,
    get_refresh_token_from_config_dir,
    get_token_info,
    save_refreshed_token,
)

logger = logging.getLogger(__name__)

# Keys in .claude.json that describe the active OAuth account. Only these are
# copied from an account's isolated config dir into $HOME/.claude.json on a
# switch, so unrelated home state (projects, mcpServers, onboarding, etc.) is
# preserved across account switches.
_OAUTH_KEYS = ("oauthAccount", "userID")

# Keys in .claude.json that are per-account UI/workspace state and should be
# written back into the account's isolated dir BEFORE switching away, so that
# state accumulated while a user ran `claude` without CLAUDE_CONFIG_DIR is not
# silently lost. These cover the fields Claude Code writes most often while
# running: project tracking, mcp state, recent history, onboarding progress.
#
# IDENTITY fields (oauthAccount, userID) are intentionally excluded. They live
# in the account dir from the login flow and must never be written back — if a
# concurrent switch has already merged a different account's oauthAccount into
# HOME, reading HOME here would clobber the account dir's identity with the
# wrong email/UUID. Keeping writeback strictly limited to workspace state
# preserves the invariant "account_dir/.claude.json.oauthAccount never changes
# after login".
_WRITEBACK_KEYS = (
    "projects",
    "mcpServers",
    "hasCompletedOnboarding",
    "lastOnboardingVersion",
    "customApiKeyResponses",
)


# ── Path helpers ───────────────────────────────────────────────────────────────

def accounts_base() -> str:
    return os.path.expanduser(settings.accounts_base_dir)


def active_claude_dir() -> str:
    """The directory Claude Code uses as CLAUDE_CONFIG_DIR's fallback
    (~/.claude by default). Used for commands/, agents/, settings.json,
    history.jsonl, etc. — NOT for the .claude.json config file."""
    return os.path.expanduser(settings.active_claude_dir)


def active_config_file() -> str:
    """Absolute path of the .claude.json file that Claude Code reads for
    oauthAccount state when CLAUDE_CONFIG_DIR is unset. Lives in HOME, not
    inside active_claude_dir()."""
    return os.path.join(os.path.expanduser("~"), ".claude.json")


def active_dir_pointer_path() -> str:
    """Path of the file that records which isolated account dir is active.
    Derived from settings.state_dir so users who override CLAUDE_MULTI_STATE_DIR
    get a single, consistent location everywhere in the codebase."""
    return os.path.join(os.path.expanduser(settings.state_dir), "active")


def make_account_config_dir(session_id: str) -> str:
    path = os.path.join(accounts_base(), f"account-{session_id}")
    os.makedirs(path, exist_ok=True)
    return path


# ── Reading credentials / email from a config dir ─────────────────────────────

def _load_json(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def get_email_from_config_dir(config_dir: str) -> str | None:
    """Return the emailAddress stored in .claude.json inside config_dir."""
    data = _load_json(os.path.join(config_dir, ".claude.json"))
    return (data.get("oauthAccount") or {}).get("emailAddress")


# ── Active (system) config helpers ────────────────────────────────────────────

def get_active_email() -> str | None:
    """Return the email of the currently-active account as Claude Code sees it
    (i.e. the oauthAccount stored in $HOME/.claude.json). Falls back to the
    legacy $HOME/.claude/.claude.json location for installs upgrading from
    older versions of this service."""
    data = _load_json(active_config_file())
    email = (data.get("oauthAccount") or {}).get("emailAddress")
    if email:
        return email
    legacy = _load_json(os.path.join(active_claude_dir(), ".claude.json"))
    return (legacy.get("oauthAccount") or {}).get("emailAddress")


def get_active_config_dir_pointer() -> str | None:
    """Read ~/.claude-multi/active to find which isolated account dir is
    currently marked active. Returns None if the file is missing or empty."""
    try:
        with open(active_dir_pointer_path()) as f:
            v = f.read().strip()
            return v or None
    except Exception:
        return None


def backup_active_config() -> dict:
    """Snapshot $HOME/.claude.json so disable can restore the caller's original
    credentials later. Returns {} if the file is missing."""
    try:
        with open(active_config_file()) as f:
            return {"claude_json": f.read()}
    except Exception:
        return {}


def restore_config_from_backup(backup: dict) -> None:
    if not backup.get("claude_json"):
        return
    path = active_config_file()
    with open(path, "w") as f:
        f.write(backup["claude_json"])
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
    # Point new terminals at ~/.claude/ so they no longer export CLAUDE_CONFIG_DIR
    # for a per-account dir that may have gone stale.
    write_active_config_dir(active_claude_dir())


# ── Activation ────────────────────────────────────────────────────────────────

def clear_active_config_dir() -> None:
    """
    Remove the ~/.claude-multi/active pointer file so that new terminal
    sessions do not export CLAUDE_CONFIG_DIR pointing at a stale or deleted
    account directory.  Called when the last (or only) account is deleted and
    there is no replacement to switch to.
    """
    try:
        os.remove(active_dir_pointer_path())
        logger.debug("Cleared active config dir pointer")
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning("Failed to clear active config dir pointer: %s", e)


def write_active_config_dir(config_dir: str) -> None:
    """
    Write the active account's isolated config_dir to ~/.claude-multi/active.
    Users can add to their shell profile:
        _d=$(cat ~/.claude-multi/active 2>/dev/null); [ -n "$_d" ] && export CLAUDE_CONFIG_DIR="$_d"; unset _d
    This ensures new terminal sessions use the correct account without Keychain gymnastics.
    File is written with mode 0o600 (owner-read/write only) since the path is sensitive.
    """
    state_dir = os.path.expanduser(settings.state_dir)
    active_path = os.path.join(state_dir, "active")
    tmp_path = f"{active_path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        os.makedirs(state_dir, exist_ok=True)
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(config_dir + "\n")
        try:
            os.replace(tmp_path, active_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass
            raise
        logger.debug("Active config dir written to %s", active_path)
    except Exception as e:
        logger.warning("Failed to write active config dir file: %s", e)


def _atomic_write_json(path: str, data: dict, mode: int = 0o600) -> None:
    """Write a JSON file atomically using os.replace, with restrictive perms."""
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _merge_oauth_into_home(source_dir: str) -> bool:
    """
    Merge oauthAccount + userID from source_dir/.claude.json into $HOME/.claude.json.

    Preserves every other field already in $HOME/.claude.json (projects,
    mcpServers, autoUpdates, onboarding, etc.) so switching accounts does not
    wipe unrelated home state. Creates $HOME/.claude.json if missing.

    Returns True if a merge happened.
    """
    source_file = os.path.join(source_dir, ".claude.json")
    if not os.path.exists(source_file):
        logger.warning("Source .claude.json missing: %s", source_file)
        return False
    source = _load_json(source_file)
    if not source.get("oauthAccount"):
        logger.warning("Source .claude.json has no oauthAccount: %s", source_file)
        return False

    home_file = active_config_file()
    home = _load_json(home_file)
    for k in _OAUTH_KEYS:
        if k in source:
            home[k] = source[k]
    _atomic_write_json(home_file, home)
    logger.info(
        "Merged oauthAccount (%s) from %s → %s",
        source["oauthAccount"].get("emailAddress", "?"),
        source_file,
        home_file,
    )
    return True


def _writeback_home_into_account(account_dir: str) -> None:
    """
    Copy the account-related subset of $HOME/.claude.json back into
    account_dir/.claude.json. Called BEFORE switching away from account_dir,
    so state the user accumulated while running `claude` in a shell without
    CLAUDE_CONFIG_DIR is preserved on the next activation.
    """
    home = _load_json(active_config_file())
    if not home:
        return
    target_file = os.path.join(account_dir, ".claude.json")
    try:
        os.makedirs(account_dir, exist_ok=True)
    except Exception:
        pass
    target = _load_json(target_file)
    changed = False
    for k in _WRITEBACK_KEYS:
        if k in home and home[k] != target.get(k):
            target[k] = home[k]
            changed = True
    if changed:
        _atomic_write_json(target_file, target)
        logger.debug("Wrote back home → %s", target_file)


def _clear_stale_legacy_keychain_entries() -> None:
    """
    Delete any 'Claude Code-credentials' Keychain entries whose account name
    is NOT the current $USER. Older Claude Code versions wrote with
    acct='claude-code', and they linger forever because find-generic-password
    returns the newest/most-recent one by a heuristic we do not control — any
    remaining stale entry is a landmine that can surface with the wrong token
    if the user ever downgrades or runs a different Claude Code build.
    """
    service = "Claude Code-credentials"
    user = getpass.getuser()
    # Enumerate candidates by querying with an unlikely-to-exist acct to force
    # an error so we don't accidentally mask a real lookup; instead we loop
    # deleting by alternative account strings known to exist historically.
    for stale_acct in ("claude-code", "claude-code-user", "root"):
        if stale_acct == user:
            continue
        try:
            subprocess.run(
                ["security", "delete-generic-password",
                 "-s", service, "-a", stale_acct],
                capture_output=True, timeout=5,
            )
        except Exception:
            pass


def activate_account_config(target_config_dir: str) -> None:
    """
    Make target_config_dir the active account for the system-default Claude
    Code invocation (i.e. what new terminals see when they run `claude`
    without setting CLAUDE_CONFIG_DIR themselves).

    Steps:
      1. Write the account-related subset of $HOME/.claude.json back into the
         PREVIOUSLY active account's dir (so we don't lose state the user
         accumulated in that account since the last switch).
      2. Merge oauthAccount + userID from target_config_dir/.claude.json into
         $HOME/.claude.json. Does NOT overwrite projects, mcpServers, or
         unrelated home state.
      3. Copy target_config_dir's Keychain entry into the legacy no-hash
         'Claude Code-credentials' service under acct=$USER.
      4. Remove any stale 'Claude Code-credentials' entries left by older
         Claude Code versions (acct=claude-code, etc.).
      5. Copy target_config_dir/.credentials.json → $HOME/.claude/.credentials.json
         as a plaintext fallback (used when the Keychain is unavailable).
      6. Update ~/.claude-multi/active so the shell-profile snippet picks up
         the new account in brand-new terminals that source it.
    """
    target_config_dir = os.path.abspath(os.path.expanduser(target_config_dir))

    # ── 1. Writeback: previous account → its own config dir ──────────────────
    prev_dir = get_active_config_dir_pointer()
    if prev_dir and os.path.abspath(prev_dir) != target_config_dir and os.path.isdir(prev_dir):
        try:
            _writeback_home_into_account(prev_dir)
        except Exception as e:
            logger.warning("Writeback to %s failed: %s", prev_dir, e)

    # ── 2. Merge target's oauthAccount → $HOME/.claude.json ──────────────────
    merged = _merge_oauth_into_home(target_config_dir)
    if not merged:
        logger.error(
            "activate_account_config: refusing to switch — target %s has no "
            "oauthAccount in .claude.json",
            target_config_dir,
        )
        return

    # ── 3. Legacy Keychain entry (no hash, read by new terminals) ────────────
    kc = _read_keychain_credentials(target_config_dir)
    if kc:
        if not _write_keychain_credentials(kc, service="Claude Code-credentials"):
            logger.warning(
                "Legacy Keychain write failed for %s — new terminal sessions "
                "may not pick up the account switch until Keychain is writable.",
                target_config_dir,
            )
    else:
        logger.warning(
            "No Keychain entry found for %s — new terminals will use whatever "
            "credentials currently live under 'Claude Code-credentials'.",
            target_config_dir,
        )

    # ── 4. Clean up old-Claude-Code ghost entries ────────────────────────────
    _clear_stale_legacy_keychain_entries()

    # ── 5. Plaintext credential fallback (for no-Keychain macOS builds) ──────
    claude_dir = active_claude_dir()
    try:
        os.makedirs(claude_dir, exist_ok=True)
    except Exception:
        pass
    src_creds = os.path.join(target_config_dir, ".credentials.json")
    if os.path.exists(src_creds):
        try:
            shutil.copy2(src_creds, os.path.join(claude_dir, ".credentials.json"))
        except Exception as e:
            # A copy failure leaves ~/.claude/ without valid credentials.
            # Re-raise so perform_switch() knows the switch did NOT complete —
            # the active-dir pointer (step 6) is intentionally skipped, keeping
            # the previous pointer intact as the best available fallback.
            logger.error(
                ".credentials.json copy failed for %s → %s: %s",
                src_creds, claude_dir, e,
            )
            raise

    # ── 6. Update active-dir pointer file ────────────────────────────────────
    # Written AFTER all credential operations succeed so the pointer is never
    # advanced to a target whose credentials were not fully installed.
    write_active_config_dir(target_config_dir)


# ── Login session tracking ─────────────────────────────────────────────────────

# session_id → creation timestamp (time.time())
_active_login_sessions: dict[str, float] = {}

# Sessions older than this many seconds are considered expired
_SESSION_TIMEOUT: int = 1800  # 30 minutes


def _cleanup_expired_sessions() -> None:
    """Remove login session dirs for sessions that have exceeded _SESSION_TIMEOUT."""
    now = time.time()
    expired = [sid for sid, created_at in list(_active_login_sessions.items())
               if now - created_at > _SESSION_TIMEOUT]
    for sid in expired:
        cleanup_login_session(sid)
        _active_login_sessions.pop(sid, None)
        logger.debug("Expired login session cleaned up: %s", sid)


# ── Login session ─────────────────────────────────────────────────────────────

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
    config_dir = make_account_config_dir(session_id)

    # Track this session so _cleanup_expired_sessions can reap it if abandoned
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
    real = os.path.realpath(config_dir)
    base = os.path.realpath(accounts_base())
    if not real.startswith(base + os.sep):
        return {"success": False, "error": "Invalid session ID"}
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

    # Successful verification — stop tracking this session so the registry
    # does not keep a pointer to a now-durable account dir.
    _active_login_sessions.pop(session_id, None)
    return {"success": True, "email": email, "config_dir": config_dir}


def cleanup_login_session(session_id: str) -> None:
    """Remove a login session's config dir (called on cancel or expiry)."""
    _active_login_sessions.pop(session_id, None)
    config_dir = os.path.join(accounts_base(), f"account-{session_id}")
    real = os.path.realpath(config_dir)
    base = os.path.realpath(accounts_base())
    if os.path.isdir(real) and real.startswith(base + os.sep):
        shutil.rmtree(real, ignore_errors=True)


# ── Query helpers ──────────────────────────────────────────────────────────────
# Extracted to account_queries.py; re-exported here for backward compatibility.
from .account_queries import (  # noqa: F401
    get_account_by_id,
    get_account_by_email,
    get_enabled_accounts,
    get_all_accounts,
    get_email_to_id_map,
    save_verified_account,
)


# ── Usage helpers ──────────────────────────────────────────────────────────────

def build_usage(usage_raw: dict, token_info: dict) -> "UsageData | None":
    """Convert a raw nested usage cache entry + token_info dict into a flat
    UsageData.  Public wrapper around UsageData.from_raw so callers outside
    the routers package can access it without importing schemas directly."""
    return UsageData.from_raw(usage_raw, token_info)
