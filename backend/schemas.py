from pydantic import BaseModel, Field, field_validator
from datetime import datetime
from typing import Optional
import re as re_module


# ── Accounts ──────────────────────────────────────────────────────────────────

class AccountOut(BaseModel):
    id: int
    email: str
    threshold_pct: float
    enabled: bool
    priority: int
    stale_reason: Optional[str] = None
    model_config = {"from_attributes": True}


class AccountUpdate(BaseModel):
    display_name: Optional[str] = None
    enabled: Optional[bool] = None
    priority: Optional[int] = None
    threshold_pct: Optional[float] = Field(None, ge=0.0, le=100.0)


class UsageData(BaseModel):
    five_hour_pct: Optional[float] = None
    five_hour_resets_at: Optional[int] = None
    seven_day_pct: Optional[float] = None
    seven_day_resets_at: Optional[int] = None
    error: Optional[str] = None
    rate_limited: Optional[bool] = None  # True when 429 but showing stale data
    # Token metadata (non-secret: expiry timestamp + subscription tier)
    token_expires_at: Optional[int] = None
    subscription_type: Optional[str] = None

    @classmethod
    def from_raw(cls, usage_raw: dict, token_info: dict) -> "UsageData | None":
        """Build a UsageData from the background poll cache entry + token
        metadata dict.  Returns None when there is literally nothing worth
        showing — a brand-new account that has never been polled and has no
        Keychain metadata.  Centralising this shape keeps routers free of the
        background module's internal cache structure."""
        base = dict(token_info)
        if "error" in usage_raw:
            base["error"] = usage_raw["error"]
            return cls(**base)
        fh = usage_raw.get("five_hour") or {}
        sd = usage_raw.get("seven_day") or {}
        if fh or sd or usage_raw.get("rate_limited"):
            base.update(
                five_hour_pct=fh.get("utilization"),
                five_hour_resets_at=fh.get("resets_at"),
                seven_day_pct=sd.get("utilization"),
                seven_day_resets_at=sd.get("resets_at"),
            )
            if usage_raw.get("rate_limited"):
                base["rate_limited"] = True
            return cls(**base)
        if base:
            return cls(**base)
        return None


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
    already_exists: bool = False


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

    @field_validator("pattern")
    @classmethod
    def validate_pattern(cls, v, info):
        pattern_type = (info.data or {}).get("pattern_type", "manual")
        if pattern_type == "regex":
            try:
                re_module.compile(v)
            except re_module.error as e:
                raise ValueError(f"Invalid regex pattern: {e}") from e
        return v


class TmuxMonitorUpdate(BaseModel):
    name: Optional[str] = None
    pattern_type: Optional[str] = None
    pattern: Optional[str] = None
    enabled: Optional[bool] = None

    @field_validator("pattern")
    @classmethod
    def validate_pattern(cls, v, info):
        if v is None:
            return v
        pattern_type = (info.data or {}).get("pattern_type", "manual")
        if pattern_type == "regex":
            try:
                re_module.compile(v)
            except re_module.error as e:
                raise ValueError(f"Invalid regex pattern: {e}") from e
        return v


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
