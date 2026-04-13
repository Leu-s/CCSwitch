from typing import Optional
from sqlalchemy import Integer, String, Boolean, DateTime, Float, Text
from sqlalchemy.orm import mapped_column, Mapped
from datetime import datetime, timezone
from .database import Base


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    display_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    # Isolated Claude config directory for this account (CLAUDE_CONFIG_DIR)
    config_dir: Mapped[str] = mapped_column(String(512), nullable=False)
    # Per-account rate-limit threshold (0–100 %)
    threshold_pct: Mapped[float] = mapped_column(Float, default=95.0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )


class TmuxMonitor(Base):
    __tablename__ = "tmux_monitors"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    pattern_type: Mapped[str] = mapped_column(String(16), default="manual")
    pattern: Mapped[str] = mapped_column(String(255), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class SwitchLog(Base):
    __tablename__ = "switch_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    from_account_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    to_account_id: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    triggered_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
