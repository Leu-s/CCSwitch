from pydantic import BaseModel
from datetime import datetime
from typing import Optional

class AccountCreate(BaseModel):
    email: str
    keychain_suffix: str
    account_uuid: Optional[str] = None
    org_uuid: Optional[str] = None
    display_name: Optional[str] = None
    enabled: bool = True
    priority: int = 0

class AccountUpdate(BaseModel):
    display_name: Optional[str] = None
    enabled: Optional[bool] = None
    priority: Optional[int] = None

class AccountOut(BaseModel):
    id: int
    email: str
    account_uuid: Optional[str]
    org_uuid: Optional[str]
    keychain_suffix: str
    display_name: Optional[str]
    enabled: bool
    priority: int
    created_at: datetime
    model_config = {"from_attributes": True}

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

class SwitchLogOut(BaseModel):
    id: int
    from_account_id: Optional[int]
    to_account_id: int
    reason: str
    triggered_at: datetime
    model_config = {"from_attributes": True}

class SettingOut(BaseModel):
    key: str
    value: str

class SettingUpdate(BaseModel):
    value: str

class UsageData(BaseModel):
    five_hour_pct: Optional[float] = None
    five_hour_resets_at: Optional[int] = None
    seven_day_pct: Optional[float] = None
    seven_day_resets_at: Optional[int] = None
    error: Optional[str] = None

class AccountWithUsage(AccountOut):
    usage: Optional[UsageData] = None
    is_active: bool = False

class ScanResult(BaseModel):
    suffix: str
    email: Optional[str] = None
    already_imported: bool = False

class TmuxPane(BaseModel):
    target: str
    command: str

class TmuxEvalResult(BaseModel):
    monitor_id: int
    target: str
    status: str
    explanation: str
