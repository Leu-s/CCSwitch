from sqlalchemy import Integer, String, Boolean, DateTime, ForeignKey, Enum, Text
from sqlalchemy.orm import mapped_column, Mapped, relationship
from datetime import datetime
from .database import Base
import enum

class SwitchReason(str, enum.Enum):
    manual = "manual"
    threshold = "threshold"
    api_error = "api_error"

class PatternType(str, enum.Enum):
    manual = "manual"
    regex = "regex"

class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    account_uuid: Mapped[str] = mapped_column(String(36), nullable=True)
    org_uuid: Mapped[str] = mapped_column(String(36), nullable=True)
    keychain_suffix: Mapped[str] = mapped_column(String(16), nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    switch_logs_to = relationship("SwitchLog", foreign_keys="SwitchLog.to_account_id", back_populates="to_account")

class TmuxMonitor(Base):
    __tablename__ = "tmux_monitors"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    pattern_type: Mapped[PatternType] = mapped_column(Enum(PatternType), default=PatternType.manual)
    pattern: Mapped[str] = mapped_column(String(255), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

class SwitchLog(Base):
    __tablename__ = "switch_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    from_account_id: Mapped[int] = mapped_column(Integer, ForeignKey("accounts.id"), nullable=True)
    to_account_id: Mapped[int] = mapped_column(Integer, ForeignKey("accounts.id"), nullable=False)
    reason: Mapped[SwitchReason] = mapped_column(Enum(SwitchReason), nullable=False)
    triggered_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    to_account = relationship("Account", foreign_keys=[to_account_id], back_populates="switch_logs_to")

class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
