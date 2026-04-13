from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import Optional
from ..database import get_db
from ..models import Account, SwitchLog
from ..schemas import AccountCreate, AccountUpdate, AccountOut, AccountWithUsage, ScanResult, SwitchLogOut, UsageData
from ..services import keychain as kc
from ..services import switcher as sw
from ..background import usage_cache
from ..ws import ws_manager
from ..config import settings

router = APIRouter(prefix="/api/accounts", tags=["accounts"])

@router.get("", response_model=list[AccountWithUsage])
async def list_accounts(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Account).order_by(Account.priority.asc(), Account.id.asc()))
    accounts = result.scalars().all()
    active_email = kc.get_active_email(settings.claude_config_dir)
    out = []
    for acc in accounts:
        usage_raw = usage_cache.get(acc.email, {})
        if "error" in usage_raw:
            usage = UsageData(error=usage_raw["error"])
        elif usage_raw:
            fh = usage_raw.get("five_hour", {})
            sd = usage_raw.get("seven_day", {})
            usage = UsageData(
                five_hour_pct=fh.get("used_percentage"),
                five_hour_resets_at=fh.get("resets_at"),
                seven_day_pct=sd.get("used_percentage"),
                seven_day_resets_at=sd.get("resets_at"),
            )
        else:
            usage = None
        out.append(AccountWithUsage(
            **AccountOut.model_validate(acc).model_dump(),
            usage=usage,
            is_active=acc.email == active_email
        ))
    return out

@router.post("", response_model=AccountOut, status_code=201)
async def create_account(payload: AccountCreate, db: AsyncSession = Depends(get_db)):
    existing = await db.execute(select(Account).where(Account.email == payload.email))
    if existing.scalars().first():
        raise HTTPException(400, "Account with this email already exists")
    account = Account(**payload.model_dump())
    db.add(account)
    await db.commit()
    await db.refresh(account)
    return account

@router.patch("/{account_id}", response_model=AccountOut)
async def update_account(account_id: int, payload: AccountUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Account).where(Account.id == account_id))
    account = result.scalars().first()
    if not account:
        raise HTTPException(404, "Account not found")
    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(account, field, value)
    await db.commit()
    await db.refresh(account)
    return account

@router.delete("/{account_id}", status_code=204)
async def delete_account(account_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Account).where(Account.id == account_id))
    account = result.scalars().first()
    if not account:
        raise HTTPException(404, "Account not found")
    await db.delete(account)
    await db.commit()

@router.post("/{account_id}/switch", status_code=200)
async def manual_switch(account_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Account).where(Account.id == account_id))
    account = result.scalars().first()
    if not account:
        raise HTTPException(404, "Account not found")
    await sw.perform_switch(account, "manual", db, ws_manager)
    return {"ok": True}

@router.post("/scan", response_model=list[ScanResult])
async def scan_accounts(db: AsyncSession = Depends(get_db)):
    suffixes = kc.scan_keychain()
    existing_result = await db.execute(select(Account.keychain_suffix))
    existing_suffixes = {row[0] for row in existing_result.all()}
    results = []
    for suffix in suffixes:
        results.append(ScanResult(
            suffix=suffix,
            email=None,
            already_imported=suffix in existing_suffixes
        ))
    return results

@router.get("/log", response_model=list[SwitchLogOut])
async def switch_log(limit: int = 20, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(SwitchLog).order_by(SwitchLog.triggered_at.desc()).limit(limit)
    )
    return result.scalars().all()
