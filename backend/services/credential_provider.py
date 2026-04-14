"""
Credential reading and writing helpers.

Handles macOS Keychain access and file-based credential fallbacks.
"""
import getpass
import hashlib
import json
import logging
import os
import subprocess
import threading

from ..config import settings

logger = logging.getLogger(__name__)

# The legacy (no-hash) Keychain service name used by Claude Code when
# CLAUDE_CONFIG_DIR is unset.  Defined once here so both credential_provider
# and account_service reference the same constant.
LEGACY_KEYCHAIN_SERVICE = "Claude Code-credentials"

# Serializes every mutation to the four shared credential artefacts:
#   1. HOME .claude.json                            (written by activate_account_config)
#   2. Legacy "Claude Code-credentials" Keychain   (written by activate_account_config and save_refreshed_token)
#   3. Hashed per-dir Keychain                     (written by save_refreshed_token)
#   4. ~/.ccswitch/active pointer              (written by activate_account_config)
#
# A switch (activate_account_config) acquires this lock for the full multi-step
# dance, and a background token refresh (save_refreshed_token) acquires it for
# its own Keychain writes.  Without this, a refresh's "is this dir the active
# one?" check races with the switch's pointer update and can leave the legacy
# Keychain holding one account's refreshed token while HOME .claude.json holds
# another account's oauthAccount — exactly the "UI shows X, reality is Y" bug.
#
# Re-entrant so activate_account_config can call helpers that also want the
# lock without deadlocking itself.
_credential_lock = threading.RLock()


def active_dir_pointer_path() -> str:
    """Path of the file that records which isolated account dir is active.
    Derived from settings.state_dir so users who override CCSWITCH_STATE_DIR
    get a single, consistent location everywhere in the codebase.

    Canonical definition lives here (not in account_service) because
    account_service imports this module, not the other way around."""
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


def save_refreshed_token(
    config_dir: str,
    access_token: str,
    expires_at: int | None = None,
    refresh_token: str | None = None,
) -> None:
    """
    Persist a refreshed access token (and optionally a rotated refresh
    token) back into the config dir.

    Writes the first applicable credentials file (.credentials.json,
    credentials.json, .claude.json) AND then updates the macOS Keychain
    entries so a freshly launched `claude` picks up the new token without
    needing CLAUDE_CONFIG_DIR.

    Acquires ``_credential_lock`` for the full body so a background refresh
    cannot clobber a concurrent account switch.  See the lock's docstring.
    """
    with _credential_lock:
        _save_refreshed_token_locked(config_dir, access_token, expires_at, refresh_token)


def _save_refreshed_token_locked(
    config_dir: str, access_token: str, expires_at: int | None,
    refresh_token: str | None = None,
) -> None:
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
            if refresh_token is not None:
                data["claudeAiOauth"]["refreshToken"] = refresh_token
        elif "accessToken" in data:
            data["accessToken"] = access_token
            if expires_at is not None:
                data["expiresAt"] = expires_at
            if refresh_token is not None:
                data["refreshToken"] = refresh_token
        else:
            continue
        tmp_path = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass
            raise
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
                if refresh_token is not None:
                    kc["claudeAiOauth"]["refreshToken"] = refresh_token
            else:
                kc["accessToken"] = access_token
                if expires_at is not None:
                    kc["expiresAt"] = expires_at
                if refresh_token is not None:
                    kc["refreshToken"] = refresh_token
            _write_keychain_credentials(kc, service=_keychain_service_name(config_dir))
            # Only touch the legacy (no-hash) entry if THIS account is the
            # one currently active system-wide; otherwise a background refresh
            # of a non-active account would clobber the active account's
            # credentials in the shared "Claude Code-credentials" entry.
            #
            # Under the active-ownership refresh model the poll loop never
            # refreshes the currently-active account — so the only caller
            # that actually reaches this branch is ``force_refresh_config_dir``
            # (the user-triggered escape hatch).  The predicate stays as
            # defense in depth against future refactors that might put the
            # poll loop back in the active-account refresh business.
            active_dir = ""
            try:
                with open(active_dir_pointer_path()) as f:
                    active_dir = f.read().strip()
            except Exception:
                pass
            if active_dir and os.path.abspath(active_dir) == os.path.abspath(config_dir):
                _write_keychain_credentials(kc, service=LEGACY_KEYCHAIN_SERVICE)
    except Exception as e:
        logger.warning("Failed to update Keychain after token refresh for %s: %s", config_dir, e)


def wipe_credentials_for_config_dir(config_dir: str) -> None:
    """Remove all OAuth credentials belonging to an isolated config directory.

    Deletes the hashed per-dir Keychain entry, the credential files inside
    ``config_dir`` (``.credentials.json`` / ``credentials.json``), and strips
    the ``oauthAccount`` / ``userID`` keys from ``.claude.json`` — other keys
    in that file (projects, MCP state, etc.) are preserved.

    Used to roll back a re-login attempt where the user authenticated as a
    different account than the slot expects — this returns the config dir
    to the same "no credentials" state a stale account already has, so the
    user can try again without leaving a split-brain mix (one identity in
    ``.claude.json``, a different one in the Keychain entry).

    Best-effort: individual step failures are logged but do not abort the
    remaining steps, because a half-wiped slot is still safer than one
    where some of the bad credentials linger.

    Acquires ``_credential_lock`` for the same reason token refresh does —
    to not race with a concurrent switch or save_refreshed_token.
    """
    user = getpass.getuser()

    with _credential_lock:
        # 1. Hashed per-dir Keychain entry.  `security delete-generic-password`
        #    exits non-zero when the item does not exist; that is fine and we
        #    do not raise on it.
        service = _keychain_service_name(config_dir)
        try:
            subprocess.run(
                ["security", "delete-generic-password", "-s", service, "-a", user],
                capture_output=True, timeout=5,
            )
        except Exception as e:
            logger.debug("Keychain wipe for %s failed: %s", service, e)

        # 2. Credential files inside config_dir.
        for fname in (".credentials.json", "credentials.json"):
            path = os.path.join(config_dir, fname)
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            except Exception as e:
                logger.warning("wipe_credentials: failed to unlink %s: %s", path, e)

        # 3. Strip oauthAccount / userID from .claude.json while preserving
        #    every other key (projects, MCP state, user prefs, etc.).
        claude_json = os.path.join(config_dir, ".claude.json")
        if not os.path.exists(claude_json):
            return
        try:
            data = _load_json_safe(claude_json)
            if "oauthAccount" not in data and "userID" not in data:
                return
            data.pop("oauthAccount", None)
            data.pop("userID", None)
            tmp = f"{claude_json}.{os.getpid()}.{threading.get_ident()}.wipe.tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(data, f, indent=2)
                os.replace(tmp, claude_json)
            except Exception:
                try:
                    os.unlink(tmp)
                except FileNotFoundError:
                    pass
                raise
        except Exception as e:
            logger.warning("wipe_credentials: failed to strip oauthAccount from %s: %s", claude_json, e)
