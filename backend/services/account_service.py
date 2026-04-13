"""
Account lifecycle service.

Each managed account has an isolated Claude config directory under
~/.claude-multi-accounts/account-{uuid}/.

Claude Code respects the CLAUDE_CONFIG_DIR environment variable; when set to
an empty (fresh) directory it will run a new OAuth flow, storing all credentials
and config in that directory without touching ~/.claude/.
"""

import hashlib
import json
import logging
import os
import shutil
import subprocess
import uuid

from ..config import settings

logger = logging.getLogger(__name__)


# ── Path helpers ───────────────────────────────────────────────────────────────

def accounts_base() -> str:
    return os.path.expanduser(settings.accounts_base_dir)


def active_claude_dir() -> str:
    return os.path.expanduser(settings.claude_config_dir)


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


def _load_json_safe(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def _keychain_service_name(config_dir: str) -> str:
    """Claude Code uses sha256(config_dir)[:8] as the Keychain service suffix."""
    h = hashlib.sha256(config_dir.encode()).hexdigest()[:8]
    return f"Claude Code-credentials-{h}"


def _read_keychain_credentials(config_dir: str) -> dict:
    """
    On macOS, Claude Code stores OAuth tokens in the Keychain under a service
    name derived from sha256(CLAUDE_CONFIG_DIR)[:8].  Returns the parsed JSON
    or {} on any failure.
    """
    service = _keychain_service_name(config_dir)
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout.strip())
    except Exception as e:
        logger.debug("Keychain lookup failed for %s: %s", service, e)
    return {}


def get_token_info(config_dir: str) -> dict:
    """
    Return non-secret token metadata: token expiry timestamp and subscription type.
    Does NOT return access/refresh tokens or rate limit tier.
    Falls back to file-based credentials if Keychain is unavailable.
    """
    kc = _read_keychain_credentials(config_dir)
    oauth = kc.get("claudeAiOauth", kc)
    result = {}
    if oauth.get("expiresAt"):
        result["token_expires_at"] = oauth["expiresAt"]
    if oauth.get("subscriptionType"):
        result["subscription_type"] = oauth["subscriptionType"]
    if result:
        return result

    # Fall back to file-based credentials
    for filename in [".credentials.json", "credentials.json", ".claude.json"]:
        path = os.path.join(config_dir, filename)
        data = _load_json_safe(path)
        oauth_data = data.get("claudeAiOauth", data)
        if oauth_data.get("expiresAt"):
            result["token_expires_at"] = oauth_data["expiresAt"]
        if oauth_data.get("subscriptionType"):
            result["subscription_type"] = oauth_data["subscriptionType"]
        if result:
            return result
    return {}


def get_access_token_from_config_dir(config_dir: str) -> str | None:
    """
    Try several locations Claude Code may use to persist the OAuth access token
    when CLAUDE_CONFIG_DIR is set to a custom path.

    On macOS (native build) Claude Code stores tokens in the Keychain.
    On other platforms it writes credential files to the config directory.
    """
    # 1. macOS Keychain (primary location for native Claude Code)
    kc = _read_keychain_credentials(config_dir)
    if kc:
        token = kc.get("claudeAiOauth", {}).get("accessToken")
        if token:
            return token
        if kc.get("accessToken"):
            return kc["accessToken"]

    # 2. Fall back to credential files (Linux / older versions)
    candidates = [
        os.path.join(config_dir, ".credentials.json"),
        os.path.join(config_dir, "credentials.json"),
        os.path.join(config_dir, ".claude.json"),
    ]
    for path in candidates:
        if not os.path.exists(path):
            continue
        data = _load_json_safe(path)
        if "claudeAiOauth" in data:
            token = data["claudeAiOauth"].get("accessToken")
            if token:
                return token
        if "accessToken" in data:
            return data["accessToken"]
    return None


def get_refresh_token_from_config_dir(config_dir: str) -> str | None:
    # 1. macOS Keychain
    kc = _read_keychain_credentials(config_dir)
    if kc:
        token = kc.get("claudeAiOauth", {}).get("refreshToken")
        if token:
            return token
        if kc.get("refreshToken"):
            return kc["refreshToken"]

    # 2. Credential files
    candidates = [
        os.path.join(config_dir, ".credentials.json"),
        os.path.join(config_dir, "credentials.json"),
        os.path.join(config_dir, ".claude.json"),
    ]
    for path in candidates:
        if not os.path.exists(path):
            continue
        data = _load_json_safe(path)
        if "claudeAiOauth" in data:
            token = data["claudeAiOauth"].get("refreshToken")
            if token:
                return token
        if "refreshToken" in data:
            return data["refreshToken"]
    return None


def save_refreshed_token(config_dir: str, access_token: str, expires_at: int | None = None) -> None:
    """Persist a refreshed access token back into the config dir."""
    for filename in [".credentials.json", "credentials.json"]:
        path = os.path.join(config_dir, filename)
        if not os.path.exists(path):
            continue
        data = _load_json_safe(path)
        if "claudeAiOauth" in data:
            data["claudeAiOauth"]["accessToken"] = access_token
            if expires_at is not None:
                data["claudeAiOauth"]["expiresAt"] = expires_at
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
            return
        if "accessToken" in data:
            data["accessToken"] = access_token
            if expires_at is not None:
                data["expiresAt"] = expires_at
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
            return

    # Also try .claude.json
    path = os.path.join(config_dir, ".claude.json")
    if os.path.exists(path):
        data = _load_json_safe(path)
        if "claudeAiOauth" in data:
            data["claudeAiOauth"]["accessToken"] = access_token
            if expires_at is not None:
                data["claudeAiOauth"]["expiresAt"] = expires_at
            with open(path, "w") as f:
                json.dump(data, f, indent=2)


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
    os.makedirs(active_claude_dir(), exist_ok=True)
    with open(os.path.join(active_claude_dir(), ".claude.json"), "w") as f:
        f.write(backup["claude_json"])


# ── Activation ────────────────────────────────────────────────────────────────

def activate_account_config(config_dir: str) -> None:
    """
    Copy all config/credential files from an account's isolated directory
    into the active ~/.claude/ directory, making that account the one Claude
    Code will use when run without an explicit CLAUDE_CONFIG_DIR.
    """
    dst = active_claude_dir()
    os.makedirs(dst, exist_ok=True)

    files_to_copy = [
        ".claude.json",
        "credentials.json",
        ".credentials.json",
    ]
    for filename in files_to_copy:
        src = os.path.join(config_dir, filename)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(dst, filename))
            logger.debug("Copied %s → %s", src, os.path.join(dst, filename))


# ── Login session ─────────────────────────────────────────────────────────────

def start_login_session() -> dict:
    """
    Create a fresh isolated config directory and open a tmux window where the
    user can run `claude` (with CLAUDE_CONFIG_DIR set) to authenticate.

    Returns session metadata including the tmux pane target.
    """
    session_id = str(uuid.uuid4())[:8]
    config_dir = make_account_config_dir(session_id)

    # Ensure at least one tmux server/session is running so new-window works
    sessions = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        capture_output=True, text=True,
    )
    if sessions.returncode != 0 or not sessions.stdout.strip():
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", "claude-multi"],
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

    return {"success": True, "email": email, "config_dir": config_dir}


def cleanup_login_session(session_id: str) -> None:
    """Remove a login session's config dir (called on cancel)."""
    config_dir = os.path.join(accounts_base(), f"account-{session_id}")
    if os.path.isdir(config_dir) and config_dir.startswith(accounts_base()):
        shutil.rmtree(config_dir, ignore_errors=True)
