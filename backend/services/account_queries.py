"""
DB query helpers for Account records.

These are pure async SQLAlchemy queries with no side effects.
Extracted from account_service.py to reduce file size and clarify separation.
"""
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Account


async def get_account_by_id(account_id: int, db: AsyncSession) -> Account | None:
    result = await db.execute(select(Account).where(Account.id == account_id))
    return result.scalars().first()


async def get_account_by_email(email: str, db: AsyncSession) -> Account | None:
    result = await db.execute(select(Account).where(Account.email == email))
    return result.scalars().first()


async def get_enabled_accounts(db: AsyncSession) -> list[Account]:
    result = await db.execute(
        select(Account)
        .where(Account.enabled == True)
        .order_by(Account.priority.asc(), Account.id.asc())
    )
    return result.scalars().all()


async def get_all_accounts(db: AsyncSession) -> list[Account]:
    result = await db.execute(
        select(Account).order_by(Account.priority.asc(), Account.id.asc())
    )
    return result.scalars().all()


async def get_email_to_id_map(db: AsyncSession) -> dict[str, int]:
    """Return a mapping of email → account id for all accounts."""
    rows = await db.execute(select(Account.id, Account.email))
    return {row[1]: row[0] for row in rows.all()}


async def save_verified_account(
    email: str,
    config_dir: str,
    threshold_pct: float,
    db: AsyncSession,
) -> Account | None:
    """
    Persist a newly verified login account. Returns the saved Account,
    or None if an account with this email already exists (caller should
    treat as 'already_exists').
    """
    existing = await db.execute(select(Account).where(Account.email == email))
    if existing.scalars().first():
        return None

    max_result = await db.execute(select(func.max(Account.priority)))
    max_prio = max_result.scalar()

    account = Account(
        email=email,
        config_dir=config_dir,
        threshold_pct=threshold_pct,
        priority=(max_prio + 1) if max_prio is not None else 0,
    )
    db.add(account)
    await db.commit()
    return account
