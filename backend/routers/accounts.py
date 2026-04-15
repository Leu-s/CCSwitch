import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from ..background import cache
from ..config import settings
from ..database import get_db
from ..models import Account, SwitchLog
from ..schemas import (
    AccountUpdate, AccountOut, AccountWithUsage, SwitchLogOut, UsageData,
    LoginSessionOut, LoginVerifyResult, LogCount,
    LoginSessionCaptureOut, LoginSessionSendRequest,
)
from ..services import account_queries as aq
from ..services import account_service as ac
from ..services import login_session_service as ls
from ..services import settings_service as ss
from ..services import switcher as sw
from ..services import tmux_service
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
        # loop.  Only fall back to a direct Keychain lookup when the cache
        # has never been hydrated — e.g., a brand-new account right after
        # verify-login, before the next poll cycle.
        token_info = await cache.get_token_info_async(acc.email)
        if token_info is None:
            token_info = await asyncio.to_thread(
                ac.get_token_info, acc.email, active_email
            )
        usage = UsageData.from_raw(usage_raw, token_info)
        is_active = acc.email == active_email

        out.append(AccountWithUsage(
            **AccountOut.model_validate(acc).model_dump(),
            usage=usage,
            is_active=is_active,
        ))
    return out


# ── Login flow (add account) ───────────────────────────────────────────────────


@router.post("/start-login", response_model=LoginSessionOut)
async def start_login():
    """Create a tmux window for authenticating a new Claude account."""
    try:
        info = await asyncio.to_thread(ls.start_login_session)
    except Exception:
        logger.exception("start_login_session failed")
        raise HTTPException(503, "Could not start login terminal — is tmux running?")
    return LoginSessionOut(**info)


@router.post("/verify-login", response_model=LoginVerifyResult)
async def verify_login(session_id: str, db: AsyncSession = Depends(get_db)):
    """Verify that a login session completed and save the account."""
    result = await asyncio.to_thread(ls.verify_login_session, session_id)
    if not result["success"]:
        return LoginVerifyResult(success=False, error=result["error"])

    email = result["email"]
    oauth_account = result["oauth_account"] or {}
    user_id = result["user_id"]
    oauth_tokens = result["oauth_tokens"] or {}

    # Promote scratch credentials into the vault.
    saved_vault = await asyncio.to_thread(
        ac.save_new_vault_account, email, oauth_tokens, oauth_account, user_id
    )
    if not saved_vault:
        await asyncio.to_thread(ls.cleanup_login_session, session_id)
        raise HTTPException(500, "Failed to write vault entry — Keychain error")

    # Clean up the scratch dir + its hashed Keychain entry.  Safe to run
    # before the DB write because the vault entry is already persistent.
    await asyncio.to_thread(ls.cleanup_login_session, session_id)

    saved = await aq.save_verified_account(
        email, settings.default_account_threshold_pct, db
    )

    if saved is None:
        # Duplicate — the account already exists in the DB.  The vault
        # entry was just overwritten with fresh credentials, which is the
        # user's likely intent.  Return already_exists=True so the UI
        # shows the "already enrolled" warning.
        return LoginVerifyResult(success=True, email=email, already_exists=True)

    # First-ever account on this machine?  Activate it immediately so the
    # user can start using Claude Code right away without a manual switch.
    if (await aq.get_all_accounts(db)).__len__() == 1:
        try:
            await asyncio.to_thread(ac.swap_to_account, email)
        except Exception as e:
            logger.warning(
                "First-account auto-activation failed for %s: %s", email, e
            )

    try:
        await ws_manager.broadcast({
            "type": "account_added",
            "id": saved.id,
            "email": email,
        })
    except Exception:
        logger.warning("Failed to broadcast account_added for %s", email)

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
    """Capture recent terminal output from an active login session."""
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
    """Send keystrokes to an active login session's tmux pane."""
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
    """Return paginated switch log rows, enriched with from_email / to_email."""
    result = await db.execute(
        select(SwitchLog).order_by(SwitchLog.triggered_at.desc()).limit(limit).offset(offset)
    )
    rows = result.scalars().all()

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

    # If this account is currently active, swap away first.  If there is
    # no replacement, just clear its credentials — the new-terminal shell
    # will show no active identity until the user picks one.
    if account.email == await ac.get_active_email_async():
        next_acc = await sw.get_next_account(account.email, db)
        if next_acc:
            await sw.perform_switch(next_acc, "manual", db, ws_manager)
        else:
            await asyncio.to_thread(ac.delete_account_everywhere, account.email)
            if await ss.get_bool("service_enabled", False, db):
                await ss.set_setting("service_enabled", "false", db)
    else:
        # Non-active account — just drop its vault entry.
        await asyncio.to_thread(ac.delete_account_everywhere, account.email)

    if await ss.get_int_or_none("default_account_id", db) == account_id:
        await ss.set_setting("default_account_id", "", db)

    await db.delete(account)
    await db.commit()

    await cache.invalidate(account.email)

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


# ── Re-login flow (existing account whose credentials have gone stale) ───────


@router.post("/{account_id}/relogin", response_model=LoginSessionOut)
async def relogin_account(account_id: int, db: AsyncSession = Depends(get_db)):
    """Open an interactive tmux login for an existing account.

    The re-login flow uses the same transient scratch directory as a
    brand-new login — there is no persistent per-account config directory
    in the vault-swap architecture.  On successful verification the
    freshly minted credentials are written to the existing vault entry
    for this account, and the scratch dir is cleaned up.

    Returns ``409`` if another re-login session is already in progress
    for the same email (we must not race two tmux windows writing to
    the same vault entry).
    """
    account = await aq.get_account_by_id(account_id, db)
    if not account:
        raise HTTPException(404, "Account not found")
    try:
        info = await asyncio.to_thread(ls.start_relogin_session, account.email)
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

    If the user authenticated under a DIFFERENT email than the slot
    expects, the scratch dir is wiped and the DB row is left as-is so
    the user can retry with the right identity.
    """
    account = await aq.get_account_by_id(account_id, db)
    if not account:
        await asyncio.to_thread(ls.cleanup_login_session, session_id)
        raise HTTPException(404, "Account not found")

    result = await asyncio.to_thread(ls.verify_login_session, session_id)
    if not result["success"]:
        return LoginVerifyResult(success=False, error=result["error"])

    new_email = result["email"]

    if new_email != account.email:
        # Wrong identity — wipe scratch dir and its hashed Keychain entry.
        await asyncio.to_thread(ls.cleanup_login_session, session_id)
        return LoginVerifyResult(
            success=False,
            error=(
                f"Logged in as {new_email}, but this slot is for {account.email}. "
                "The new credentials were wiped — please re-login with the correct account."
            ),
        )

    # Email matches — promote fresh tokens into the vault and clear stale.
    await asyncio.to_thread(
        ac.save_new_vault_account,
        account.email,
        result["oauth_tokens"],
        result["oauth_account"] or {},
        result["user_id"],
    )
    await asyncio.to_thread(ls.cleanup_login_session, session_id)

    account.stale_reason = None
    await db.commit()

    await cache.invalidate(account.email)

    # If the revived account is still the active one, re-run swap_to so
    # the standard Keychain entry and the identity file also reflect the
    # fresh credentials — otherwise new terminals would still read stale
    # tokens from the standard entry.
    if account.email == await ac.get_active_email_async():
        try:
            await asyncio.to_thread(ac.swap_to_account, account.email)
        except Exception:
            logger.exception("Post-relogin swap failed for %s", account.email)

    try:
        await ws_manager.broadcast({
            "type": "account_updated",
            "id": account.id,
            "email": account.email,
        })
    except Exception:
        logger.warning("Post-relogin broadcast failed for %s", account.email)

    return LoginVerifyResult(success=True, email=new_email)
