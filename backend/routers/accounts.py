import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from ..database import get_db
from ..models import Account, SwitchLog
from ..schemas import AccountUpdate, AccountOut, AccountWithUsage, SwitchLogOut, UsageData
from ..schemas import LoginSessionOut, LoginVerifyResult, OkResult, LogCount
from ..config import settings
from ..services import account_service as ac
from ..services import account_queries as aq
from ..services import login_session_service as ls
from ..services import settings_service as ss
from ..services import switcher as sw
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

        out.append(AccountWithUsage(
            **AccountOut.model_validate(acc).model_dump(),
            usage=usage,
            is_active=(acc.email == active_email),
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
    result = await db.execute(
        select(SwitchLog).order_by(SwitchLog.triggered_at.desc()).limit(limit).offset(offset)
    )
    return result.scalars().all()


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
            # loop from running with no valid active account.
            service_on = await ss.get_bool("service_enabled", False, db)
            if service_on:
                await ss.set_setting("service_enabled", "false", db)
                try:
                    await ws_manager.broadcast({"type": "service_disabled"})
                except Exception:
                    logger.warning("Failed to broadcast service_disabled after last-account delete")

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
    await sw.perform_switch(account, "manual", db, ws_manager)
    return {"ok": True}
