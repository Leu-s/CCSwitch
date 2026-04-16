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
from contextlib import asynccontextmanager

import httpx

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


class SwapRefreshTerminalError(SwapError):
    """Raised by step 0.5 when the incoming account's refresh_token is
    terminally rejected by Anthropic's OAuth endpoint.

    Distinct from ``SwapError`` so the switch orchestrator can mark the
    account's ``stale_reason`` in the DB and broadcast an ``account_updated``
    event — the stale state is stable and the user must Re-login before any
    future swap to this account can succeed.

    Carries ``target_email`` + ``reason`` (the exact stale_reason string).
    """

    def __init__(self, target_email: str, reason: str):
        super().__init__(
            f"Cannot activate {target_email}: {reason}.  "
            f"Click Re-login first to restore this account."
        )
        self.target_email = target_email
        self.reason = reason


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
    # INVARIANT: refresh_lock(email) is always acquired OUTSIDE
    # cp._credential_lock.  Poll-loop and Revalidate also follow this
    # order (refresh_lock first via ``with_refresh_lock_async``, then
    # cp._credential_lock via ``save_refreshed_vault_token``).  Reversing
    # the order would deadlock under contention: thread A holding
    # refresh_lock waiting for cp._credential_lock while thread B holds
    # cp._credential_lock waiting for refresh_lock.
    #
    # Holding the refresh_lock across the whole swap also covers step
    # 0.5's internal refresh — a concurrent async refresher on the same
    # email (poll-loop reactive refresh or Revalidate) cannot race the
    # swap-time refresh for the same single-use refresh_token because
    # both paths wait on the same threading.Lock.
    refresh_lock = get_refresh_lock(target_email)
    with refresh_lock:
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

    # ── Step 0.5: refresh incoming tokens on promotion ────────────────────
    # Ensures the CLI starts the newly-activated account with fresh
    # tokens (avoids a 401 on the user's first post-swap keypress).
    # Serialisation against a concurrent Revalidate or poll-reactive-
    # refresh on the same email is provided by the outer
    # ``refresh_lock(target_email)`` acquired in ``swap_to_account``
    # (held across this whole function) — so the inner HTTP refresh
    # here is already covered and MUST NOT re-acquire (threading.Lock
    # is non-reentrant, re-entrance would deadlock).  On terminal
    # failure this raises SwapError BEFORE the standard-entry
    # overwrite — user stays on the previous active account and is
    # told to Re-login.
    incoming = _refresh_incoming_on_promotion(target_email, incoming)

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


def _refresh_incoming_on_promotion(email: str, incoming: dict) -> dict:
    """Swap step 0.5: refresh the incoming vault entry's access_token before
    promoting it to the standard Keychain slot.  Minimises the window where
    a newly-promoted account's access_token is already near expiry and
    forces the CLI to 401 + self-refresh on the user's first keypress.

    Returns the ``incoming`` blob with fresh tokens folded into
    ``claudeAiOauth`` on success, or the original blob unchanged if:
      * the vault has no refresh_token (nothing to refresh), or
      * the refresh failed transiently (network, 5xx, below-threshold
        400 transient) — warning logged, swap continues with stored
        tokens and the CLI refreshes on its first call as it always
        does.

    Raises ``SwapError`` on terminal failure (invalid_grant, 401 with
    terminal OAuth body).  ``_swap_to_account_locked`` propagates that
    error — step 2 (checkpoint) and step 3 (standard-entry overwrite)
    never run, so the user stays on the previous active account with a
    clear "re-login first" error.

    **Lock-ordering note.**  Step 0.5's HTTP refresh is protected by the
    outer ``refresh_lock(target_email)`` acquired in ``swap_to_account``.
    No inner acquisition is needed — in fact it would deadlock because
    ``threading.Lock`` is non-reentrant.

    The outer ``cp._credential_lock`` (``threading.RLock``) is also held
    across this whole step; to avoid re-acquiring it on the worker
    thread inside ``asyncio.to_thread(cp.save_refreshed_vault_token)``
    (which would block forever — RLock re-entrance is per-thread),
    ``_refresh_vault_token`` is called with ``already_locked=True`` so
    the worker skips the internal ``with _credential_lock`` guard.
    """
    from .. import background as bg  # late import to avoid circular

    rt = cp.refresh_token_of(incoming)
    if not rt:
        return incoming

    async def _do_refresh():
        return await bg._refresh_vault_token(email, rt, already_locked=True)

    try:
        new = asyncio.run(_do_refresh())
    except bg._RefreshTerminal as term_err:
        reason = term_err.reason or "refresh_token invalid"
        raise SwapRefreshTerminalError(target_email=email, reason=reason)
    except (httpx.HTTPStatusError, httpx.RequestError, RuntimeError) as e:
        logger.warning(
            "Swap-time refresh for %s failed transiently (%s: %s); "
            "proceeding with stored tokens",
            email, type(e).__name__, e,
        )
        return incoming

    # Refresh succeeded — fold fresh tokens into the incoming blob.
    inner = dict(incoming.get("claudeAiOauth") or {})
    inner["accessToken"] = new["access_token"]
    if new.get("refresh_token"):
        inner["refreshToken"] = new["refresh_token"]
    if new.get("expires_at_ms"):
        inner["expiresAt"] = new["expires_at_ms"]
    fresh_incoming = dict(incoming)
    fresh_incoming["claudeAiOauth"] = inner
    return fresh_incoming


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
        # Strip expiresAt — CLI-authored claim that can be stale relative
        # to Anthropic's server state.  Next successful _refresh_vault_token
        # writes a fresh one.  See spec §9.11.
        stripped = {k: v for k, v in nested.items() if k != "expiresAt"}
        merged["claudeAiOauth"] = stripped
    else:
        token_fields = {
            k: fresh_standard[k]
            for k in ("accessToken", "refreshToken", "subscriptionType")
            if k in fresh_standard
        }
        if token_fields:
            merged["claudeAiOauth"] = token_fields
    # Also strip any root-level expiresAt that may have been copied from a
    # legacy-shape standard entry via ``dict(previous_vault)`` → merged.
    merged.pop("expiresAt", None)
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


# ── Shared per-email refresh lock ─────────────────────────────────────────
#
# Unified refresh lock, covers revalidate + poll-loop reactive refresh +
# swap-refresh.  Single-use refresh_tokens race across these code paths;
# one lock per email is the right granularity (different emails don't
# contend; same email serialises).
#
# Uses ``threading.Lock`` (NOT ``asyncio.Lock``) because swap step 0.5
# runs on a worker thread (``asyncio.to_thread``) inside a throwaway
# event loop spun up by ``asyncio.run``.  ``asyncio.Lock`` instances are
# bound to a specific event loop and do NOT serialise across threads or
# across different event loops — a primitive sufficient for the two
# async paths (reactive-refresh and Revalidate) but unable to block the
# swap thread when the two paths race.  Threading locks block uniformly
# across threads and loops.
#
# Critical: refresh_tokens are single-use; two concurrent calls with the
# same token would have the losing call get 400 invalid_grant and
# overwrite the winner's success with a terminal stale_reason.  Worse,
# Anthropic's reuse-detection then family-revokes every token minted
# from that chain — so a race here does not fail a single request, it
# permanently invalidates the account until the user re-logs in.
#
# Async callers MUST use ``with_refresh_lock_async(email)`` to acquire
# the lock without blocking the event loop (the acquire happens inside
# ``asyncio.to_thread``).  Sync callers (only ``swap_to_account`` today)
# use ``get_refresh_lock(email)`` with a plain ``with`` statement.
_refresh_locks: dict[str, threading.Lock] = {}
_refresh_locks_guard = threading.Lock()  # guards setdefault atomicity


def get_refresh_lock(email: str) -> threading.Lock:
    """Return the single ``threading.Lock`` for ``email``, creating it
    atomically.

    The ``_refresh_locks_guard`` wrap is defensive (``setdefault`` is
    already atomic under the CPython GIL, so same-email racers receive
    the same lock object); the explicit guard documents intent for
    readers.
    """
    with _refresh_locks_guard:
        return _refresh_locks.setdefault(email, threading.Lock())


@asynccontextmanager
async def with_refresh_lock_async(email: str):
    """Async context manager that acquires the per-email refresh lock
    without blocking the event loop.

    Cancellation-safe.  ``asyncio.to_thread(lock.acquire)`` is NOT
    cancellable (Python runs the underlying ``threading.Lock.acquire()``
    to completion on the executor thread regardless).  If the calling
    task is cancelled mid-wait, we must ensure the executor thread's
    eventual acquire is matched by a release — otherwise the lock is
    held by a ghost and the email deadlocks forever.

    Implementation: use ``loop.run_in_executor`` (returns an asyncio
    Future) wrapped in ``asyncio.shield`` so the underlying acquire is
    protected from cancellation.  On outer cancellation, install a
    done-callback that calls ``lock.release()`` as soon as the acquire
    future completes.  The lock is briefly held between executor-thread
    completion and callback-fire, but no caller can observe it — the
    shielded future finishes before any awaiter can re-enter.
    """
    lock = get_refresh_lock(email)
    loop = asyncio.get_running_loop()
    acquire_future = loop.run_in_executor(None, lock.acquire)
    try:
        await asyncio.shield(acquire_future)
    except asyncio.CancelledError:
        def _release_on_acquire(fut: asyncio.Future) -> None:
            try:
                if fut.result():
                    lock.release()
            except BaseException:
                pass
        acquire_future.add_done_callback(_release_on_acquire)
        raise
    try:
        yield
    finally:
        lock.release()


def forget_refresh_lock(email: str) -> None:
    """Drop the per-email refresh lock.  Called on account delete so the
    dict doesn't grow unbounded across the app lifetime."""
    with _refresh_locks_guard:
        _refresh_locks.pop(email, None)


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
    # NOTE: uses the threading-lock-backed async context manager so the
    # lock also blocks the swap path (worker thread) and not just other
    # async callers on the main event loop.
    async with with_refresh_lock_async(email):
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

        try:
            await bg._refresh_vault_token(email, refresh_token, already_locked=False)
        except bg._RefreshTerminal as terminal_err:
            account.stale_reason = terminal_err.reason
            await db.commit()
            return {
                "success": False,
                "stale_reason": account.stale_reason,
                "email": email,
                "active_refused": False,
            }
        except httpx.HTTPStatusError as refresh_err:
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

        # Success — helper already persisted tokens and cleared backoff dicts.
        account.stale_reason = None
        await db.commit()
        return {
            "success": True,
            "stale_reason": None,
            "email": email,
            "active_refused": False,
        }
