from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional


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
            if usage_raw.get("rate_limited"):
                base["rate_limited"] = True
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


# ── Settings (response) ──────────────────────────────────────────────────────

class ShellStatus(BaseModel):
    active_file_exists: bool
    shell_configured: bool


class SetupShellResult(BaseModel):
    results: dict[str, str]


# ── Switch log (count) ────────────────────────────────────────────────────────

class LogCount(BaseModel):
    total: int


# ── Credential targets (auto-discovered .claude.json mirror list) ─────────────

class CredentialTargetOut(BaseModel):
    path: str
    canonical: str
    label: str
    exists: bool
    current_email: Optional[str] = None
    enabled: bool


class CredentialTargetUpdate(BaseModel):
    canonical: str
    enabled: bool
