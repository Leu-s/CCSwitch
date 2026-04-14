"""
Account lifecycle service.

Each managed account has an isolated Claude config directory under
``~/.ccswitch-accounts/account-{uuid}/``.  Claude Code reads credentials
from whatever ``CLAUDE_CONFIG_DIR`` points at, and every isolated dir has its
own Keychain entry keyed by ``sha256(config_dir)[:8]``.

Credential targets — the user-controlled mirror list
----------------------------------------------------
Different tools read ``oauthAccount`` from different ``.claude.json`` locations
on the same machine (HOME root, HOME/.claude/, Gas Town, cmux, …).  This
service no longer guesses which of those to update on a switch.  The user
picks explicit targets in the dashboard; ``activate_account_config`` takes
that list and delegates identity-key mirroring to
``credential_targets.mirror_oauth_into_targets``.

When the user enables a "system default" target — either ``$HOME/.claude.json``
or ``$HOME/.claude/.claude.json`` — this service ALSO writes the legacy
(no-hash) ``Claude Code-credentials`` Keychain entry and mirrors
``.credentials.json`` into ``~/.claude/``, so a fresh ``claude`` invocation
without ``CLAUDE_CONFIG_DIR`` picks up the switch.  With zero targets enabled,
only the dashboard's own pointer file is touched.
"""

import asyncio
import getpass
import logging
import os
import shutil
import subprocess
import threading
import time


from ..config import settings
from ..models import Account
from ..schemas import UsageData
from .credential_provider import (
    LEGACY_KEYCHAIN_SERVICE,
    _load_json_safe as _load_json,
    _read_keychain_credentials,
    _write_keychain_credentials,
    active_dir_pointer_path,
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
    """The directory Claude Code uses as CLAUDE_CONFIG_DIR's fallback
    (~/.claude by default). Used for commands/, agents/, settings.json,
    history.jsonl, etc. — NOT for the .claude.json config file."""
    return os.path.expanduser(settings.active_claude_dir)


def active_config_file() -> str:
    """Absolute path of the .claude.json file that Claude Code reads for
    oauthAccount state when CLAUDE_CONFIG_DIR is unset. Lives in HOME, not
    inside active_claude_dir()."""
    return os.path.join(os.path.expanduser("~"), ".claude.json")


# ── Active (system) config helpers ────────────────────────────────────────────

def get_active_email() -> str | None:
    """Return the email of the currently-active account.

    Reads the dashboard's pointer file first — that is the authoritative
    source of "what this service thinks is active", independent of whether
    the user has enabled any system-level credential target.  Falls back to
    ``$HOME/.claude.json`` for the cold-start case (service just installed,
    no switch yet) and then to ``$HOME/.claude/.claude.json`` so upgrades
    from older installs still work.
    """
    pointer = get_active_config_dir_pointer()
    if pointer:
        data = _load_json(os.path.join(pointer, ".claude.json"))
        email = (data.get("oauthAccount") or {}).get("emailAddress")
        if email:
            return email
    data = _load_json(active_config_file())
    email = (data.get("oauthAccount") or {}).get("emailAddress")
    if email:
        return email
    legacy = _load_json(os.path.join(active_claude_dir(), ".claude.json"))
    return (legacy.get("oauthAccount") or {}).get("emailAddress")


async def get_active_email_async() -> str | None:
    """Async wrapper for get_active_email — runs the blocking file reads in a
    worker thread so the event loop stays responsive."""
    return await asyncio.to_thread(get_active_email)


def get_active_config_dir_pointer() -> str | None:
    """Read ~/.ccswitch/active to find which isolated account dir is
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
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w") as f:
            f.write(backup["claude_json"])
        os.replace(tmp_path, path)
    except Exception:
        # Clean up orphaned temp file on write failure
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
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
    Remove the ~/.ccswitch/active pointer file so that new terminal
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
    Write the active account's isolated config_dir to ~/.ccswitch/active.
    Users can add to their shell profile:
        _d=$(cat ~/.ccswitch/active 2>/dev/null); [ -n "$_d" ] && export CLAUDE_CONFIG_DIR="$_d"; unset _d
    This ensures new terminal sessions use the correct account without Keychain gymnastics.
    File is written with mode 0o600 (owner-read/write only) since the path is sensitive.

    Raises on any failure — callers in ``_activate_account_config_locked``
    assume success, so a silently-swallowed write would leave HOME/Keychain
    holding the new identity while the pointer still references the previous
    account (the very split-brain state the credential lock was designed to
    prevent).
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
        logger.error("Failed to write active config dir file: %s", e)
        raise


def _clear_stale_legacy_keychain_entries() -> None:
    """
    Delete any 'Claude Code-credentials' Keychain entries whose account name
    is NOT the current $USER. Older Claude Code versions wrote with
    acct='claude-code', and they linger forever because find-generic-password
    returns the newest/most-recent one by a heuristic we do not control — any
    remaining stale entry is a landmine that can surface with the wrong token
    if the user ever downgrades or runs a different Claude Code build.
    """
    service = LEGACY_KEYCHAIN_SERVICE
    user = getpass.getuser()
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


def _system_default_canonicals() -> set[str]:
    """Canonical (symlink-resolved) paths Claude Code reads when
    CLAUDE_CONFIG_DIR is unset.  Enabling *either* as a credential target
    means the legacy Keychain entry and ``~/.claude/.credentials.json`` must
    also move with the switch — that is the combination a fresh ``claude``
    run pairs together."""
    home = os.path.expanduser("~")
    return {
        os.path.realpath(os.path.join(home, ".claude.json")),
        os.path.realpath(os.path.join(home, ".claude", ".claude.json")),
    }


def activate_account_config(
    target_config_dir: str,
    enabled_credential_targets: list[str] | None = None,
) -> dict:
    """
    Make ``target_config_dir`` the active account.

    Steps (order matters — the failure-prone credential install runs FIRST so
    the identity mirror and pointer are only reached when the credentials
    were actually installed):

      1. If any enabled target is a system-default location
         (``$HOME/.claude.json`` or ``$HOME/.claude/.claude.json``):
           a. Atomically copy ``.credentials.json`` into ``~/.claude/`` via a
              tmp file + ``os.replace`` in the same directory.  A failure
              here raises BEFORE any identity state is touched, so the
              previous account stays intact.
           b. Write the legacy ``Claude Code-credentials`` Keychain entry.
           c. Clean stale Keychain entries left by older Claude Code versions.
      2. Mirror ``oauthAccount`` + ``userID`` from
         ``target_config_dir/.claude.json`` into every file in
         ``enabled_credential_targets`` (canonical paths).  With an empty
         list, no ``.claude.json`` files outside the isolated account dir
         are touched.
      3. Update ``~/.ccswitch/active`` so the shell-profile snippet picks
         up the new account in brand-new terminals that source it.

    Acquires ``credential_provider._credential_lock`` for the full body so a
    background token refresh running in another thread cannot interleave
    between the legacy-Keychain write and the pointer update — see the lock's
    docstring in ``credential_provider.py``.

    Returns a summary dict:
        {
          "mirror": {"written": [...], "skipped": [...], "errors": [...]},
          "keychain_written": bool,
          "system_default_enabled": bool,
        }
    """
    from .credential_provider import _credential_lock  # local import avoids cycle

    with _credential_lock:
        return _activate_account_config_locked(
            target_config_dir, list(enabled_credential_targets or [])
        )


def _atomic_copy_credentials(src: str, dst: str) -> None:
    """Copy ``src`` → ``dst`` via a same-dir tmp file + ``os.replace``.

    Matches the tmp-path pattern in ``write_active_config_dir`` (pid + tid
    suffix).  On any exception the tmp file is unlinked so failed attempts
    do not leave junk behind.  The source is copied with ``shutil.copy2``
    into the tmp path so metadata (mode/mtime) is preserved, exactly like
    the previous single-step ``shutil.copy2`` call.
    """
    tmp = f"{dst}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _activate_account_config_locked(
    target_config_dir: str, enabled_targets: list[str]
) -> dict:
    from . import credential_targets as ct

    target_config_dir = os.path.abspath(os.path.expanduser(target_config_dir))

    # ── 1. System-default hooks (plaintext copy + legacy Keychain) ───────────
    # Run the FAILURE-PRONE credential install BEFORE any identity mirroring
    # so a copy failure cannot leave HOME/.claude.json or the Keychain holding
    # the new identity while the pointer still points at the previous account.
    system_defaults = _system_default_canonicals()
    system_default_enabled = bool(set(enabled_targets) & system_defaults)

    keychain_written = False
    if system_default_enabled:
        claude_dir = active_claude_dir()
        try:
            os.makedirs(claude_dir, exist_ok=True)
        except Exception:
            pass
        src_creds = os.path.join(target_config_dir, ".credentials.json")
        if os.path.exists(src_creds):
            # Atomic copy — raises on failure so steps 2 + 3 never run and
            # the previous pointer/HOME identity stay as the best available
            # fallback.
            _atomic_copy_credentials(
                src_creds, os.path.join(claude_dir, ".credentials.json")
            )

        kc = _read_keychain_credentials(target_config_dir)
        if kc:
            keychain_written = _write_keychain_credentials(
                kc, service=LEGACY_KEYCHAIN_SERVICE
            )
            if not keychain_written:
                logger.warning(
                    "Legacy Keychain write failed for %s — a fresh `claude` "
                    "run may still use stale credentials.",
                    target_config_dir,
                )
        else:
            logger.warning(
                "No Keychain entry found for %s — new terminals will use "
                "whatever credentials currently live under 'Claude Code-credentials'.",
                target_config_dir,
            )
        _clear_stale_legacy_keychain_entries()

    # ── 2. Mirror identity keys into every user-enabled target file ──────────
    mirror_summary = ct.mirror_oauth_into_targets(target_config_dir, enabled_targets)

    # ── 3. Update active-dir pointer file ────────────────────────────────────
    # Written AFTER credential operations so the pointer is never advanced to
    # a target whose credentials were not fully installed.
    write_active_config_dir(target_config_dir)

    return {
        "mirror": mirror_summary,
        "keychain_written": keychain_written,
        "system_default_enabled": system_default_enabled,
    }


def sync_active_to_targets(
    enabled_credential_targets: list[str] | None = None,
) -> dict:
    """Re-mirror the currently active account's identity into every enabled
    credential target without performing an account switch.

    Used by the "Sync now" button: the user has just enabled a new target and
    wants to backfill it immediately so a fresh ``claude`` invocation reads
    the right account, instead of waiting until the next switch.

    Reads the active config dir from inside the credential lock so a
    concurrent switch cannot race with us choosing the wrong dir.  Returns
    the same summary shape as ``activate_account_config``.
    """
    from .credential_provider import _credential_lock  # local import avoids cycle

    with _credential_lock:
        pointer = get_active_config_dir_pointer()
        if not pointer or not os.path.isdir(pointer):
            return {
                "mirror": {
                    "written": [],
                    "skipped": [],
                    "errors": [
                        "no active account — switch to an account first, then sync"
                    ],
                },
                "keychain_written": False,
                "system_default_enabled": False,
            }
        return _activate_account_config_locked(
            pointer, list(enabled_credential_targets or [])
        )


# ── Manual refresh ─────────────────────────────────────────────────────────────

# Per-config-dir async locks serializing concurrent force-refresh callers.
# Anthropic rotates refresh_tokens on every /oauth/token call and the old one
# immediately 404s on reuse, so two concurrent force-refreshes would race:
# the first rotates the stored token, the second — still holding the old one
# in memory — fails and (without this lock) would mark the now-fresh
# credentials stale.  Safe without a threading guard because all callers are
# dispatched on the FastAPI event-loop thread; the dict get + assign runs
# atomically between awaits.
_force_refresh_locks: dict[str, asyncio.Lock] = {}


def _get_force_refresh_lock(config_dir: str) -> asyncio.Lock:
    lock = _force_refresh_locks.get(config_dir)
    if lock is None:
        lock = asyncio.Lock()
        _force_refresh_locks[config_dir] = lock
    return lock


async def force_refresh_config_dir(config_dir: str) -> dict:
    """Force-refresh OAuth tokens for ``config_dir`` and persist the new pair.

    Intended for the "CCSwitch enabled, Claude Code not running" case where
    the user explicitly asks us to refresh an account whose access token has
    expired.  Under the active-ownership model the poll loop never refreshes
    the active account by itself — this endpoint is the escape hatch.

    Caller's responsibility: only call this when you are confident no other
    process (e.g., a running Claude Code CLI) is about to refresh the same
    refresh token.  The button in the UI is surfaced only while the active
    card is in the ``waiting_for_cli`` soft state, so the user has already
    acknowledged no CLI is running.

    Returns the freshly-read ``token_info`` dict (including the new
    ``token_expires_at``) on success.  Raises:

    * ``ValueError`` — no refresh token stored for this config dir.  The
      router maps this to ``409`` (user must re-login).
    * ``RuntimeError`` — upstream responded 200 but without an
      ``access_token`` field.  Mapped to ``502`` (upstream bug, retryable).
    * ``httpx.HTTPStatusError`` — upstream 4xx/5xx.  The router maps 400/401
      to ``409`` + stale_reason; everything else is ``502``.
    * Any other exception surfaces untouched.

    Concurrency: acquires a per-``config_dir`` ``asyncio.Lock`` across the
    full read → HTTP → Keychain-write sequence so two simultaneous callers
    cannot burn the same refresh_token.  The underlying Keychain write still
    uses ``_credential_lock`` inside ``save_refreshed_token``.
    """
    from . import anthropic_api  # local import avoids cycle

    async with _get_force_refresh_lock(config_dir):
        refresh_token = await asyncio.to_thread(
            get_refresh_token_from_config_dir, config_dir
        )
        if not refresh_token:
            raise ValueError("No refresh token stored for this account — re-login required")

        resp = await anthropic_api.refresh_access_token(refresh_token)
        new_token = resp.get("access_token")
        if not new_token:
            raise RuntimeError("Refresh response missing access_token")

        expires_in = resp.get("expires_in")
        new_expires_at_ms = (
            int(time.time() * 1000) + int(expires_in) * 1000 if expires_in else None
        )
        new_refresh = resp.get("refresh_token")

        await asyncio.to_thread(
            save_refreshed_token, config_dir, new_token,
            new_expires_at_ms, new_refresh,
        )
        logger.info("Force-refreshed access token for %s", config_dir)

        return await asyncio.to_thread(get_token_info, config_dir)


# ── Usage helpers ──────────────────────────────────────────────────────────────

def build_usage(usage_raw: dict, token_info: dict) -> "UsageData | None":
    """Convert a raw nested usage cache entry + token_info dict into a flat
    UsageData.  Public wrapper around UsageData.from_raw so callers outside
    the routers package can access it without importing schemas directly."""
    return UsageData.from_raw(usage_raw, token_info)


async def build_ws_snapshot(db) -> list[dict]:
    """Build the initial WS snapshot from cache + DB id map.

    Used by the /ws endpoint to send the full state to a freshly connected
    client without duplicating the cache → UsageData flattening logic.

    Carries ``waiting_for_cli`` and ``stale_reason`` so a reconnecting tab
    does not flash a healthy card for up to one poll cycle when either flag
    is active — see Phase 3 review notes (build_ws_snapshot used to drop
    both and each reconnect momentarily rendered every waiting card as
    healthy until the next usage_updated broadcast arrived).
    """
    from . import account_queries as aq
    from ..cache import cache as _cache
    from ..models import Account as _Account
    from sqlalchemy import select as _select

    cache_snapshot = await _cache.snapshot()
    if not cache_snapshot:
        return []
    id_map = await aq.get_email_to_id_map(db)
    # Pull stale_reason for every referenced email in one shot so the
    # snapshot stays aligned with the DB.  A freshly reconnecting tab relies
    # on this to render stale banners without waiting for the next poll.
    result = await db.execute(_select(_Account.email, _Account.stale_reason))
    stale_by_email = {email: reason for email, reason in result.all()}
    snapshot = []
    for email, usage in cache_snapshot.items():
        acct_id = id_map.get(email)
        if acct_id is None:
            continue
        token_info = await _cache.get_token_info_async(email) or {}
        flat = build_usage(usage, token_info)
        snapshot.append({
            "id": acct_id,
            "email": email,
            "usage": flat.model_dump() if flat else {},
            "error": usage.get("error"),
            "waiting_for_cli": await _cache.is_waiting_async(email),
            "stale_reason": stale_by_email.get(email),
        })
    return snapshot
