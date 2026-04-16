"""
Credential reading and writing helpers for the vault-swap architecture.

Two disjoint Keychain namespaces:

1. ``Claude Code-credentials`` (STANDARD_SERVICE): the entry Claude Code CLI
   reads on every API call.  CCSwitch writes this only during a swap (the
   ``swap_to_account`` orchestrator in ``account_service``).  Background
   refresh never touches it — the CLI owns the active account's refresh
   lifecycle.

2. ``ccswitch-vault`` (VAULT_SERVICE): CCSwitch's private credential store,
   one entry per email.  CCSwitch writes these on login, during the swap
   checkpoint of the outgoing account, and on background refresh of
   inactive accounts.  The CLI cannot reach these — different service
   name, no enumeration API on the CLI side.

All mutations go through ``_credential_lock`` so a background refresh
cannot race a concurrent swap.

During the login flow a transient scratch dir (``$TMPDIR/ccswitch-login-*``)
holds a freshly-minted token in a CLI-hashed Keychain entry; the
``read_login_scratch`` / ``delete_login_scratch`` helpers promote that
material into the vault and clean up.
"""

import getpass
import hashlib
import json
import logging
import subprocess
import threading


logger = logging.getLogger(__name__)

# ── Keychain namespaces ────────────────────────────────────────────────────

STANDARD_SERVICE = "Claude Code-credentials"
VAULT_SERVICE = "ccswitch-vault"

_VAULT_COMMENT = (
    "CCSwitch subscription vault — do not delete. "
    "Managed by the CCSwitch dashboard at http://127.0.0.1:41924."
)

# Every ``security`` subprocess uses this timeout.  A locked Keychain can
# hang the call indefinitely otherwise.
_KEYCHAIN_SUBPROCESS_TIMEOUT = 5

# ── Concurrency ────────────────────────────────────────────────────────────
#
# Serializes every mutation to the two Keychain service namespaces so a
# background refresh (vault write) and a swap orchestrator (vault + standard
# writes) cannot interleave and leave the system with one account's fresh
# token in the vault while its identity file still points at another.
#
# Re-entrant so ``save_refreshed_vault_token`` can call ``read_vault``
# without deadlocking inside the same thread.
_credential_lock = threading.RLock()


# ── Low-level Keychain helpers ─────────────────────────────────────────────


def _find_password(service: str, account: str) -> dict | None:
    """Read a keychain entry and parse its value as JSON.

    Returns ``None`` on miss, parse failure, timeout, or any other
    error.  Callers treat ``None`` as "credentials not available";
    distinguishing "locked keychain" from "entry missing" is the job of
    ``probe_keychain_available``.
    """
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True,
            text=True,
            timeout=_KEYCHAIN_SUBPROCESS_TIMEOUT,
        )
        if result.returncode != 0:
            return None
        raw = result.stdout.strip()
        if not raw:
            return None
        return json.loads(raw)
    except subprocess.TimeoutExpired:
        logger.warning("Keychain read timed out for %s / %s", service, account)
        return None
    except json.JSONDecodeError:
        logger.warning("Keychain entry %s / %s is not valid JSON", service, account)
        return None
    except Exception as e:  # pragma: no cover — defensive
        logger.debug("Keychain lookup failed for %s / %s: %s", service, account, e)
        return None


def _add_password(
    service: str,
    account: str,
    credentials: dict,
    comment: str | None = None,
) -> bool:
    """Write (or overwrite) a keychain entry.  Returns ``True`` on success."""
    cred_json = json.dumps(credentials)
    try:
        # Idempotent overwrite: delete then add.  ``security`` has no single-
        # call update.  The narrow window between delete and add is covered
        # by ``_credential_lock`` for in-process concurrency; cross-process
        # races are out of scope (see §9.5 of the design spec).
        subprocess.run(
            ["security", "delete-generic-password", "-s", service, "-a", account],
            capture_output=True,
            timeout=_KEYCHAIN_SUBPROCESS_TIMEOUT,
        )
        cmd = ["security", "add-generic-password", "-s", service, "-a", account, "-w", cred_json]
        if comment:
            cmd.extend(["-j", comment])
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_KEYCHAIN_SUBPROCESS_TIMEOUT,
        )
        if result.returncode != 0:
            logger.warning(
                "Keychain add failed for %s / %s (rc=%s): %s",
                service, account, result.returncode, (result.stderr or "").strip(),
            )
            return False
        return True
    except subprocess.TimeoutExpired:
        logger.warning("Keychain write timed out for %s / %s", service, account)
        return False
    except Exception as e:  # pragma: no cover — defensive
        logger.warning("Keychain write raised for %s / %s: %s", service, account, e)
        return False


def _delete_password(service: str, account: str) -> None:
    """Best-effort delete.  Swallows "item not found" — that is the intended
    final state."""
    try:
        subprocess.run(
            ["security", "delete-generic-password", "-s", service, "-a", account],
            capture_output=True,
            timeout=_KEYCHAIN_SUBPROCESS_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        logger.warning("Keychain delete timed out for %s / %s", service, account)
    except Exception as e:  # pragma: no cover — defensive
        logger.debug("Keychain delete raised for %s / %s: %s", service, account, e)


# ── Standard entry (Claude Code-credentials) API ───────────────────────────
#
# The CLI's side of the partition.  CCSwitch writes only from inside the
# swap orchestrator; reads happen from the poll loop for active-account
# rate-limit probes.


def read_standard() -> dict | None:
    """Read the standard ``Claude Code-credentials`` Keychain entry.

    Returns the parsed credentials dict or ``None`` on miss.
    """
    return _find_password(STANDARD_SERVICE, getpass.getuser())


def write_standard(credentials: dict) -> bool:
    """Write credentials to the standard entry.  Acquires the credential
    lock.  Only called by the swap orchestrator — never by the background
    refresh, which must not touch the active account."""
    with _credential_lock:
        return _add_password(STANDARD_SERVICE, getpass.getuser(), credentials)


def delete_standard() -> None:
    """Delete the standard entry.  Used when the last account is removed."""
    with _credential_lock:
        _delete_password(STANDARD_SERVICE, getpass.getuser())


# ── Vault entry (ccswitch-vault) API ───────────────────────────────────────


def read_vault(email: str) -> dict | None:
    """Read the vault entry for ``email``.  Returns the parsed credentials
    dict or ``None`` on miss."""
    return _find_password(VAULT_SERVICE, email)


def write_vault(email: str, credentials: dict) -> bool:
    """Write credentials to ``ccswitch-vault / email``.

    Acquires the credential lock.  Returns ``True`` on success.
    """
    with _credential_lock:
        return _add_password(VAULT_SERVICE, email, credentials, comment=_VAULT_COMMENT)


def delete_vault(email: str) -> None:
    """Delete a vault entry.  No-op if missing."""
    with _credential_lock:
        _delete_password(VAULT_SERVICE, email)


# ── Token-field extraction ─────────────────────────────────────────────────
#
# The CLI writes OAuth credentials in two slightly different shapes
# depending on build / platform: either nested under ``claudeAiOauth`` or
# at the top level.  All reader helpers must tolerate both.


def _extract_field(credentials: dict | None, field: str) -> str | None:
    if not credentials:
        return None
    nested = credentials.get("claudeAiOauth") or {}
    return nested.get(field) or credentials.get(field)


def access_token_of(credentials: dict | None) -> str | None:
    return _extract_field(credentials, "accessToken")


def refresh_token_of(credentials: dict | None) -> str | None:
    return _extract_field(credentials, "refreshToken")


def token_info_of(credentials: dict | None) -> dict:
    """Return non-secret token metadata: expiry timestamp + subscription tier.
    Never returns the access or refresh token itself."""
    if not credentials:
        return {}
    nested = credentials.get("claudeAiOauth") or credentials
    result: dict = {}
    if nested.get("expiresAt"):
        result["token_expires_at"] = nested["expiresAt"]
    if nested.get("subscriptionType"):
        result["subscription_type"] = nested["subscriptionType"]
    return result


# ── Vault refresh persistence ──────────────────────────────────────────────


def _save_refreshed_vault_token_locked(
    email: str,
    access_token: str,
    expires_at: int | None = None,
    refresh_token: str | None = None,
) -> None:
    """Inner persist body — ASSUMES the caller already holds
    ``_credential_lock`` on the current thread.  Never call directly from
    outside this module; use ``save_refreshed_vault_token`` which wraps
    this with the appropriate lock-acquire policy."""
    current = read_vault(email) or {}
    if "claudeAiOauth" in current:
        current["claudeAiOauth"]["accessToken"] = access_token
        if expires_at is not None:
            current["claudeAiOauth"]["expiresAt"] = expires_at
        if refresh_token is not None:
            current["claudeAiOauth"]["refreshToken"] = refresh_token
    else:
        current["accessToken"] = access_token
        if expires_at is not None:
            current["expiresAt"] = expires_at
        if refresh_token is not None:
            current["refreshToken"] = refresh_token
    write_vault(email, current)


def save_refreshed_vault_token(
    email: str,
    access_token: str,
    expires_at: int | None = None,
    refresh_token: str | None = None,
) -> None:
    """Persist a refreshed access token (and optionally a rotated refresh
    token) back into the vault entry for ``email``.

    Only called from the background poll loop for inactive accounts and
    from ``revalidate_account``.  CCSwitch is the sole consumer of vault
    entries, so there is no race partner — the narrow write window
    inside ``_credential_lock`` is sufficient.
    """
    with _credential_lock:
        _save_refreshed_vault_token_locked(email, access_token, expires_at, refresh_token)


# ── Login-flow scratch entry helpers ───────────────────────────────────────
#
# The add-account and re-login flows launch `claude /login` in a tmux pane
# with ``CLAUDE_CONFIG_DIR=$scratch_dir``, so the CLI writes the freshly
# minted credentials to a hashed service name derived from that path.  We
# read them back, promote them to the vault, and delete the scratch
# hashed entry.  That hashed entry is the ONLY place in the new
# architecture where a ``Claude Code-credentials-<sha>`` namespace still
# exists — and it lives for seconds, not forever.


def _scratch_service_name(scratch_dir: str) -> str:
    h = hashlib.sha256(scratch_dir.encode()).hexdigest()[:8]
    return f"{STANDARD_SERVICE}-{h}"


def read_login_scratch(scratch_dir: str) -> dict | None:
    """Read the hashed Keychain entry Claude Code wrote for a login session
    whose ``CLAUDE_CONFIG_DIR`` was ``scratch_dir``."""
    return _find_password(_scratch_service_name(scratch_dir), getpass.getuser())


def delete_login_scratch(scratch_dir: str) -> None:
    """Delete the login scratch hashed entry after extraction."""
    with _credential_lock:
        _delete_password(_scratch_service_name(scratch_dir), getpass.getuser())


# ── Keychain availability probe ────────────────────────────────────────────


def probe_keychain_available() -> bool:
    """Return ``True`` if the login keychain is unlocked and readable.

    Used on startup (main.lifespan) to detect the LaunchAgent-at-boot case
    where the keychain may not yet be unlocked.  A short-circuit probe
    against a non-existent service is enough: ``find-generic-password``
    returns 44 (item not found) when the keychain is unlocked but the
    entry is absent, 51 (interaction not allowed) or 36 (locked keychain)
    when locked.
    """
    try:
        result = subprocess.run(
            [
                "security", "find-generic-password",
                "-s", "__ccswitch_probe_does_not_exist__",
                "-a", getpass.getuser(),
            ],
            capture_output=True,
            text=True,
            timeout=_KEYCHAIN_SUBPROCESS_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False
    except Exception:  # pragma: no cover — defensive
        return False
    # returncode 44 (item not found) means the keychain is unlocked and
    # just doesn't have that entry.  Anything else — 36 (locked), 51
    # (interaction not allowed), or another non-zero — means unavailable.
    return result.returncode == 44
