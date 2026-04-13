"""
Credential reading and writing helpers.

Handles macOS Keychain access and file-based credential fallbacks.
"""
import hashlib
import json
import logging
import os
import subprocess

from ..config import settings

logger = logging.getLogger(__name__)


def _active_dir_pointer_path() -> str:
    """Same path as account_service.active_dir_pointer_path(); inlined here
    to avoid a circular import (account_service imports this module)."""
    return os.path.join(os.path.expanduser(settings.state_dir), "active")


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


def _write_keychain_entry(service_name: str, credentials: dict) -> bool:
    """Write credentials to a Keychain entry identified by service_name.

    Returns True if credentials were successfully written, False otherwise.
    """
    import getpass
    acct = getpass.getuser()
    cred_json = json.dumps(credentials)
    try:
        subprocess.run(
            ["security", "delete-generic-password", "-s", service_name, "-a", acct],
            capture_output=True, timeout=5,
        )
        result = subprocess.run(
            ["security", "add-generic-password", "-s", service_name, "-a", acct, "-w", cred_json],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return False
        return True
    except Exception:
        return False


def _write_keychain_credentials(credentials: dict, service: str) -> bool:
    """Returns True if credentials were successfully written, False otherwise."""
    ok = _write_keychain_entry(service, credentials)
    if not ok:
        logger.warning("Keychain write failed for %s", service)
    else:
        logger.debug("Keychain credentials written for %s", service)
    return ok


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


def _get_oauth_field(config_dir: str, field: str) -> str | None:
    """
    Look up an OAuth credential field from Keychain then files.

    field: key inside the nested "claudeAiOauth" object (e.g. "accessToken", "refreshToken").
           Also checked at the top level when claudeAiOauth is absent.
    """
    # 1. macOS Keychain (primary location for native Claude Code)
    kc = _read_keychain_credentials(config_dir)
    if kc:
        token = (kc.get("claudeAiOauth") or {}).get(field)
        if token:
            return token
        if kc.get(field):
            return kc[field]

    # 2. Fall back to credential files (Linux / older versions)
    for fname in [".credentials.json", "credentials.json", ".claude.json"]:
        path = os.path.join(config_dir, fname)
        data = _load_json_safe(path)
        if not data:
            continue
        if "claudeAiOauth" in data:
            token = data["claudeAiOauth"].get(field)
            if token:
                return token
        if data.get(field):
            return data[field]
    return None


def get_access_token_from_config_dir(config_dir: str) -> str | None:
    """
    Try several locations Claude Code may use to persist the OAuth access token
    when CLAUDE_CONFIG_DIR is set to a custom path.

    On macOS (native build) Claude Code stores tokens in the Keychain.
    On other platforms it writes credential files to the config directory.
    """
    return _get_oauth_field(config_dir, "accessToken")


def get_refresh_token_from_config_dir(config_dir: str) -> str | None:
    return _get_oauth_field(config_dir, "refreshToken")


def save_refreshed_token(config_dir: str, access_token: str, expires_at: int | None = None) -> None:
    """
    Persist a refreshed access token back into the config dir.

    Writes the first applicable credentials file (.credentials.json,
    credentials.json, .claude.json) AND then updates the macOS Keychain
    entries so a freshly launched `claude` picks up the new token without
    needing CLAUDE_CONFIG_DIR.
    """
    # Update the first applicable credentials file found.  `break` (not
    # `return`) so the Keychain update below still runs.
    for filename in [".credentials.json", "credentials.json", ".claude.json"]:
        path = os.path.join(config_dir, filename)
        if not os.path.exists(path):
            continue
        data = _load_json_safe(path)
        if "claudeAiOauth" in data:
            data["claudeAiOauth"]["accessToken"] = access_token
            if expires_at is not None:
                data["claudeAiOauth"]["expiresAt"] = expires_at
        elif "accessToken" in data:
            data["accessToken"] = access_token
            if expires_at is not None:
                data["expiresAt"] = expires_at
        else:
            continue
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        break

    # Update the macOS Keychain so Claude Code picks up the refreshed token
    # even when launched fresh without CLAUDE_CONFIG_DIR.
    try:
        kc = _read_keychain_credentials(config_dir)
        if kc:
            if "claudeAiOauth" in kc:
                kc["claudeAiOauth"]["accessToken"] = access_token
                if expires_at is not None:
                    kc["claudeAiOauth"]["expiresAt"] = expires_at
            else:
                kc["accessToken"] = access_token
                if expires_at is not None:
                    kc["expiresAt"] = expires_at
            _write_keychain_credentials(kc, service=_keychain_service_name(config_dir))
            # Only touch the legacy (no-hash) entry if THIS account is the
            # one currently active system-wide; otherwise a background refresh
            # of a non-active account would clobber the active account's
            # credentials in the shared "Claude Code-credentials" entry.
            active_dir = ""
            try:
                with open(_active_dir_pointer_path()) as f:
                    active_dir = f.read().strip()
            except Exception:
                pass
            if active_dir and os.path.abspath(active_dir) == os.path.abspath(config_dir):
                _write_keychain_credentials(kc, service="Claude Code-credentials")
    except Exception as e:
        logger.warning("Failed to update Keychain after token refresh for %s: %s", config_dir, e)
