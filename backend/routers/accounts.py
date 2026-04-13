import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from ..database import get_db
from ..models import Account, SwitchLog, Setting
from ..schemas import AccountUpdate, AccountOut, AccountWithUsage, SwitchLogOut, UsageData
from ..schemas import LoginSessionOut, LoginVerifyResult
from ..config import settings
from ..services import account_service as ac
from ..services import settings_service as ss
from ..services import switcher as sw
from ..background import usage_cache, token_info_cache
from ..ws import ws_manager

logger = logging.getLogger(__name__)


def _build_usage(usage_raw: dict, token_info: dict) -> UsageData | None:
    """Convert raw usage cache entry + token metadata into UsageData schema."""
    if "error" in usage_raw:
        return UsageData(error=usage_raw["error"], **token_info)
    fh = usage_raw.get("five_hour") or {}
    sd = usage_raw.get("seven_day") or {}
    if usage_raw.get("rate_limited"):
        return UsageData(
            five_hour_pct=fh.get("utilization"),
            five_hour_resets_at=fh.get("resets_at"),
            seven_day_pct=sd.get("utilization"),
            seven_day_resets_at=sd.get("resets_at"),
            rate_limited=True,
            **token_info,
        )
    if usage_raw:
        return UsageData(
            five_hour_pct=fh.get("utilization"),
            five_hour_resets_at=fh.get("resets_at"),
            seven_day_pct=sd.get("utilization"),
            seven_day_resets_at=sd.get("resets_at"),
            **token_info,
        )
    return UsageData(**token_info) if token_info else None


router = APIRouter(prefix="/api/accounts", tags=["accounts"])


# ── List accounts ──────────────────────────────────────────────────────────────

@router.get("", response_model=list[AccountWithUsage])
async def list_accounts(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Account).order_by(Account.priority.asc(), Account.id.asc())
    )
    accounts = result.scalars().all()
    active_email = ac.get_active_email()

    out = []
    for acc in accounts:
        usage_raw = usage_cache.get(acc.email, {})
        # Prefer the cached token metadata populated by the background poll
        # loop.  Only fall back to a direct Keychain/file lookup when the
        # cache has never been hydrated (e.g., a brand new account that was
        # just added and /api/accounts is called before the next poll cycle).
        token_info = token_info_cache.get(acc.email)
        if token_info is None:
            token_info = ac.get_token_info(acc.config_dir)
        usage = _build_usage(usage_raw, token_info)

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
        info = ac.start_login_session()
    except Exception as e:
        raise HTTPException(500, str(e))
    return LoginSessionOut(**info)


@router.post("/verify-login", response_model=LoginVerifyResult)
async def verify_login(session_id: str, db: AsyncSession = Depends(get_db)):
    """
    Verify that a login session completed and save the account to the database.
    """
    result = ac.verify_login_session(session_id)
    if not result["success"]:
        return LoginVerifyResult(success=False, error=result["error"])

    email = result["email"]
    config_dir = result["config_dir"]

    # Check for duplicate — the user re-authenticated an existing account.
    # Clean up the temporary login config dir so it does not linger, and tell
    # the UI via already_exists so it can show "already added" instead of "created".
    existing = await db.execute(select(Account).where(Account.email == email))
    if existing.scalars().first():
        ac.cleanup_login_session(session_id)
        return LoginVerifyResult(success=True, email=email, already_exists=True)

    # Assign next available priority
    max_result = await db.execute(select(func.max(Account.priority)))
    max_prio = max_result.scalar()

    account = Account(
        email=email,
        config_dir=config_dir,
        threshold_pct=settings.default_account_threshold_pct,
        priority=(max_prio + 1) if max_prio is not None else 0,
    )
    db.add(account)
    await db.commit()

    return LoginVerifyResult(success=True, email=email)


@router.delete("/cancel-login")
async def cancel_login(session_id: str):
    """Clean up a login session that was abandoned."""
    ac.cleanup_login_session(session_id)
    return {"ok": True}


# ── Switch log ─────────────────────────────────────────────────────────────────

@router.get("/log/count")
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
    result = await db.execute(select(Account).where(Account.id == account_id))
    account = result.scalars().first()
    if not account:
        raise HTTPException(404, "Account not found")
    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(account, field, value)
    await db.commit()
    await db.refresh(account)

    # If the currently active account was just disabled, switch away from it
    if payload.enabled is False and account.email == ac.get_active_email():
        service_enabled = await ss.get_bool("service_enabled", False, db)
        if service_enabled:
            next_acc = await sw.get_next_account(account.email, db)
            if next_acc:
                await sw.perform_switch(next_acc, "manual", db, ws_manager)

    return account


@router.delete("/{account_id}", status_code=204)
async def delete_account(account_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Account).where(Account.id == account_id))
    account = result.scalars().first()
    if not account:
        raise HTTPException(404, "Account not found")

    logger.info("Deleting account %s (id=%d)", account.email, account_id)

    # If this is the currently active account, switch to another one first.
    # If there is no replacement, clear the active pointer so new shells do
    # not export CLAUDE_CONFIG_DIR to a directory that no longer exists.
    if account.email == ac.get_active_email():
        next_result = await db.execute(
            select(Account)
            .where((Account.id != account_id) & (Account.enabled == True))
            .order_by(Account.priority.asc(), Account.id.asc())
        )
        next_acc = next_result.scalars().first()
        if next_acc:
            await sw.perform_switch(next_acc, "manual", db, ws_manager)
        else:
            ac.clear_active_config_dir()

    # Clear the default_account_id setting if this was the default
    setting_result = await db.execute(
        select(Setting).where(Setting.key == "default_account_id")
    )
    default_setting = setting_result.scalars().first()
    if default_setting and default_setting.value.isdigit() and int(default_setting.value) == account_id:
        default_setting.value = ""
        await db.flush()

    await db.delete(account)
    await db.commit()

    # Drop any cached usage/token entries so the deleted account does not
    # linger in memory forever.
    usage_cache.pop(account.email, None)
    token_info_cache.pop(account.email, None)

    # Notify all connected clients so their UI removes the card immediately.
    # The account is already deleted at this point, so a broadcast failure must
    # not surface as a 500 — the deletion itself succeeded.
    try:
        await ws_manager.broadcast({"type": "account_deleted", "id": account_id})
    except Exception:
        logger.warning("Failed to broadcast account_deleted for id=%s", account_id)


@router.post("/{account_id}/switch", status_code=200)
async def manual_switch(account_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Account).where(Account.id == account_id))
    account = result.scalars().first()
    if not account:
        raise HTTPException(404, "Account not found")
    await sw.perform_switch(account, "manual", db, ws_manager)
    return {"ok": True}
