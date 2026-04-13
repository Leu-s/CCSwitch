"""
Credential reading and writing helpers.

Handles macOS Keychain access and file-based credential fallbacks.
"""
import hashlib
import json
import logging
import os
import subprocess

logger = logging.getLogger(__name__)


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


def _write_keychain_credentials(config_dir: str, credentials: dict) -> bool:
    """Returns True if credentials were successfully written, False otherwise."""
    import getpass
    service = _keychain_service_name(config_dir)
    acct = getpass.getuser()
    cred_json = json.dumps(credentials)
    try:
        subprocess.run(
            ["security", "delete-generic-password", "-s", service, "-a", acct],
            capture_output=True, timeout=5,
        )
        result = subprocess.run(
            ["security", "add-generic-password", "-s", service, "-a", acct, "-w", cred_json],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            logger.warning("Keychain write failed for %s: %s", service, result.stderr.strip())
            return False
        logger.debug("Keychain credentials written for %s", service)
        return True
    except Exception as e:
        logger.warning("Keychain write exception for %s: %s", service, e)
        return False


def _write_keychain_credentials_legacy(credentials: dict) -> bool:
    """Returns True if credentials were successfully written, False otherwise."""
    import getpass
    service = "Claude Code-credentials"
    acct = getpass.getuser()
    cred_json = json.dumps(credentials)
    try:
        subprocess.run(
            ["security", "delete-generic-password", "-s", service, "-a", acct],
            capture_output=True, timeout=5,
        )
        result = subprocess.run(
            ["security", "add-generic-password", "-s", service, "-a", acct, "-w", cred_json],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            logger.warning("Legacy Keychain write failed: %s", result.stderr.strip())
            return False
        logger.debug("Legacy Keychain credentials updated for active account")
        return True
    except Exception as e:
        logger.warning("Legacy Keychain write exception: %s", e)
        return False


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
