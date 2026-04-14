import asyncio
import logging
import os

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from ..database import get_db
from ..models import Account, SwitchLog
from ..schemas import AccountUpdate, AccountOut, AccountWithUsage, SwitchLogOut, UsageData
from ..schemas import LoginSessionOut, LoginVerifyResult, LogCount
from ..schemas import LoginSessionCaptureOut, LoginSessionSendRequest
from ..config import settings
from ..services import account_service as ac
from ..services import account_queries as aq
from ..services import credential_provider
from ..services import login_session_service as ls
from ..services import settings_service as ss
from ..services import switcher as sw
from ..services import tmux_service
from ..background import cache
from ..ws import ws_manager

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/accounts", tags=["accounts"])


# ── List accounts ──────────────────────────────────────────────────────────────

@router.get("", response_model=list[AccountWithUsage])
async def list_accounts(db: AsyncSession = Depends(get_db)):
    accounts = await aq.get_all_accounts(db)
    active_email = await ac.get_active_email_async()

    out = []
    for acc in accounts:
        usage_raw = await cache.get_usage_async(acc.email)
        # Prefer the cached token metadata populated by the background poll
        # loop.  Only fall back to a direct Keychain/file lookup when the
        # cache has never been hydrated (e.g., a brand new account that was
        # just added and /api/accounts is called before the next poll cycle).
        token_info = await cache.get_token_info_async(acc.email)
        if token_info is None:
            token_info = await asyncio.to_thread(ac.get_token_info, acc.config_dir)
        usage = UsageData.from_raw(usage_raw, token_info)
        is_active = acc.email == active_email
        # Waiting is only meaningful for the currently active account —
        # it means "CCSwitch is refusing to refresh this because Claude
        # Code CLI owns the refresh lifecycle right now".  If the account
        # is not active, CCSwitch owns the refresh and there is no such
        # wait.  Defense-in-depth gate: perform_switch already clears the
        # flag on both sides of a switch, but race windows around that
        # clear (e.g. a poll-cycle interleaved between the switch and
        # the next GET /api/accounts) can still leak a stale True.
        waiting = is_active and await cache.is_waiting_async(acc.email)

        out.append(AccountWithUsage(
            **AccountOut.model_validate(acc).model_dump(),
            usage=usage,
            is_active=is_active,
            waiting_for_cli=waiting,
        ))
    return out


# ── Login flow ─────────────────────────────────────────────────────────────────

@router.post("/start-login", response_model=LoginSessionOut)
async def start_login():
    """Create an isolated tmux window for authenticating a new Claude account."""
    try:
        info = await asyncio.to_thread(ls.start_login_session)
    except Exception as e:
        logger.exception("start_login_session failed")
        raise HTTPException(503, "Could not start login terminal — is tmux running?")
    return LoginSessionOut(**info)


@router.post("/verify-login", response_model=LoginVerifyResult)
async def verify_login(session_id: str, db: AsyncSession = Depends(get_db)):
    """
    Verify that a login session completed and save the account to the database.
    """
    result = await asyncio.to_thread(ls.verify_login_session, session_id)
    if not result["success"]:
        return LoginVerifyResult(success=False, error=result["error"])

    email = result["email"]
    config_dir = result["config_dir"]

    saved = await aq.save_verified_account(email, config_dir, settings.default_account_threshold_pct, db)

    # Seed ~/.ccswitch/active if no pointer exists yet so the shell snippet
    # works immediately without waiting for the first manual/auto switch.
    # Runs for both new and already-existing accounts — the pointer may have
    # been cleared (e.g. last account deleted then re-added).
    if not os.path.isfile(ac.active_dir_pointer_path()):
        try:
            await asyncio.to_thread(ac.write_active_config_dir, config_dir)
        except Exception:
            logger.warning("Failed to seed active pointer for %s", email)

    if saved is None:
        await asyncio.to_thread(ls.cleanup_login_session, session_id)
        return LoginVerifyResult(success=True, email=email, already_exists=True)

    return LoginVerifyResult(success=True, email=email)


@router.delete("/cancel-login")
async def cancel_login(session_id: str):
    """Clean up a login session that was abandoned."""
    try:
        await asyncio.to_thread(ls.cleanup_login_session, session_id)
    except Exception as e:
        logger.warning("cleanup_login_session failed for %s: %s", session_id, e)
    return {"ok": True}


@router.get(
    "/login-sessions/{session_id}/capture",
    response_model=LoginSessionCaptureOut,
)
async def capture_login_session(
    session_id: str,
    lines: int = Query(default=100, ge=10, le=500),
):
    """Capture recent terminal output from an active login session's tmux pane."""
    pane_target = ls.get_pane_target(session_id)
    if not pane_target:
        raise HTTPException(404, "Login session not found or expired")
    output = await tmux_service.capture_pane(pane_target, lines)
    return LoginSessionCaptureOut(output=output)


@router.post("/login-sessions/{session_id}/send")
async def send_to_login_session(
    session_id: str,
    payload: LoginSessionSendRequest,
):
    """Send keystrokes to an active login session's tmux pane (followed by Enter)."""
    pane_target = ls.get_pane_target(session_id)
    if not pane_target:
        raise HTTPException(404, "Login session not found or expired")
    await tmux_service.send_keys(pane_target, payload.text, press_enter=True)
    return {"ok": True}


# ── Switch log ─────────────────────────────────────────────────────────────────

@router.get("/log/count", response_model=LogCount)
async def switch_log_count(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(func.count()).select_from(SwitchLog))
    return {"total": result.scalar()}


@router.get("/log", response_model=list[SwitchLogOut])
async def switch_log(
    limit: int = Query(default=10, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """Return paginated switch log rows, enriched with from_email / to_email.

    Emails are resolved here instead of on the frontend so a switch that just
    fired cannot render as ``#42`` while ``state.accounts`` is still being
    reloaded in parallel over WebSocket.
    """
    result = await db.execute(
        select(SwitchLog).order_by(SwitchLog.triggered_at.desc()).limit(limit).offset(offset)
    )
    rows = result.scalars().all()

    # Single batched lookup for every account referenced in this page.
    referenced_ids: set[int] = set()
    for r in rows:
        referenced_ids.add(r.to_account_id)
        if r.from_account_id is not None:
            referenced_ids.add(r.from_account_id)

    email_by_id: dict[int, str] = {}
    if referenced_ids:
        account_rows = await db.execute(
            select(Account.id, Account.email).where(Account.id.in_(referenced_ids))
        )
        email_by_id = {aid: email for aid, email in account_rows.all()}

    return [
        SwitchLogOut(
            id=r.id,
            from_account_id=r.from_account_id,
            to_account_id=r.to_account_id,
            from_email=email_by_id.get(r.from_account_id) if r.from_account_id is not None else None,
            to_email=email_by_id.get(r.to_account_id),
            reason=r.reason,
            triggered_at=r.triggered_at,
        )
        for r in rows
    ]


# ── Per-account CRUD ───────────────────────────────────────────────────────────

@router.patch("/{account_id}", response_model=AccountOut)
async def update_account(
    account_id: int, payload: AccountUpdate, db: AsyncSession = Depends(get_db)
):
    account = await aq.get_account_by_id(account_id, db)
    if not account:
        raise HTTPException(404, "Account not found")
    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(account, field, value)
    await db.commit()
    await db.refresh(account)

    if payload.enabled is False:
        try:
            await sw.switch_if_active_disabled(account, db, ws_manager)
        except Exception as switch_err:
            logger.warning("Auto-switch after disable failed: %s", switch_err)

    return account


@router.delete("/{account_id}", status_code=204)
async def delete_account(account_id: int, db: AsyncSession = Depends(get_db)):
    account = await aq.get_account_by_id(account_id, db)
    if not account:
        raise HTTPException(404, "Account not found")

    logger.info("Deleting account %s (id=%d)", account.email, account_id)

    # If this is the currently active account, switch to another one first.
    # If there is no replacement, clear the active pointer so new shells do
    # not export CLAUDE_CONFIG_DIR to a directory that no longer exists.
    if account.email == await ac.get_active_email_async():
        next_acc = await sw.get_next_account(account.email, db)
        if next_acc:
            await sw.perform_switch(next_acc, "manual", db, ws_manager)
        else:
            await asyncio.to_thread(ac.clear_active_config_dir)
            # No replacement account — disable service to prevent the poll
            # loop from running with no valid active account.  The follow-up
            # account_deleted broadcast triggers a /api/service reload on the
            # client, so no separate service_disabled event is needed.
            if await ss.get_bool("service_enabled", False, db):
                await ss.set_setting("service_enabled", "false", db)

    # Clear the default_account_id setting if this was the default
    if await ss.get_int_or_none("default_account_id", db) == account_id:
        await ss.set_setting("default_account_id", "", db)

    await db.delete(account)
    await db.commit()

    # Drop any cached usage/token entries so the deleted account does not
    # linger in memory forever.
    await cache.invalidate(account.email)

    # Notify all connected clients so their UI removes the card immediately.
    # The account is already deleted at this point, so a broadcast failure must
    # not surface as a 500 — the deletion itself succeeded.
    try:
        await ws_manager.broadcast({"type": "account_deleted", "id": account_id})
    except Exception:
        logger.warning("Failed to broadcast account_deleted for id=%s", account_id)


@router.post("/{account_id}/switch", status_code=200)
async def manual_switch(account_id: int, db: AsyncSession = Depends(get_db)):
    account = await aq.get_account_by_id(account_id, db)
    if not account:
        raise HTTPException(404, "Account not found")
    current_email = await ac.get_active_email_async()
    if current_email and current_email == account.email:
        return {"ok": True, "already_active": True}
    await sw.perform_switch(account, "manual", db, ws_manager)
    return {"ok": True, "already_active": False}


async def _broadcast_single_account(account: Account) -> None:
    """Broadcast a single-account ``usage_updated`` so connected clients
    patch this one card without waiting for the next poll cycle.

    Reads the current cache state (usage + token_info + waiting flag) and
    the live ``account.stale_reason`` to build a usage_entry with the same
    shape the poll loop emits.  Used after ``force-refresh`` to give
    immediate visual feedback on both success and stale-on-failure paths.

    ``waiting_for_cli`` is gated by ``email == active_email`` — matching
    the defense-in-depth rule in both ``list_accounts`` and
    ``build_ws_snapshot``.  Without this gate a future caller that
    broadcasts for a non-active account could silently leak a stale True,
    contradicting the other two surfaces.
    """
    usage_raw = await cache.get_usage_async(account.email)
    if not isinstance(usage_raw, dict):
        usage_raw = {}
    token_info = await cache.get_token_info_async(account.email) or {}
    try:
        flat = ac.build_usage(usage_raw, token_info)
        flat_dict = flat.model_dump() if flat else {}
    except Exception as _bu_err:
        logger.warning("build_usage failed for %s: %s", account.email, _bu_err)
        flat_dict = {}
    active_email = await ac.get_active_email_async()
    is_active = account.email == active_email
    usage_entry = {
        "id": account.id,
        "email": account.email,
        "usage": flat_dict,
        "error": account.stale_reason,
        "stale_reason": account.stale_reason,
        "waiting_for_cli": is_active and await cache.is_waiting_async(account.email),
    }
    try:
        await ws_manager.broadcast({"type": "usage_updated", "accounts": [usage_entry]})
    except Exception as _bc_err:
        logger.warning("WS broadcast failed after force refresh: %s", _bc_err)


@router.post("/{account_id}/force-refresh", status_code=200)
async def force_refresh_account(account_id: int, db: AsyncSession = Depends(get_db)):
    """Force-refresh the OAuth token for an account.

    Intended for the "CCSwitch enabled, Claude Code CLI not running" case
    where the active account's access token has expired and the soft
    ``waiting_for_cli`` state would otherwise persist.  The button that
    invokes this endpoint is only surfaced while the card is in that
    soft state, so the caller is acknowledging no concurrent refresher.

    Error model:

    * 404 — account not found.
    * 409 — account already carries a ``stale_reason`` (user must re-login
      first); refresh token is missing; or upstream rejected the refresh
      token (400/401).  In the last case the account is marked stale and a
      single-account ``usage_updated`` broadcast fires so the card flips
      from "waiting" to stale.
    * 502 — any other upstream error (network, 5xx, malformed response)
      without mutating state; the user can retry.
    """
    account = await aq.get_account_by_id(account_id, db)
    if not account:
        raise HTTPException(404, "Account not found")

    # Already stale: re-login is the only path forward.  Returning 409 here
    # also protects against a user double-clicking the button after the
    # first click burned their refresh token and marked them stale.
    if account.stale_reason:
        raise HTTPException(409, f"Account already stale: {account.stale_reason}")

    try:
        new_token_info = await ac.force_refresh_config_dir(account.config_dir)
    except ValueError as e:
        raise HTTPException(409, str(e))
    except RuntimeError as e:
        logger.warning("Force refresh malformed response for %s: %s", account.email, e)
        raise HTTPException(502, str(e))
    except httpx.HTTPStatusError as e:
        status_code = e.response.status_code
        if status_code in (400, 401):
            # Match the stale_reason wording the poll loop uses for the same
            # status codes so the UI shows a consistent message regardless of
            # which path marked the account stale.
            reason_detail = "revoked" if status_code == 401 else "rejected (400)"
            reason = f"Refresh token {reason_detail} — re-login required"
            # Bookkeeping: a DB hiccup or cache-internal crash here must
            # not hide the 409 the caller needs to see — raising from a
            # stale-branch failure would downgrade to 500.
            #
            # Ordering: clear cache BEFORE db.commit so a failed commit
            # cannot leave a ghost waiting flag pointing at a clean DB row.
            # Each cache op runs in its OWN try so an exotic failure in
            # ``invalidate_token_info`` (e.g. asyncio cancellation mid-lock)
            # cannot skip ``clear_waiting`` — which would reintroduce the
            # very leak this reorder was designed to prevent.  Both ops are
            # idempotent, so running them always is safe.
            try:
                await cache.invalidate_token_info(account.email)
            except Exception as _ti_err:
                logger.debug(
                    "invalidate_token_info failed in 401 branch for %s: %s",
                    account.email, _ti_err,
                )
            try:
                await cache.clear_waiting(account.email)
            except Exception as _cw_err:
                logger.debug(
                    "clear_waiting failed in 401 branch for %s: %s",
                    account.email, _cw_err,
                )
            # DB mutation + commit + broadcast in its own try.  Per-request
            # SQLAlchemy session discards the dirty ``stale_reason`` attr
            # on rollback, so the next GET sees DB truth even if commit
            # raised.
            try:
                account.stale_reason = reason
                await db.commit()
                await _broadcast_single_account(account)
            except Exception as _bk_err:
                try:
                    await db.rollback()
                except Exception:
                    pass
                logger.warning(
                    "Post-stale bookkeeping failed for %s: %s",
                    account.email, _bk_err,
                )
            raise HTTPException(409, reason)
        logger.warning(
            "Force refresh upstream error for %s: HTTP %d",
            account.email, status_code,
        )
        raise HTTPException(502, f"Upstream HTTP {status_code}")
    except Exception as e:
        logger.exception("Force refresh failed for %s", account.email)
        raise HTTPException(502, f"Force refresh failed: {e}")

    # Success: hydrate token_info cache, clear the waiting flag, and
    # broadcast so clients see fresh expiry metadata immediately instead of
    # waiting for the next poll cycle.  A failure here must NOT downgrade
    # the 200 — the refresh already succeeded and is persisted in the
    # Keychain.
    try:
        await cache.set_token_info(account.email, new_token_info)
        await cache.clear_waiting(account.email)
        await _broadcast_single_account(account)
    except Exception as _post_err:
        logger.warning(
            "Post-force-refresh cache/broadcast failed for %s: %s",
            account.email, _post_err,
        )
    return {"ok": True}


# ── Re-login flow (existing account whose credentials have gone stale) ───────

@router.post("/{account_id}/relogin", response_model=LoginSessionOut)
async def relogin_account(account_id: int, db: AsyncSession = Depends(get_db)):
    """Open an interactive tmux login for an existing account.

    Reuses the account's existing isolated config directory so the slot's
    email, priority, threshold, and credential-target mappings all stay
    intact — only the OAuth material inside the isolated dir is replaced
    when the user finishes the interactive login.

    Returns ``409`` if another re-login session is already in progress for
    the same config dir (two tmux windows must not race the same Keychain).
    """
    account = await aq.get_account_by_id(account_id, db)
    if not account:
        raise HTTPException(404, "Account not found")
    try:
        info = await asyncio.to_thread(ls.start_relogin_session, account.config_dir)
    except ValueError as e:
        raise HTTPException(409, str(e))
    except Exception:
        logger.exception("start_relogin_session failed for %s", account.email)
        raise HTTPException(503, "Could not start re-login terminal — is tmux running?")
    return LoginSessionOut(**info)


@router.post("/{account_id}/relogin/verify", response_model=LoginVerifyResult)
async def verify_relogin(
    account_id: int,
    session_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Verify a re-login session and, on success, clear ``stale_reason``.

    If the user authenticated under a DIFFERENT email than the slot expects,
    the newly written credentials are wiped from the config dir (hashed
    Keychain entry + credential files + ``oauthAccount`` in ``.claude.json``)
    so the slot is returned to the same "no credentials" state a stale
    account already has.  The error message tells the user which email was
    detected so they can retry with the right one.

    If the revived account is the one currently active, the credential
    mirror pipeline is re-run (``perform_sync_to_targets``) so the legacy
    ``Claude Code-credentials`` Keychain entry, ``~/.claude/.credentials.json``,
    and any enabled ``.claude.json`` mirror targets all pick up the freshly
    written tokens — otherwise a fresh ``claude`` spawned without
    ``CLAUDE_CONFIG_DIR`` would still see the old (dead) credentials.
    """
    account = await aq.get_account_by_id(account_id, db)
    if not account:
        # Clean up the orphaned session so its tmux pane does not linger in
        # the tracking dict forever (the row was deleted mid-flow).
        await asyncio.to_thread(ls.cleanup_login_session, session_id)
        raise HTTPException(404, "Account not found")

    result = await asyncio.to_thread(ls.verify_login_session, session_id)
    if not result["success"]:
        return LoginVerifyResult(success=False, error=result["error"])

    new_email = result["email"]

    if new_email != account.email:
        # Wrong identity — wipe the new credentials so the slot is left in a
        # clean "no credentials" state and the user can retry without a
        # split-brain mix.  stale_reason is preserved (it was already set).
        await asyncio.to_thread(
            credential_provider.wipe_credentials_for_config_dir,
            account.config_dir,
        )
        return LoginVerifyResult(
            success=False,
            error=(
                f"Logged in as {new_email}, but this slot is for {account.email}. "
                "The new credentials were wiped — please re-login with the correct account."
            ),
        )

    # Email matches — mark the account healthy.
    account.stale_reason = None
    await db.commit()

    # Drop cached usage + token_info so the next poll cycle hydrates fresh
    # metadata (expiry, subscription tier) from the new credentials.
    await cache.invalidate(account.email)

    # If the revived account is still the active one, re-run the mirror
    # pipeline so legacy Keychain / ~/.claude/ / credential targets all
    # reflect the freshly written tokens.  Non-active accounts do not need
    # this — the next switch to them will mirror on its own.
    if account.email == await ac.get_active_email_async():
        try:
            await sw.perform_sync_to_targets(db)
        except Exception:
            logger.exception("Post-relogin mirror failed for %s", account.email)

    # The initiating frontend reloads /api/accounts on its own via the
    # app:reload-accounts custom event, so no WS broadcast is needed — other
    # tabs catch up on the next poll cycle's usage_updated anyway.
    return LoginVerifyResult(success=True, email=new_email)
