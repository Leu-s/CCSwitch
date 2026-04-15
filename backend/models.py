from typing import Optional
from sqlalchemy import Integer, String, Boolean, DateTime, Float, Text, Index
from sqlalchemy.orm import mapped_column, Mapped
from datetime import datetime, timezone
from .database import Base


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    # Per-account rate-limit threshold (0–100 %)
    threshold_pct: Mapped[float] = mapped_column(Float, default=95.0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    # Non-null when the account's refresh token has been revoked / rejected
    # and the user must re-login through the UI.  Transient probe failures
    # (network, DNS, 5xx, timeouts, active-account 401) never populate this
    # column — they are cached in-memory only.
    stale_reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    __table_args__ = (
        Index("ix_accounts_priority_enabled", "priority", "enabled"),
        Index("ix_accounts_enabled", "enabled"),
    )


class SwitchLog(Base):
    __tablename__ = "switch_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    from_account_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    to_account_id: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    triggered_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    __table_args__ = (
        Index("ix_switch_log_triggered_at", "triggered_at"),
    )


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
