from pydantic import BaseModel
from datetime import datetime
from typing import Any, Optional


# ── Accounts ──────────────────────────────────────────────────────────────────

class AccountOut(BaseModel):
    id: int
    email: str
    display_name: Optional[str] = None
    threshold_pct: float
    enabled: bool
    priority: int
    created_at: datetime
    model_config = {"from_attributes": True}


class AccountUpdate(BaseModel):
    display_name: Optional[str] = None
    enabled: Optional[bool] = None
    priority: Optional[int] = None
    threshold_pct: Optional[float] = None


class UsageData(BaseModel):
    five_hour_pct: Optional[float] = None
    five_hour_resets_at: Optional[Any] = None
    seven_day_pct: Optional[float] = None
    seven_day_resets_at: Optional[Any] = None
    error: Optional[str] = None
    rate_limited: Optional[bool] = None  # True when 429 but showing stale data
    # Token metadata (non-secret: expiry timestamp + subscription tier)
    token_expires_at: Optional[int] = None
    subscription_type: Optional[str] = None


class AccountWithUsage(AccountOut):
    usage: Optional[UsageData] = None
    is_active: bool = False
    model_config = {"from_attributes": True}


# ── Login flow ─────────────────────────────────────────────────────────────────

class LoginSessionOut(BaseModel):
    session_id: str
    pane_target: str
    config_dir: str
    instructions: str


class LoginVerifyResult(BaseModel):
    success: bool
    email: Optional[str] = None
    error: Optional[str] = None


# ── Service toggle ─────────────────────────────────────────────────────────────

class ServiceStatus(BaseModel):
    enabled: bool
    active_email: Optional[str] = None
    default_account_id: Optional[int] = None


# ── Switch log ─────────────────────────────────────────────────────────────────

class SwitchLogOut(BaseModel):
    id: int
    from_account_id: Optional[int] = None
    to_account_id: int
    reason: str
    triggered_at: datetime
    model_config = {"from_attributes": True}


# ── Settings ───────────────────────────────────────────────────────────────────

class SettingOut(BaseModel):
    key: str
    value: str


class SettingUpdate(BaseModel):
    value: str


# ── Tmux ───────────────────────────────────────────────────────────────────────

class TmuxMonitorCreate(BaseModel):
    name: str
    pattern_type: str = "manual"
    pattern: str
    enabled: bool = True


class TmuxMonitorOut(BaseModel):
    id: int
    name: str
    pattern_type: str
    pattern: str
    enabled: bool
    model_config = {"from_attributes": True}


class TmuxPane(BaseModel):
    target: str
    command: str


class TmuxEvalResult(BaseModel):
    monitor_id: int
    target: str
    status: str
    explanation: str
