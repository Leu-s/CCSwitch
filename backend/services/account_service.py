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
from . import account_queries as aq
from . import anthropic_api
from . import credential_provider as cp


logger = logging.getLogger(__name__)


# ── Paths ──────────────────────────────────────────────────────────────────
#
# Claude Code CLI reads two different files at two different locations:
#
#   ~/.claude.json           — identity file (oauthAccount, userID).  Lives
#                              at HOME ROOT, NOT inside ~/.claude/.  This is
#                              what the CLI consults on every startup when
#                              CLAUDE_CONFIG_DIR is unset.
#   ~/.claude/.credentials.json  — token fallback inside the config dir.
#
# Writing ``oauthAccount`` to the wrong file (~/.claude/.claude.json) is
# invisible to the CLI, so /stats keeps showing the previous identity even
# after a successful Keychain swap.

_HOME = os.path.expanduser("~")
_CLAUDE_HOME = os.path.join(_HOME, ".claude")
_CLAUDE_JSON_PATH = os.path.join(_HOME, ".claude.json")
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

    Creates the parent directory if it does not already exist.  Callers
    are expected to pass a path inside a known location (the HOME root,
    ``~/.claude``, or a test tmpdir fixture); there is no defensive
    validation of ``path`` beyond the parent-dir creation — this helper
    is module-private in spirit and is not intended for arbitrary paths.

    Raises on failure so callers that assume the write succeeded can
    treat a swap as failed.
    """
    parent = os.path.dirname(path) or "."
    if not os.path.isdir(parent):
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
    # The identity file write (step 4) needs oauthAccount + userID from
    # the incoming blob.  Refuse to proceed if either is missing — without
    # them the CLI's /stats would keep showing the pre-swap identity even
    # though the standard Keychain entry was rewritten, and the startup
    # integrity check would try to reconcile from a vault blob that still
    # lacks the metadata it needs.
    if not isinstance(incoming.get("oauthAccount"), dict):
        raise SwapError(
            f"Cannot activate {target_email}: vault entry has no oauthAccount "
            "(re-login required)"
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
    # If fresh_standard carries a fresh oauthAccount / userID (the CLI
    # sometimes writes both), prefer them so the vault learns about
    # upstream identity changes.  Both are gated on a truthy value so a
    # stale/empty field in the standard entry cannot clobber the vault's
    # existing identity metadata.
    fresh_oauth = fresh_standard.get("oauthAccount")
    if isinstance(fresh_oauth, dict) and fresh_oauth:
        merged["oauthAccount"] = fresh_oauth
    fresh_user_id = fresh_standard.get("userID")
    if fresh_user_id:
        merged["userID"] = fresh_user_id
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


# ── revalidate_account (on-demand stale recovery) ─────────────────────────
#
# Per-email async locks serialise concurrent revalidate calls on the same
# account.  Critical: refresh_tokens are single-use; two concurrent calls
# with the same token would have the losing call get 400 invalid_grant
# and overwrite the winner's success with a terminal stale_reason.
_revalidate_locks: dict[str, asyncio.Lock] = {}


def _get_revalidate_lock(email: str) -> asyncio.Lock:
    # dict.setdefault is a single atomic insert-or-return in CPython so
    # two callers racing on the same email cannot end up with two
    # distinct Lock objects (which would defeat the serialisation goal).
    return _revalidate_locks.setdefault(email, asyncio.Lock())


def forget_revalidate_lock(email: str) -> None:
    """Drop the per-email revalidate lock.  Call on account delete so the
    dict doesn't grow unbounded across the app lifetime."""
    _revalidate_locks.pop(email, None)


async def revalidate_account(account_id: int, db) -> dict | None:
    """Run a single on-demand refresh attempt for a stale **vault** account.

    Used by the new ``POST /api/accounts/{id}/revalidate`` endpoint so the
    user can recover accounts that were marked ``stale_reason`` by the poll
    loop's transient-failure escalation without going through the full
    re-login tmux flow.

    **Invariant:** this function refuses to operate on the currently-active
    account.  The CLI owns the active account's refresh lifecycle (see
    CLAUDE.md §"Credential storage") and racing it would corrupt the single-
    use refresh_token.  Users with a phantom-stale active account should
    switch to another account first, then revalidate the now-vault entry.

    Returns ``None`` if the account does not exist.  Otherwise returns:

        {
          "success":         bool,
          "stale_reason":    str | None,   # value after this call
          "email":           str,
          "active_refused":  bool,         # True iff we refused because
                                           # the account is currently active
        }

    On success: ``stale_reason`` is cleared in the DB, fresh tokens are
    written to the vault via ``save_refreshed_vault_token``, and the in-
    memory refresh-backoff counters for the email are cleared.

    On failure: the precise reason is written to ``stale_reason`` so the
    caller and the UI can show an accurate message (a genuine
    ``invalid_grant`` stays stuck; a transient 400 reflects "try again
    later").
    """
    # Late import to avoid circular on background module.
    from .. import background as bg

    account = await aq.get_account_by_id(account_id, db)
    if account is None:
        return None

    email = account.email

    # ── Invariant guard: refuse active-account revalidate ────────────────
    active_email = await get_active_email_async()
    if email == active_email:
        # Returned stale_reason describes the refusal for the UI; the
        # DB-persisted ``account.stale_reason`` is deliberately left alone
        # so we don't clobber the real diagnostic with a transient guard
        # message for an operation we refused.
        return {
            "success": False,
            "stale_reason": (
                "Cannot revalidate the active account — the Claude Code "
                "CLI owns this account's refresh token lifecycle. Switch "
                "to another account first, then revalidate."
            ),
            "email": email,
            "active_refused": True,
        }

    # ── Serialise concurrent calls on the same email ─────────────────────
    lock = _get_revalidate_lock(email)
    async with lock:
        credentials = read_credentials_for_email(email, active_email)
        if not credentials:
            account.stale_reason = "No access token in vault — re-login required"
            await db.commit()
            return {
                "success": False,
                "stale_reason": account.stale_reason,
                "email": email,
                "active_refused": False,
            }

        refresh_token = cp.refresh_token_of(credentials)
        if not refresh_token:
            account.stale_reason = "No refresh token in vault — re-login required"
            await db.commit()
            return {
                "success": False,
                "stale_reason": account.stale_reason,
                "email": email,
                "active_refused": False,
            }

        import httpx
        import time as _time

        try:
            resp = await anthropic_api.refresh_access_token(refresh_token)
        except httpx.HTTPStatusError as refresh_err:
            kind = anthropic_api.parse_oauth_error(refresh_err)
            if kind is anthropic_api.OAuthErrorKind.TERMINAL_REVOKED:
                account.stale_reason = "Refresh token revoked — re-login required"
            elif kind is anthropic_api.OAuthErrorKind.TERMINAL_REJECTED:
                account.stale_reason = "Refresh token rejected — re-login required"
            else:
                account.stale_reason = (
                    f"Refresh endpoint transient failure "
                    f"(HTTP {refresh_err.response.status_code}) — try again later"
                )
            await db.commit()
            return {
                "success": False,
                "stale_reason": account.stale_reason,
                "email": email,
                "active_refused": False,
            }
        except (httpx.RequestError, RuntimeError) as net_err:
            logger.warning("Refresh network error for %s: %s", email, net_err)
            account.stale_reason = "Refresh network error — try again later"
            await db.commit()
            return {
                "success": False,
                "stale_reason": account.stale_reason,
                "email": email,
                "active_refused": False,
            }

        new_token = resp.get("access_token")
        if not new_token:
            account.stale_reason = "Refresh succeeded but response had no access_token"
            await db.commit()
            return {
                "success": False,
                "stale_reason": account.stale_reason,
                "email": email,
                "active_refused": False,
            }

        expires_in = resp.get("expires_in")
        new_expires_at_ms = (
            int(_time.time() * 1000) + int(expires_in) * 1000
            if expires_in
            else None
        )
        new_refresh = resp.get("refresh_token")

        await asyncio.to_thread(
            cp.save_refreshed_vault_token,
            email, new_token, new_expires_at_ms, new_refresh,
        )

        # Clear backoff counters — next poll will re-probe normally.
        bg._refresh_backoff_until.pop(email, None)
        bg._refresh_backoff_count.pop(email, None)
        bg._refresh_backoff_first_failure_at.pop(email, None)

        account.stale_reason = None
        await db.commit()

        return {
            "success": True,
            "stale_reason": None,
            "email": email,
            "active_refused": False,
        }
