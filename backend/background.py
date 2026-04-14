import logging
import asyncio
import os
import time
import httpx

from sqlalchemy import select

from .database import AsyncSessionLocal
from .models import Account
from .services import account_service as ac
from .services.account_service import build_usage
from .services import anthropic_api
from .services import settings_service as ss
from .services import switcher as sw
from .ws import WebSocketManager
from .config import settings
from .cache import cache

logger = logging.getLogger(__name__)

# ── Per-account 429 backoff state ────────────────────────────────────────────
# Maps email → monotonic deadline (seconds); if time.monotonic() < deadline,
# skip the probe and return stale cached data instead.
_backoff_until: dict[str, float] = {}
# Maps email → consecutive 429 count for exponential doubling.
_backoff_count: dict[str, int] = {}

_BACKOFF_INITIAL = settings.rate_limit_backoff_initial
_BACKOFF_MAX = settings.rate_limit_backoff_max

# Active-ownership refresh model: CCSwitch is the SOLE refresher for every
# INACTIVE account, so the wider 20-minute pre-expiry window is race-free
# (no CLI is touching these credentials).  The active account is skipped
# entirely by the gate at ``_process_single_account`` — Claude Code CLI
# owns that account's refresh lifecycle.  Named constant so a future tuning
# knob does not drift out of the design doc.
_REFRESH_SKEW_MS_INACTIVE = 20 * 60 * 1000


class _RefreshTerminal(Exception):
    """Raised when a refresh attempt returns a terminal status (400/401),
    meaning the stored refresh_token is dead.  Skips the subsequent probe
    (which would just fail with 401 and overwrite the precise stale_reason
    with a generic one)."""


async def _process_single_account(
    account: Account,
    db,
    active_cfg_dir: "str | None",
) -> tuple[dict, "str | None"]:
    """Fetch token, optionally refresh, probe usage, and update caches for one account.

    ``active_cfg_dir`` is the canonicalized path of ``~/.ccswitch/active`` snapped
    once per poll cycle by ``poll_usage_and_switch``.  It partitions accounts into
    the one whose refresh lifecycle belongs to Claude Code CLI (``is_active``) and
    the N-1 others that CCSwitch alone consumes.  See the active-ownership refresh
    model section in ``CLAUDE.md`` and the design doc under ``docs/superpowers/``.

    Returns:
        (usage_entry, new_stale_reason) where usage_entry is a dict with keys
        ``id``, ``email``, ``usage``, ``error``, ``waiting_for_cli`` suitable for
        the WS broadcast, and new_stale_reason is the (possibly None) stale reason
        to persist.
    """
    new_stale_reason: str | None = None
    # Canonicalize via realpath so symlinked home dirs (macOS FileVault etc.)
    # do not defeat the ownership check by producing different-looking strings
    # for the same filesystem location.
    abs_cfg_dir = os.path.realpath(account.config_dir)
    is_active = active_cfg_dir is not None and abs_cfg_dir == active_cfg_dir
    try:
        token = await asyncio.to_thread(ac.get_access_token_from_config_dir, account.config_dir)
        if not token:
            new_stale_reason = "No access token in config dir — re-login required"
            raise ValueError(new_stale_reason)

        # Hydrate the token_info cache so GET /api/accounts can read
        # expiry + subscription metadata without spawning a Keychain
        # subprocess per account per request.
        token_info = await asyncio.to_thread(ac.get_token_info, account.config_dir)
        await cache.set_token_info(account.email, token_info)

        # Refresh the token if it is about to expire (within 20 minutes).
        # Active-ownership model: CCSwitch does NOT refresh the account whose
        # config dir matches ``~/.ccswitch/active`` — Claude Code CLI owns that
        # account's refresh lifecycle.  For inactive accounts CCSwitch is the
        # sole consumer, so the 20 min window is free (no race surface).
        # Stale accounts are also skipped — their refresh token is revoked,
        # so retrying would just produce a 401 log entry on every poll cycle.
        # Compute in integer milliseconds throughout to avoid float drift.
        if not account.stale_reason and not is_active:
            try:
                expires_at_ms = (token_info or {}).get("token_expires_at")
                now_ms = int(time.time() * 1000)
                if expires_at_ms and now_ms > expires_at_ms - _REFRESH_SKEW_MS_INACTIVE:
                    # Double-check ownership right before the refresh call.
                    # The ``active_cfg_dir`` snap was taken at the start of the
                    # poll cycle, but a manual switch during ``asyncio.gather``
                    # could have flipped ownership to THIS account.  If so,
                    # Claude Code will refresh it momentarily — do not race.
                    current_active = await asyncio.to_thread(
                        ac.get_active_config_dir_pointer
                    )
                    if (
                        current_active
                        and os.path.realpath(current_active) == abs_cfg_dir
                    ):
                        logger.info(
                            "Skip refresh for %s — became active mid-cycle",
                            account.email,
                        )
                        is_active = True  # re-sync for the rest of this pass
                        refresh_token = None
                    else:
                        refresh_token = await asyncio.to_thread(ac.get_refresh_token_from_config_dir, account.config_dir)
                    if refresh_token:
                        resp = await anthropic_api.refresh_access_token(refresh_token)
                        new_token = resp.get("access_token")
                        if new_token:
                            expires_in = resp.get("expires_in")
                            new_expires_at_ms = (
                                now_ms + int(expires_in) * 1000
                                if expires_in
                                else None
                            )
                            new_refresh = resp.get("refresh_token")
                            await asyncio.to_thread(
                                ac.save_refreshed_token, account.config_dir, new_token,
                                new_expires_at_ms, new_refresh,
                            )
                            token = new_token
                            logger.info("Refreshed access token for %s", account.email)
            except httpx.HTTPStatusError as refresh_http_err:
                status = refresh_http_err.response.status_code
                # Anthropic returns 400 when the refresh_token has been
                # rotated or invalidated (e.g. after a crash-loop race where
                # our stored copy fell behind the server's current record),
                # and 401 when the token is explicitly revoked.  Both are
                # terminal — the probe would just fail with 401 — so we
                # short-circuit by raising a marker exception that skips the
                # probe and preserves the precise stale_reason.
                if status in (400, 401):
                    reason_detail = "revoked" if status == 401 else "rejected (400)"
                    logger.error(
                        "Refresh token %s for %s — re-login required.",
                        reason_detail, account.email,
                    )
                    new_stale_reason = f"Refresh token {reason_detail} — re-login required"
                    # Mark token as permanently expired so we don't retry on every poll.
                    await asyncio.to_thread(ac.save_refreshed_token, account.config_dir, token, expires_at=1)
                    raise _RefreshTerminal()
                logger.warning("Token refresh HTTP error for %s: %s", account.email, refresh_http_err)
            except _RefreshTerminal:
                raise
            except Exception as refresh_err:
                logger.warning("Token refresh failed for %s: %s", account.email, refresh_err)

        # Probe usage; a 401 here also means the credentials are dead.
        # Skip the probe if the account is in a 429 backoff window — return
        # stale cached data instead of hammering the endpoint.
        backoff_deadline = _backoff_until.get(account.email, 0)
        if time.monotonic() < backoff_deadline:
            remaining = int(backoff_deadline - time.monotonic())
            logger.debug(
                "Skipping probe for %s — 429 backoff active (%ds remaining)",
                account.email, remaining,
            )
            cached = await cache.get_usage_async(account.email)
            token_info = await cache.get_token_info_async(account.email) or {}
            try:
                flat = build_usage(cached, token_info)
                flat_dict = flat.model_dump() if flat else {}
            except Exception as _bu_err:
                logger.warning("build_usage failed for %s: %s", account.email, _bu_err)
                flat_dict = {}
            # Backoff ≠ waiting_for_cli — we're not stuck on a stale token,
            # we're just politely not hammering a 429'd endpoint.  Clear the
            # flag so the card shows its regular rate-limited treatment.
            await cache.clear_waiting(account.email)
            return {
                "id": account.id,
                "email": account.email,
                "usage": flat_dict,
                "error": cached.get("error"),
                "waiting_for_cli": False,
            }, new_stale_reason

        try:
            usage = await anthropic_api.probe_usage(token)
        except httpx.HTTPStatusError as probe_err:
            status = probe_err.response.status_code
            if status == 401:
                # Retry once after re-reading the Keychain: Claude Code may
                # have just rotated the token while our cached copy went
                # stale.  A fresh token in the Keychain means the probe
                # should succeed on retry — and for the active account, a
                # persistent 401 becomes a soft "waiting for CLI" state,
                # never a persisted stale_reason.
                fresh_token = await asyncio.to_thread(
                    ac.get_access_token_from_config_dir, account.config_dir
                )
                retry_succeeded = False
                if fresh_token and fresh_token != token:
                    try:
                        usage = await anthropic_api.probe_usage(fresh_token)
                        # Flag success BEFORE any follow-up work so a
                        # CancelledError mid-bookkeeping can't reverse it.
                        retry_succeeded = True
                        await cache.set_token_info(
                            account.email,
                            await asyncio.to_thread(
                                ac.get_token_info, account.config_dir
                            ),
                        )
                    except Exception as _retry_err:
                        logger.debug(
                            "Probe retry after Keychain re-read failed for %s: %s",
                            account.email, _retry_err,
                        )
                if not retry_succeeded:
                    if is_active:
                        # Soft waiting state — do NOT overwrite cache, do NOT
                        # set stale_reason, do NOT broadcast an error entry.
                        # Claude Code will refresh on its next API call and
                        # the next poll cycle picks up the fresh token.
                        cached = await cache.get_usage_async(account.email)
                        cached_dict = cached if isinstance(cached, dict) else {}
                        cached_ti = await cache.get_token_info_async(account.email) or {}
                        try:
                            flat = build_usage(cached_dict, cached_ti)
                            flat_dict = flat.model_dump() if flat else {}
                        except Exception as _bu_err:
                            logger.warning("build_usage failed for %s: %s", account.email, _bu_err)
                            flat_dict = {}
                        # Record the soft waiting state so GET /api/accounts
                        # and subsequent cards see it without waiting for the
                        # next poll cycle.
                        await cache.set_waiting(account.email)
                        # Preserve any pre-existing ``stale_reason`` so a
                        # switch onto an already-stale account does NOT
                        # inadvertently clear the DB flag — the user still
                        # needs to re-login.
                        return {
                            "id": account.id,
                            "email": account.email,
                            "usage": flat_dict,
                            "error": cached_dict.get("error"),
                            "waiting_for_cli": True,
                        }, account.stale_reason
                    new_stale_reason = "Anthropic API returned 401 — re-login required"
                    raise
                # retry_succeeded → fall through to the success path below
            elif status == 429:
                # Compute exponential backoff: double the previous window, capped.
                count = _backoff_count.get(account.email, 0) + 1
                _backoff_count[account.email] = count
                backoff_seconds = min(_BACKOFF_INITIAL * (2 ** (count - 1)), _BACKOFF_MAX)
                _backoff_until[account.email] = time.monotonic() + backoff_seconds
                logger.warning(
                    "429 for %s (offense #%d) — backing off %ds",
                    account.email, count, backoff_seconds,
                )
                raise
            else:
                raise

        # Successful probe — clear any backoff + waiting state for this account.
        _backoff_until.pop(account.email, None)
        _backoff_count.pop(account.email, None)
        await cache.clear_waiting(account.email)

        await cache.set_usage(account.email, usage)
        token_info = await cache.get_token_info_async(account.email) or {}
        try:
            flat = build_usage(usage, token_info)
            flat_dict = flat.model_dump() if flat else {}
        except Exception as _bu_err:
            logger.warning("build_usage failed for %s: %s", account.email, _bu_err)
            flat_dict = {}
        usage_entry: dict = {
            "id": account.id,
            "email": account.email,
            "usage": flat_dict,
            "error": None,
            "waiting_for_cli": False,
        }
    except _RefreshTerminal:
        # Refresh returned a terminal 400/401 — skip probe, new_stale_reason
        # is already set to the precise reason.  Return a cache-backed usage
        # entry so the UI shows the error inline.  Stale overrides waiting.
        await cache.clear_waiting(account.email)
        err_str = new_stale_reason or "Refresh token invalid"
        new_entry, err_str = await cache.set_usage_error(account.email, err_str, False)
        token_info = await cache.get_token_info_async(account.email) or {}
        try:
            flat = build_usage(new_entry, token_info)
            flat_dict = flat.model_dump() if flat else {"error": err_str}
        except Exception as _bu_err:
            logger.warning("build_usage failed for %s: %s", account.email, _bu_err)
            flat_dict = {"error": err_str}
        usage_entry = {
            "id": account.id,
            "email": account.email,
            "usage": flat_dict,
            "error": err_str,
            "waiting_for_cli": False,
        }
    except Exception as e:
        err_str = str(e)
        if isinstance(e, httpx.HTTPStatusError):
            err_str = f"HTTP {e.response.status_code}"
            try:
                body = e.response.json()
                msg = (body.get("error") or {}).get("message") or body.get("message")
                if msg:
                    err_str = msg
            except Exception:
                pass
        logger.warning("Usage fetch failed for %s: %s", account.email, err_str)

        # Prefer the response status code over string parsing — it's the
        # only reliable signal that the API actually rate-limited us, and
        # the auto-switch loop relies on this flag firing on every 429.
        if isinstance(e, httpx.HTTPStatusError):
            is_rate_limited = e.response.status_code == 429
        else:
            is_rate_limited = "429" in str(e) or "rate_limit" in str(e).lower()
        # Any error branch supersedes the soft waiting state — the user
        # needs to see the real reason, not a stale "waiting" badge.
        await cache.clear_waiting(account.email)
        new_entry, err_str = await cache.set_usage_error(account.email, err_str, is_rate_limited)

        token_info = await cache.get_token_info_async(account.email) or {}
        try:
            flat = build_usage(new_entry, token_info)
            flat_dict = flat.model_dump() if flat else {"error": err_str}
        except Exception as _bu_err:
            logger.warning("build_usage failed for %s: %s", account.email, _bu_err)
            flat_dict = {"error": err_str}
        usage_entry = {
            "id": account.id,
            "email": account.email,
            "usage": flat_dict,
            "error": err_str if "error" in new_entry else None,
            "waiting_for_cli": False,
        }

    return usage_entry, new_stale_reason



async def poll_usage_and_switch(ws: WebSocketManager) -> None:
    async with AsyncSessionLocal() as db:
        # Polling always runs — the dashboard's usage bars must reflect live
        # state regardless of whether auto-switching is on.  The
        # ``service_enabled`` flag gates only the auto-switch decision, which
        # is checked inside ``maybe_auto_switch`` below.
        #
        # Snap ``active_cfg_dir`` once per cycle so every _process_single_account
        # call in this gather sees the same ownership partition.  Active-ownership
        # model: the matching account is refreshed by Claude Code CLI; CCSwitch
        # handles the N-1 others.
        active_cfg_dir_raw = await asyncio.to_thread(ac.get_active_config_dir_pointer)
        active_cfg_dir = (
            os.path.realpath(active_cfg_dir_raw) if active_cfg_dir_raw else None
        )

        accounts_result = await db.execute(select(Account))
        accounts = accounts_result.scalars().all()

        results = await asyncio.gather(
            *[_process_single_account(account, db, active_cfg_dir) for account in accounts],
            return_exceptions=True,
        )

        updated = []
        stale_changed = False
        for account, result in zip(accounts, results):
            # ``asyncio.gather(return_exceptions=True)`` captures both
            # Exception and BaseException subclasses (notably
            # ``asyncio.CancelledError`` during lifespan shutdown), so the
            # check must be against BaseException — not Exception — or a
            # cancelled per-account coroutine lands in the ``else`` branch
            # and crashes on the ``usage_entry, new_stale_reason = result``
            # unpack.
            if isinstance(result, BaseException):
                logger.exception(
                    "_process_single_account raised for %s: %s", account.email, result
                )
                # A crash in the per-account coroutine must not leave a stale
                # waiting flag behind in the cache.  Without this clear, a
                # subsequent GET /api/accounts would disagree with the WS
                # broadcast (which emits waiting_for_cli=False below) and the
                # card would flicker on reload.
                try:
                    await cache.clear_waiting(account.email)
                except Exception as _cw_err:
                    logger.debug(
                        "clear_waiting failed in exception branch for %s: %s",
                        account.email, _cw_err,
                    )
                usage_entry = {
                    "id": account.id,
                    "email": account.email,
                    "usage": {"error": str(result)},
                    "error": str(result),
                    "waiting_for_cli": False,
                }
                new_stale_reason = account.stale_reason  # leave unchanged
            else:
                usage_entry, new_stale_reason = result

            # Every poll-loop broadcast entry now carries the live
            # ``stale_reason`` so the frontend can flip a card in or out of
            # stale state without waiting for a full reload.  This closes
            # the pre-existing gap where a waiting→stale transition would
            # leave other open tabs showing the Force-refresh button for
            # up to one poll cycle after the DB was already updated.
            usage_entry["stale_reason"] = new_stale_reason
            updated.append(usage_entry)

            # Flip stale_reason on the DB row if it changed. Rate-limiting is NOT
            # staleness — we only set stale for 401-class auth failures.
            if new_stale_reason != account.stale_reason:
                account.stale_reason = new_stale_reason
                await cache.invalidate_token_info(account.email)
                stale_changed = True
                if new_stale_reason:
                    logger.warning("Marking %s stale: %s", account.email, new_stale_reason)
                else:
                    logger.info("Cleared stale flag for %s", account.email)

        if stale_changed:
            await db.commit()

        try:
            await ws.broadcast({"type": "usage_updated", "accounts": updated})
        except Exception as _bc_err:
            logger.warning("WS broadcast failed: %s", _bc_err)

        # ── Auto-switch logic ─────────────────────────────────────────────────
        await sw.maybe_auto_switch(db, ws)
