"""
Account lifecycle service — vault-swap architecture.

Every managed account has one Keychain entry in the ``ccswitch-vault``
service (see ``credential_provider.py``).  The currently active account's
credentials also live in the standard ``Claude Code-credentials`` entry
that Claude Code CLI reads on every API call.  A "swap" moves credentials
from vault → standard, checkpoints the outgoing account back into the
vault, and updates ``~/.claude/.claude.json`` with the new identity.

There are no per-account config directories and no pointer file.  The
active account is the one whose email appears in
``~/.claude/.claude.json``'s ``oauthAccount``.
"""

import asyncio
import json
import logging
import os
import threading

from ..schemas import UsageData
from . import credential_provider as cp


logger = logging.getLogger(__name__)


# ── Paths ──────────────────────────────────────────────────────────────────
#
# Hardcoded to ``~/.claude`` — Claude Code has no public override for this
# location, and the previous configurable ``active_claude_dir`` setting was
# dead weight.

_CLAUDE_HOME = os.path.expanduser("~/.claude")
_CLAUDE_JSON_PATH = os.path.join(_CLAUDE_HOME, ".claude.json")
_CREDENTIALS_JSON_PATH = os.path.join(_CLAUDE_HOME, ".credentials.json")


# ── Helpers ────────────────────────────────────────────────────────────────


def _load_json_safe(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def _atomic_write_json(path: str, data: dict, mode: int = 0o600) -> None:
    """Write ``data`` as JSON to ``path`` via a same-dir tmp file + os.replace.

    Creates the parent directory if needed.  Raises on failure so callers
    that assume the write succeeded can treat a swap as failed.
    """
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, mode=0o700, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _extract_identity(credentials: dict | None) -> tuple[dict | None, str | None]:
    """Return (oauthAccount, userID) from a vault blob, or (None, None)."""
    if not credentials:
        return None, None
    oauth_account = credentials.get("oauthAccount")
    user_id = credentials.get("userID")
    return oauth_account if isinstance(oauth_account, dict) else None, user_id


def email_of_credentials(credentials: dict | None) -> str | None:
    """Extract the account email from a vault blob's ``oauthAccount`` field."""
    oauth_account, _ = _extract_identity(credentials)
    return (oauth_account or {}).get("emailAddress")


# ── Active account accessors ──────────────────────────────────────────────


def get_active_email() -> str | None:
    """Return the email of the currently-active account.

    The single source of truth is ``~/.claude/.claude.json``'s
    ``oauthAccount.emailAddress``.  The Claude Code CLI reads the same
    file to know which identity it is operating as.
    """
    data = _load_json_safe(_CLAUDE_JSON_PATH)
    oauth_account = data.get("oauthAccount") or {}
    email = oauth_account.get("emailAddress")
    return email or None


async def get_active_email_async() -> str | None:
    return await asyncio.to_thread(get_active_email)


# ── Vault helpers used by the poll loop ───────────────────────────────────


def read_credentials_for_email(email: str, active_email: str | None = None) -> dict | None:
    """Return the credentials dict for ``email``.

    For the active account, reads the standard Keychain entry (the CLI's
    home).  For inactive accounts, reads the vault.  Callers that already
    know which account is active pass ``active_email`` to avoid a second
    file read; otherwise this helper does the lookup itself.
    """
    if active_email is None:
        active_email = get_active_email()
    if email == active_email:
        return cp.read_standard()
    return cp.read_vault(email)


def get_token_info(email: str, active_email: str | None = None) -> dict:
    return cp.token_info_of(read_credentials_for_email(email, active_email))


# ── The swap ───────────────────────────────────────────────────────────────


class SwapError(RuntimeError):
    """Raised when swap_to_account cannot complete atomically."""


def swap_to_account(target_email: str) -> dict:
    """Activate ``target_email`` by moving credentials into the standard
    Keychain entry and rewriting the identity file.

    Runs the 5-step sequence from §2.4 of the design spec inside the
    credential lock.  Returns a summary dict::

        {
            "target_email": "...",
            "previous_email": "..." | None,
            "checkpoint_written": bool,
        }

    Raises ``SwapError`` if the vault entry for ``target_email`` is
    missing or the standard Keychain write fails.  Step 4 (identity
    file) and step 5 (file fallback) failures are logged and re-raised
    — the standard Keychain entry already reflects the new account at
    that point, so the user is told the swap half-committed.
    """
    with cp._credential_lock:
        return _swap_to_account_locked(target_email)


def _swap_to_account_locked(target_email: str) -> dict:
    # ── Step 1: load incoming ─────────────────────────────────────────────
    incoming = cp.read_vault(target_email)
    if not incoming:
        raise SwapError(
            f"Cannot activate {target_email}: no vault entry (re-login required)"
        )
    if not cp.refresh_token_of(incoming):
        raise SwapError(
            f"Cannot activate {target_email}: vault entry has no refresh token"
        )

    # ── Step 2: checkpoint outgoing ───────────────────────────────────────
    current_standard = cp.read_standard()
    outgoing_email = email_of_credentials(current_standard)
    checkpoint_written = False
    if (
        current_standard
        and outgoing_email
        and outgoing_email != target_email
    ):
        merged = _merge_checkpoint(outgoing_email, current_standard)
        if cp.write_vault(outgoing_email, merged):
            checkpoint_written = True
        else:
            # Keychain write failures are rare but catastrophic — we are
            # about to overwrite the standard entry and would lose the
            # CLI's latest rotation if the checkpoint failed.  Refuse.
            raise SwapError(
                f"Failed to checkpoint outgoing account {outgoing_email} "
                f"to vault — aborting swap to {target_email}"
            )
    elif current_standard and not outgoing_email:
        # Orphan: standard entry has tokens but no oauthAccount metadata.
        # Stash under a well-known key so the user can see and clean up
        # in the Settings page.  Never silently drop.
        logger.warning(
            "Standard entry has no oauthAccount — stashing under "
            "ccswitch-vault/__orphan_unknown__ before overwrite"
        )
        cp.write_vault("__orphan_unknown__", current_standard)

    # ── Step 3: promote incoming ──────────────────────────────────────────
    if not cp.write_standard(incoming):
        raise SwapError(
            f"Failed to write standard Keychain entry for {target_email}"
        )

    # ── Step 4: update identity file ──────────────────────────────────────
    _rewrite_claude_json_identity(incoming)

    # ── Step 5: file fallback ─────────────────────────────────────────────
    _atomic_write_json(_CREDENTIALS_JSON_PATH, incoming)

    return {
        "target_email": target_email,
        "previous_email": outgoing_email,
        "checkpoint_written": checkpoint_written,
    }


def _merge_checkpoint(outgoing_email: str, fresh_standard: dict) -> dict:
    """Build the vault blob for the outgoing account from its latest
    standard-entry state, preserving the existing oauthAccount + userID
    metadata that only lives in the vault."""
    previous_vault = cp.read_vault(outgoing_email) or {}
    merged = dict(previous_vault)
    # The CLI always writes the tokens nested under claudeAiOauth.  If the
    # standard entry has top-level token fields (legacy format), normalise
    # to nested so the vault is consistent.
    nested = fresh_standard.get("claudeAiOauth")
    if isinstance(nested, dict):
        merged["claudeAiOauth"] = nested
    else:
        token_fields = {
            k: fresh_standard[k]
            for k in ("accessToken", "refreshToken", "expiresAt", "subscriptionType")
            if k in fresh_standard
        }
        if token_fields:
            merged["claudeAiOauth"] = token_fields
    # If fresh_standard carries fresh oauthAccount / userID (the CLI
    # sometimes writes both), prefer them so the vault learns about
    # upstream identity changes.
    fresh_oauth = fresh_standard.get("oauthAccount")
    if isinstance(fresh_oauth, dict):
        merged["oauthAccount"] = fresh_oauth
    if "userID" in fresh_standard:
        merged["userID"] = fresh_standard["userID"]
    return merged


def _rewrite_claude_json_identity(credentials: dict) -> None:
    """Replace the oauthAccount + userID keys in ``~/.claude/.claude.json``,
    preserving every other key.  Creates the file (and parent dir) if missing."""
    data = _load_json_safe(_CLAUDE_JSON_PATH)
    oauth_account, user_id = _extract_identity(credentials)
    if oauth_account is not None:
        data["oauthAccount"] = oauth_account
    if user_id is not None:
        data["userID"] = user_id
    _atomic_write_json(_CLAUDE_JSON_PATH, data)


# ── Login / wipe helpers ──────────────────────────────────────────────────


def save_new_vault_account(
    email: str,
    oauth_tokens: dict,
    oauth_account: dict,
    user_id: str | None,
) -> bool:
    """Write a freshly-minted account (new or re-login) into the vault.

    ``oauth_tokens`` is the ``claudeAiOauth`` dict the CLI wrote to the
    scratch Keychain entry.  ``oauth_account`` + ``user_id`` come from the
    scratch ``.claude.json`` file.  Combines all three into the canonical
    vault blob shape.
    """
    blob: dict = {"claudeAiOauth": oauth_tokens}
    if isinstance(oauth_account, dict):
        blob["oauthAccount"] = oauth_account
    if user_id:
        blob["userID"] = user_id
    return cp.write_vault(email, blob)


def delete_account_everywhere(email: str) -> None:
    """Remove an account's credentials from the vault.  If the account is
    currently active, also clear the standard entry and the identity file.
    """
    with cp._credential_lock:
        cp.delete_vault(email)
        if get_active_email() == email:
            cp.delete_standard()
            data = _load_json_safe(_CLAUDE_JSON_PATH)
            data.pop("oauthAccount", None)
            data.pop("userID", None)
            try:
                _atomic_write_json(_CLAUDE_JSON_PATH, data)
            except Exception as e:
                logger.warning(
                    "Failed to strip oauthAccount from %s: %s",
                    _CLAUDE_JSON_PATH, e,
                )
            try:
                os.unlink(_CREDENTIALS_JSON_PATH)
            except FileNotFoundError:
                pass
            except Exception as e:
                logger.debug(
                    "Failed to remove %s: %s", _CREDENTIALS_JSON_PATH, e
                )


# ── Startup integrity check (§9.1) ────────────────────────────────────────


def startup_integrity_check() -> None:
    """Reconcile a crashed-mid-swap state on startup.

    If ``~/.claude/.claude.json``'s active email disagrees with the
    standard Keychain entry's ``oauthAccount.emailAddress`` (when both
    are readable), rewrite the identity file to match the Keychain — the
    Keychain is the later write in the swap sequence, so it wins.

    Logs a prominent warning if a mismatch is detected.  Does nothing
    when either side is empty or when they agree.
    """
    identity_email = get_active_email()
    standard = cp.read_standard()
    standard_email = email_of_credentials(standard)

    if not standard_email:
        return  # Standard entry has no identity metadata — nothing to reconcile.

    if not identity_email:
        # Identity file lacks an oauthAccount but the Keychain has one —
        # most likely a fresh install where the user ran `claude login`
        # before opening the CCSwitch dashboard.  Mirror the identity in.
        logger.info(
            "Startup: identity file missing oauthAccount; seeding from "
            "standard Keychain entry (email=%s)",
            standard_email,
        )
        try:
            _rewrite_claude_json_identity(standard)
        except Exception as e:
            logger.warning("Startup identity seed failed: %s", e)
        return

    if identity_email != standard_email:
        logger.warning(
            "Startup integrity: ~/.claude/.claude.json says %s but "
            "standard Keychain entry holds %s — reconciling to Keychain",
            identity_email, standard_email,
        )
        try:
            _rewrite_claude_json_identity(standard)
            _atomic_write_json(_CREDENTIALS_JSON_PATH, standard)
        except Exception as e:
            logger.warning("Startup integrity reconcile failed: %s", e)


# ── Usage helpers (unchanged from old service) ────────────────────────────


def build_usage(usage_raw: dict, token_info: dict) -> "UsageData | None":
    """Convert a raw usage cache entry + token_info into a flat UsageData.
    Public wrapper around UsageData.from_raw so callers outside routers can
    use it without importing schemas directly."""
    return UsageData.from_raw(usage_raw, token_info)


async def build_ws_snapshot(db) -> list[dict]:
    """Build the initial WebSocket snapshot for a freshly connected client.

    Returns a list of dicts shaped like the ``usage_updated`` broadcast
    entries so the client can render full state without waiting for the
    next poll cycle.
    """
    from . import account_queries as aq
    from ..cache import cache as _cache
    from ..models import Account as _Account
    from sqlalchemy import select as _select

    cache_snapshot = await _cache.snapshot()
    if not cache_snapshot:
        return []
    id_map = await aq.get_email_to_id_map(db)
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
            "stale_reason": stale_by_email.get(email),
        })
    return snapshot
