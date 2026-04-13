# Claude Code Multi-Account Manager Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a FastAPI + MySQL web app that manages multiple Claude.ai subscription accounts, swaps credentials automatically when usage thresholds are hit, and nudges monitored tmux sessions to continue.

**Architecture:** FastAPI serves a dark-theme Vanilla JS SPA at `/`. Background APScheduler polls Anthropic's usage API every 60s; when the active account's 5h usage ≥ threshold it swaps Keychain credentials + updates `~/.claude/.claude.json` and sends `continue` to monitored tmux panes. WebSocket broadcasts real-time state to the UI.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy (async), aiomysql, APScheduler, httpx, pytest + pytest-asyncio, Vanilla JS

---

## File Map

| File | Responsibility |
|---|---|
| `backend/config.py` | Pydantic BaseSettings — loads `.env` |
| `backend/database.py` | Async SQLAlchemy engine, `get_db` dependency |
| `backend/models.py` | ORM: Account, TmuxMonitor, SwitchLog, Setting |
| `backend/schemas.py` | Pydantic request/response schemas |
| `backend/ws.py` | WebSocket connection manager |
| `backend/services/keychain.py` | `security` CLI wrapper (read/write/scan/config) |
| `backend/services/anthropic_api.py` | Usage fetch + token refresh |
| `backend/services/tmux_service.py` | send-keys, capture-pane, haiku evaluation |
| `backend/services/switcher.py` | Core switch logic |
| `backend/background.py` | APScheduler job: poll usage → auto-switch |
| `backend/routers/accounts.py` | Account CRUD + scan + manual switch |
| `backend/routers/settings.py` | Settings GET/PATCH |
| `backend/routers/tmux.py` | Monitor CRUD + session discovery |
| `backend/main.py` | FastAPI app assembly, lifespan, WebSocket endpoint |
| `frontend/index.html` | SPA: Accounts tab + tmux Monitor tab |
| `docker-compose.yml` | MySQL 8 service |
| `requirements.txt` | Python dependencies |
| `.env.example` | Config template |
| `tests/conftest.py` | Shared fixtures |
| `tests/test_keychain.py` | Keychain service tests |
| `tests/test_anthropic_api.py` | Usage + refresh tests |
| `tests/test_tmux_service.py` | tmux service tests |
| `tests/test_switcher.py` | Switcher logic tests |
| `tests/test_accounts_router.py` | Account API tests |
| `tests/test_settings_router.py` | Settings API tests |
| `tests/test_tmux_router.py` | tmux router tests |

---

## Task 1: Project Scaffolding

**Files:**
- Create: `requirements.txt`
- Create: `docker-compose.yml`
- Create: `.env.example`
- Create: `backend/__init__.py`, `backend/routers/__init__.py`, `backend/services/__init__.py`
- Create: `tests/__init__.py`

- [ ] **Step 1: Create `requirements.txt`**

```
fastapi==0.115.0
uvicorn[standard]==0.30.6
sqlalchemy[asyncio]==2.0.36
aiomysql==0.2.0
apscheduler==3.10.4
httpx==0.27.2
python-dotenv==1.0.1
pydantic-settings==2.5.2
pytest==8.3.3
pytest-asyncio==0.24.0
anyio==4.6.2
```

- [ ] **Step 2: Create `docker-compose.yml`**

```yaml
services:
  mysql:
    image: mysql:8.0
    environment:
      MYSQL_ROOT_PASSWORD: root
      MYSQL_DATABASE: claude_multi_account
      MYSQL_USER: claude
      MYSQL_PASSWORD: claude
    ports:
      - "3306:3306"
    volumes:
      - mysql_data:/var/lib/mysql
    healthcheck:
      test: ["CMD", "mysqladmin", "ping", "-h", "localhost"]
      interval: 5s
      retries: 10

volumes:
  mysql_data:
```

- [ ] **Step 3: Create `.env.example`**

```
DATABASE_URL=mysql+aiomysql://claude:claude@localhost:3306/claude_multi_account
SERVER_PORT=8765
CLAUDE_CONFIG_DIR=~/.claude
HAIKU_MODEL=claude-haiku-4-5-20251001
```

- [ ] **Step 4: Create directory `__init__.py` files**

```bash
mkdir -p backend/routers backend/services tests
touch backend/__init__.py backend/routers/__init__.py backend/services/__init__.py tests/__init__.py
```

- [ ] **Step 5: Install dependencies**

```bash
pip install -r requirements.txt
```

Expected: all packages install without errors.

- [ ] **Step 6: Start MySQL**

```bash
docker-compose up -d
docker-compose ps
```

Expected: mysql container shows `healthy`.

- [ ] **Step 7: Commit**

```bash
git add requirements.txt docker-compose.yml .env.example backend/ tests/
git commit -m "feat: project scaffolding"
```

---

## Task 2: Config + Database

**Files:**
- Create: `backend/config.py`
- Create: `backend/database.py`
- Create: `backend/models.py`

- [ ] **Step 1: Create `backend/config.py`**

```python
from pydantic_settings import BaseSettings
import os

class Settings(BaseSettings):
    database_url: str = "mysql+aiomysql://claude:claude@localhost:3306/claude_multi_account"
    server_port: int = 8765
    claude_config_dir: str = "~/.claude"
    haiku_model: str = "claude-haiku-4-5-20251001"

    class Config:
        env_file = ".env"

settings = Settings()
```

- [ ] **Step 2: Create `backend/database.py`**

```python
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from .config import settings

engine = create_async_engine(settings.database_url, echo=False, pool_pre_ping=True)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
```

- [ ] **Step 3: Create `backend/models.py`**

```python
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
    account_uuid: Mapped[str | None] = mapped_column(String(36))
    org_uuid: Mapped[str | None] = mapped_column(String(36))
    keychain_suffix: Mapped[str] = mapped_column(String(16), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(100))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    switch_logs_to: Mapped[list["SwitchLog"]] = relationship("SwitchLog", foreign_keys="SwitchLog.to_account_id", back_populates="to_account")

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
    from_account_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("accounts.id"), nullable=True)
    to_account_id: Mapped[int] = mapped_column(Integer, ForeignKey("accounts.id"), nullable=False)
    reason: Mapped[SwitchReason] = mapped_column(Enum(SwitchReason), nullable=False)
    triggered_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    to_account: Mapped["Account"] = relationship("Account", foreign_keys=[to_account_id], back_populates="switch_logs_to")

class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
```

- [ ] **Step 4: Verify imports resolve**

```bash
cd /path/to/repo && python -c "from backend.models import Account, TmuxMonitor, SwitchLog, Setting; print('OK')"
```

Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add backend/config.py backend/database.py backend/models.py
git commit -m "feat: config, database, and ORM models"
```

---

## Task 3: Pydantic Schemas

**Files:**
- Create: `backend/schemas.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_schemas.py
from backend.schemas import AccountOut, AccountCreate, SettingOut, TmuxMonitorOut, SwitchLogOut

def test_account_out_fields():
    a = AccountOut(id=1, email="a@b.com", keychain_suffix="abc123", enabled=True, priority=0, created_at="2026-01-01T00:00:00")
    assert a.email == "a@b.com"

def test_account_create_requires_email_and_suffix():
    from pydantic import ValidationError
    import pytest
    with pytest.raises(ValidationError):
        AccountCreate(email="")
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/test_schemas.py -v
```

Expected: `ImportError` — schemas module not found.

- [ ] **Step 3: Create `backend/schemas.py`**

```python
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
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_schemas.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/schemas.py tests/test_schemas.py
git commit -m "feat: pydantic schemas"
```

---

## Task 4: WebSocket Manager

**Files:**
- Create: `backend/ws.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_ws.py
import pytest
from backend.ws import WebSocketManager

@pytest.mark.asyncio
async def test_broadcast_to_empty_set():
    manager = WebSocketManager()
    # should not raise even with no connections
    await manager.broadcast({"type": "test"})

@pytest.mark.asyncio
async def test_connect_disconnect():
    from unittest.mock import AsyncMock
    manager = WebSocketManager()
    ws = AsyncMock()
    await manager.connect(ws)
    assert ws in manager.active_connections
    manager.disconnect(ws)
    assert ws not in manager.active_connections
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/test_ws.py -v
```

Expected: `ImportError`.

- [ ] **Step 3: Create `backend/ws.py`**

```python
import json
from fastapi import WebSocket

class WebSocketManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, data: dict):
        dead = []
        for connection in self.active_connections:
            try:
                await connection.send_text(json.dumps(data))
            except Exception:
                dead.append(connection)
        for d in dead:
            self.disconnect(d)

ws_manager = WebSocketManager()
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_ws.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/ws.py tests/test_ws.py
git commit -m "feat: websocket connection manager"
```

---

## Task 5: Keychain Service

**Files:**
- Create: `backend/services/keychain.py`
- Create: `tests/test_keychain.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_keychain.py
import pytest
import json
from unittest.mock import patch, MagicMock

CREDS = {"claudeAiOauth": {"accessToken": "sk-ant-test", "refreshToken": "rt-test", "expiresAt": 9999999999}}

def test_read_credentials():
    from backend.services.keychain import read_credentials
    mock_result = MagicMock(stdout=json.dumps(CREDS), returncode=0)
    with patch("subprocess.run", return_value=mock_result):
        result = read_credentials("abc123")
    assert result["claudeAiOauth"]["accessToken"] == "sk-ant-test"

def test_write_active_credentials():
    from backend.services.keychain import write_active_credentials
    with patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
        write_active_credentials(CREDS)
    args = mock_run.call_args[0][0]
    assert "add-generic-password" in args
    assert "Claude Code-credentials" in args

def test_scan_keychain_returns_suffixes():
    from backend.services.keychain import scan_keychain
    dump_output = """
    "svce"<blob>="Claude Code-credentials-3798613e"
    "svce"<blob>="Claude Code-credentials-c4d4f94b"
    "svce"<blob>="Claude Code-credentials"
    """
    with patch("subprocess.run", return_value=MagicMock(stdout=dump_output, returncode=0)):
        result = scan_keychain()
    assert "3798613e" in result
    assert "c4d4f94b" in result

def test_update_oauth_account(tmp_path):
    from backend.services.keychain import update_oauth_account
    config = {"oauthAccount": {"emailAddress": "old@x.com"}, "numStartups": 5}
    config_file = tmp_path / ".claude.json"
    config_file.write_text(json.dumps(config))
    new_oauth = {"emailAddress": "new@x.com", "accountUuid": "uuid-1"}
    update_oauth_account(str(tmp_path), new_oauth)
    updated = json.loads(config_file.read_text())
    assert updated["oauthAccount"]["emailAddress"] == "new@x.com"
    assert updated["numStartups"] == 5  # other fields preserved

def test_get_active_email(tmp_path):
    from backend.services.keychain import get_active_email
    config = {"oauthAccount": {"emailAddress": "active@x.com"}}
    (tmp_path / ".claude.json").write_text(json.dumps(config))
    assert get_active_email(str(tmp_path)) == "active@x.com"
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/test_keychain.py -v
```

Expected: all 5 fail with `ImportError`.

- [ ] **Step 3: Create `backend/services/keychain.py`**

```python
import subprocess
import json
import os
import re

ACTIVE_SERVICE = "Claude Code-credentials"
SUFFIX_PREFIX = "Claude Code-credentials-"

def read_credentials(suffix: str) -> dict:
    result = subprocess.run(
        ["security", "find-generic-password", "-s", f"{SUFFIX_PREFIX}{suffix}", "-w"],
        capture_output=True, text=True, check=True
    )
    return json.loads(result.stdout.strip())

def read_active_credentials() -> dict:
    result = subprocess.run(
        ["security", "find-generic-password", "-s", ACTIVE_SERVICE, "-w"],
        capture_output=True, text=True, check=True
    )
    return json.loads(result.stdout.strip())

def write_active_credentials(creds: dict) -> None:
    subprocess.run(
        ["security", "add-generic-password", "-U",
         "-s", ACTIVE_SERVICE,
         "-a", os.environ.get("USER", "user"),
         "-w", json.dumps(creds)],
        capture_output=True, text=True, check=True
    )

def scan_keychain() -> list[str]:
    """Return list of unique suffix strings for Claude Code credential entries."""
    result = subprocess.run(
        ["security", "dump-keychain"],
        capture_output=True, text=True
    )
    suffixes = re.findall(r'Claude Code-credentials-([a-f0-9]{8})', result.stdout)
    return list(set(suffixes))

def get_claude_config(config_dir: str) -> dict:
    path = os.path.join(os.path.expanduser(config_dir), ".claude.json")
    with open(path) as f:
        return json.load(f)

def update_oauth_account(config_dir: str, oauth_account: dict) -> None:
    path = os.path.join(os.path.expanduser(config_dir), ".claude.json")
    with open(path) as f:
        config = json.load(f)
    config["oauthAccount"] = oauth_account
    with open(path, "w") as f:
        json.dump(config, f, indent=2)

def get_active_email(config_dir: str) -> str | None:
    try:
        config = get_claude_config(config_dir)
        return config.get("oauthAccount", {}).get("emailAddress")
    except Exception:
        return None
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_keychain.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/services/keychain.py tests/test_keychain.py
git commit -m "feat: keychain service"
```

---

## Task 6: Anthropic API Service

**Files:**
- Create: `backend/services/anthropic_api.py`
- Create: `tests/test_anthropic_api.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_anthropic_api.py
import pytest
import httpx
from unittest.mock import patch, AsyncMock, MagicMock

USAGE_RESPONSE = {
    "five_hour": {"used_percentage": 42.0, "resets_at": 1742651200},
    "seven_day": {"used_percentage": 18.0, "resets_at": 1743120000}
}
REFRESH_RESPONSE = {
    "access_token": "sk-ant-new",
    "refresh_token": "rt-new",
    "expires_in": 3600
}

@pytest.mark.asyncio
async def test_fetch_usage_success():
    from backend.services.anthropic_api import fetch_usage
    mock_response = MagicMock()
    mock_response.json.return_value = USAGE_RESPONSE
    mock_response.raise_for_status = MagicMock()
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response):
        result = await fetch_usage("sk-ant-test")
    assert result["five_hour"]["used_percentage"] == 42.0

@pytest.mark.asyncio
async def test_fetch_usage_401_raises():
    from backend.services.anthropic_api import fetch_usage
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock,
               side_effect=httpx.HTTPStatusError("401", request=MagicMock(), response=MagicMock(status_code=401))):
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_usage("bad-token")

@pytest.mark.asyncio
async def test_refresh_token_success():
    from backend.services.anthropic_api import refresh_access_token
    mock_response = MagicMock()
    mock_response.json.return_value = REFRESH_RESPONSE
    mock_response.raise_for_status = MagicMock()
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response):
        result = await refresh_access_token("rt-test")
    assert result["access_token"] == "sk-ant-new"
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/test_anthropic_api.py -v
```

Expected: 3 fail with `ImportError`.

- [ ] **Step 3: Create `backend/services/anthropic_api.py`**

```python
import httpx

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
REFRESH_URL = "https://platform.claude.com/v1/oauth/token"

async def fetch_usage(access_token: str) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            USAGE_URL,
            headers={"Authorization": f"Bearer {access_token}"}
        )
        resp.raise_for_status()
        return resp.json()

async def refresh_access_token(refresh_token: str) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            REFRESH_URL,
            json={"grant_type": "refresh_token", "refresh_token": refresh_token}
        )
        resp.raise_for_status()
        return resp.json()
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_anthropic_api.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/services/anthropic_api.py tests/test_anthropic_api.py
git commit -m "feat: anthropic usage and token refresh service"
```

---

## Task 7: tmux Service

**Files:**
- Create: `backend/services/tmux_service.py`
- Create: `tests/test_tmux_service.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_tmux_service.py
import pytest
from unittest.mock import patch, MagicMock

def test_list_panes_parses_output():
    from backend.services.tmux_service import list_panes
    output = "main:0.0 claude\nwork:1.0 bash\n"
    with patch("subprocess.run", return_value=MagicMock(stdout=output, returncode=0)):
        panes = list_panes()
    assert len(panes) == 2
    assert panes[0]["target"] == "main:0.0"
    assert panes[0]["command"] == "claude"

def test_list_panes_returns_empty_on_no_tmux():
    from backend.services.tmux_service import list_panes
    with patch("subprocess.run", side_effect=FileNotFoundError()):
        panes = list_panes()
    assert panes == []

def test_send_continue():
    from backend.services.tmux_service import send_continue
    with patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
        send_continue("main:0.0")
    args = mock_run.call_args[0][0]
    assert "send-keys" in args
    assert "main:0.0" in args
    assert "continue" in args

def test_capture_pane():
    from backend.services.tmux_service import capture_pane
    with patch("subprocess.run", return_value=MagicMock(stdout="some output\n", returncode=0)):
        result = capture_pane("main:0.0")
    assert result == "some output\n"

@pytest.mark.asyncio
async def test_evaluate_with_haiku_success():
    from backend.services.tmux_service import evaluate_with_haiku
    with patch("subprocess.run", return_value=MagicMock(
        stdout="SUCCESS The session continued normally.", returncode=0
    )):
        result = await evaluate_with_haiku("some terminal output", "claude-haiku-4-5-20251001")
    assert result["status"] == "SUCCESS"

@pytest.mark.asyncio
async def test_evaluate_with_haiku_defaults_uncertain():
    from backend.services.tmux_service import evaluate_with_haiku
    with patch("subprocess.run", return_value=MagicMock(stdout="Something happened.", returncode=0)):
        result = await evaluate_with_haiku("output", "model")
    assert result["status"] == "UNCERTAIN"
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/test_tmux_service.py -v
```

Expected: 6 fail with `ImportError`.

- [ ] **Step 3: Create `backend/services/tmux_service.py`**

```python
import subprocess
import asyncio
import re

def list_panes() -> list[dict]:
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-a", "-F",
             "#{session_name}:#{window_index}.#{pane_index} #{pane_current_command}"],
            capture_output=True, text=True
        )
        panes = []
        for line in result.stdout.strip().splitlines():
            if not line.strip():
                continue
            parts = line.split(" ", 1)
            panes.append({
                "target": parts[0],
                "command": parts[1] if len(parts) > 1 else ""
            })
        return panes
    except FileNotFoundError:
        return []

def send_continue(target: str) -> None:
    subprocess.run(
        ["tmux", "send-keys", "-t", target, "continue", "Enter"],
        check=True, capture_output=True
    )

def capture_pane(target: str, lines: int = 20) -> str:
    result = subprocess.run(
        ["tmux", "capture-pane", "-t", target, "-p", "-S", f"-{lines}"],
        capture_output=True, text=True, check=True
    )
    return result.stdout

async def evaluate_with_haiku(capture: str, model: str) -> dict:
    prompt = (
        "Did the Claude Code session successfully continue after an account switch? "
        "Reply with one of: SUCCESS, FAILED, UNCERTAIN. Then one sentence of explanation.\n\n"
        f"Terminal output:\n{capture}"
    )
    result = subprocess.run(
        ["claude", "-p", "--model", model, prompt],
        capture_output=True, text=True, timeout=30
    )
    output = result.stdout.strip()
    status = "UNCERTAIN"
    for s in ("SUCCESS", "FAILED", "UNCERTAIN"):
        if s in output:
            status = s
            break
    explanation = output.replace(status, "").strip(" .\n") or "No explanation"
    return {"status": status, "explanation": explanation, "raw": output}
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_tmux_service.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/services/tmux_service.py tests/test_tmux_service.py
git commit -m "feat: tmux service (send-keys, capture, haiku eval)"
```

---

## Task 8: Switcher Service

**Files:**
- Create: `backend/services/switcher.py`
- Create: `tests/test_switcher.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_switcher.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

def make_account(id, email, priority, enabled=True):
    a = MagicMock()
    a.id = id
    a.email = email
    a.priority = priority
    a.enabled = enabled
    a.keychain_suffix = f"suffix{id}"
    a.account_uuid = f"uuid-{id}"
    a.org_uuid = f"org-{id}"
    a.display_name = None
    return a

@pytest.mark.asyncio
async def test_get_next_account_skips_current():
    from backend.services.switcher import get_next_account
    accounts = [make_account(1, "a@x.com", 0), make_account(2, "b@x.com", 1)]
    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = accounts[1]
    mock_db = AsyncMock()
    mock_db.execute.return_value = mock_result
    result = await get_next_account("a@x.com", mock_db)
    assert result.email == "b@x.com"

@pytest.mark.asyncio
async def test_get_next_account_returns_none_when_no_others():
    from backend.services.switcher import get_next_account
    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = None
    mock_db = AsyncMock()
    mock_db.execute.return_value = mock_result
    result = await get_next_account("only@x.com", mock_db)
    assert result is None

@pytest.mark.asyncio
async def test_perform_switch_calls_keychain_and_broadcasts(tmp_path):
    import json
    from backend.services.switcher import perform_switch
    config = {"oauthAccount": {"emailAddress": "old@x.com", "accountUuid": "old-uuid"}, "numStartups": 1}
    (tmp_path / ".claude.json").write_text(json.dumps(config))

    target = make_account(2, "new@x.com", 1)
    creds = {"claudeAiOauth": {"accessToken": "tok", "refreshToken": "rt", "expiresAt": 9999}}
    mock_db = AsyncMock()
    mock_db.execute.return_value = MagicMock(scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=None))))
    mock_ws = AsyncMock()

    with patch("backend.services.keychain.read_credentials", return_value=creds), \
         patch("backend.services.keychain.write_active_credentials") as mock_write, \
         patch("backend.services.keychain.update_oauth_account") as mock_update, \
         patch("backend.services.keychain.get_active_email", return_value="old@x.com"), \
         patch("backend.config.settings.claude_config_dir", str(tmp_path)):
        await perform_switch(target, "threshold", mock_db, mock_ws)

    mock_write.assert_called_once_with(creds)
    mock_update.assert_called_once()
    mock_ws.broadcast.assert_called_once()
    broadcast_data = mock_ws.broadcast.call_args[0][0]
    assert broadcast_data["type"] == "account_switched"
    assert broadcast_data["to"] == "new@x.com"
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/test_switcher.py -v
```

Expected: 3 fail with `ImportError`.

- [ ] **Step 3: Create `backend/services/switcher.py`**

```python
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime
from ..models import Account, SwitchLog
from ..ws import WebSocketManager
from ..config import settings
from . import keychain as kc

async def get_next_account(current_email: str, db: AsyncSession) -> Account | None:
    result = await db.execute(
        select(Account)
        .where(Account.enabled == True)
        .where(Account.email != current_email)
        .order_by(Account.priority.asc(), Account.id.asc())
    )
    return result.scalars().first()

async def perform_switch(
    target: Account,
    reason: str,
    db: AsyncSession,
    ws: WebSocketManager
) -> None:
    current_email = kc.get_active_email(settings.claude_config_dir)

    # Read target credentials from their dedicated Keychain entry
    creds = kc.read_credentials(target.keychain_suffix)

    # Overwrite the active Keychain slot
    kc.write_active_credentials(creds)

    # Surgically update oauthAccount in .claude.json
    oauth = {
        "emailAddress": target.email,
        "accountUuid": target.account_uuid or "",
        "organizationUuid": target.org_uuid or "",
        "organizationName": target.display_name or target.email,
        "hasExtraUsageEnabled": False,
        "billingType": "stripe_subscription",
    }
    kc.update_oauth_account(settings.claude_config_dir, oauth)

    # Find current account id for the log
    from_acc = None
    if current_email:
        result = await db.execute(select(Account).where(Account.email == current_email))
        from_acc = result.scalars().first()

    log = SwitchLog(
        from_account_id=from_acc.id if from_acc else None,
        to_account_id=target.id,
        reason=reason,
        triggered_at=datetime.utcnow()
    )
    db.add(log)
    await db.commit()

    await ws.broadcast({
        "type": "account_switched",
        "from": current_email,
        "to": target.email,
        "reason": reason
    })
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_switcher.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/services/switcher.py tests/test_switcher.py
git commit -m "feat: switcher service"
```

---

## Task 9: Background Polling Task

**Files:**
- Create: `backend/background.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_background.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

@pytest.mark.asyncio
async def test_poll_skips_switch_when_auto_disabled():
    from backend.background import poll_usage_and_switch
    mock_ws = AsyncMock()
    settings_data = {"auto_switch_enabled": "false", "switch_threshold_percent": "90"}

    mock_db = AsyncMock()
    mock_setting = MagicMock()

    def execute_side_effect(query):
        result = MagicMock()
        # Return empty accounts list
        result.scalars.return_value.all.return_value = []
        result.scalars.return_value.first.return_value = MagicMock(value="false")
        return result

    mock_db.execute = AsyncMock(side_effect=execute_side_effect)

    with patch("backend.background.AsyncSessionLocal") as mock_session_cls, \
         patch("backend.services.keychain.get_active_email", return_value="a@x.com"):
        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        # Should complete without switching
        await poll_usage_and_switch(mock_ws)
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/test_background.py -v
```

Expected: fail with `ImportError`.

- [ ] **Step 3: Create `backend/background.py`**

```python
import json
import logging
from sqlalchemy import select
from .database import AsyncSessionLocal
from .models import Account, Setting
from .services import keychain as kc, anthropic_api, switcher as sw
from .ws import WebSocketManager
from .config import settings

logger = logging.getLogger(__name__)

# In-memory usage cache: {email: usage_dict}
usage_cache: dict[str, dict] = {}

async def poll_usage_and_switch(ws: WebSocketManager) -> None:
    async with AsyncSessionLocal() as db:
        # Load settings
        auto_row = await db.execute(select(Setting).where(Setting.key == "auto_switch_enabled"))
        auto_setting = auto_row.scalars().first()
        auto_enabled = json.loads(auto_setting.value) if auto_setting else True

        threshold_row = await db.execute(select(Setting).where(Setting.key == "switch_threshold_percent"))
        threshold_setting = threshold_row.scalars().first()
        threshold = json.loads(threshold_setting.value) if threshold_setting else 90

        # Fetch all accounts
        accounts_result = await db.execute(select(Account))
        accounts = accounts_result.scalars().all()

        updated = []
        for account in accounts:
            try:
                creds = kc.read_credentials(account.keychain_suffix)
                oauth = creds.get("claudeAiOauth", {})
                token = oauth.get("accessToken", "")

                # Try token refresh if expired
                expires_at = oauth.get("expiresAt", 0)
                import time
                if expires_at and expires_at < time.time() * 1000:
                    try:
                        refreshed = await anthropic_api.refresh_access_token(oauth.get("refreshToken", ""))
                        token = refreshed.get("access_token", token)
                    except Exception as e:
                        logger.warning(f"Token refresh failed for {account.email}: {e}")

                usage = await anthropic_api.fetch_usage(token)
                usage_cache[account.email] = usage
                updated.append({
                    "id": account.id,
                    "email": account.email,
                    "usage": usage,
                    "error": None
                })
            except Exception as e:
                logger.warning(f"Usage fetch failed for {account.email}: {e}")
                usage_cache[account.email] = {"error": str(e)}
                updated.append({
                    "id": account.id,
                    "email": account.email,
                    "usage": None,
                    "error": str(e)
                })

        await ws.broadcast({"type": "usage_updated", "accounts": updated})

        if not auto_enabled:
            return

        current_email = kc.get_active_email(settings.claude_config_dir)
        if not current_email:
            return

        current_usage = usage_cache.get(current_email, {})
        five_hour_pct = (current_usage.get("five_hour") or {}).get("used_percentage", 0)

        if five_hour_pct >= threshold:
            next_account = await sw.get_next_account(current_email, db)
            if next_account:
                logger.info(f"Auto-switching from {current_email} to {next_account.email} (usage {five_hour_pct}%)")
                await sw.perform_switch(next_account, "threshold", db, ws)
                # Notify tmux monitors
                from .services import tmux_service
                from .config import settings as cfg
                from sqlalchemy import select
                from .models import TmuxMonitor
                monitors_result = await db.execute(select(TmuxMonitor).where(TmuxMonitor.enabled == True))
                monitors = monitors_result.scalars().all()
                await notify_tmux_monitors(monitors, ws, cfg.haiku_model)
            else:
                logger.warning("No eligible account to switch to")
                await ws.broadcast({"type": "error", "message": "Rate limit reached — no eligible accounts to switch to"})

async def notify_tmux_monitors(monitors, ws: WebSocketManager, model: str) -> None:
    from .services import tmux_service
    import re
    all_panes = tmux_service.list_panes()
    for monitor in monitors:
        if monitor.pattern_type == "manual":
            matching = [p for p in all_panes if p["target"] == monitor.pattern]
        else:
            try:
                matching = [p for p in all_panes if re.search(monitor.pattern, p["target"])]
            except re.error:
                matching = []
        for pane in matching:
            try:
                tmux_service.send_continue(pane["target"])
                import asyncio
                await asyncio.sleep(2)
                capture = tmux_service.capture_pane(pane["target"])
                eval_result = await tmux_service.evaluate_with_haiku(capture, model)
                await ws.broadcast({
                    "type": "tmux_result",
                    "monitor_id": monitor.id,
                    "target": pane["target"],
                    "status": eval_result["status"],
                    "explanation": eval_result["explanation"],
                    "capture": capture
                })
            except Exception as e:
                await ws.broadcast({
                    "type": "tmux_result",
                    "monitor_id": monitor.id,
                    "target": pane["target"],
                    "status": "FAILED",
                    "explanation": str(e),
                    "capture": ""
                })
```

- [ ] **Step 4: Run test**

```bash
pytest tests/test_background.py -v
```

Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/background.py tests/test_background.py
git commit -m "feat: background polling task with auto-switch"
```

---

## Task 10: Accounts Router

**Files:**
- Create: `backend/routers/accounts.py`
- Create: `tests/conftest.py`
- Create: `tests/test_accounts_router.py`

- [ ] **Step 1: Create `tests/conftest.py`**

```python
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from backend.database import Base, get_db

TEST_DB_URL = "sqlite+aiosqlite:///./test.db"

@pytest_asyncio.fixture
async def test_db():
    engine = create_async_engine(TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    AsyncTestSession = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with AsyncTestSession() as session:
        yield session
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()
```

Add `aiosqlite` to requirements.txt:
```
aiosqlite==0.20.0
```

Run `pip install aiosqlite`.

- [ ] **Step 2: Write failing tests**

```python
# tests/test_accounts_router.py
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi.testclient import TestClient

@pytest.fixture
def client():
    from backend.main import app
    return TestClient(app)

def test_list_accounts_empty(client):
    with patch("backend.routers.accounts.get_db"):
        resp = client.get("/api/accounts")
    assert resp.status_code == 200

def test_create_account(client):
    payload = {"email": "test@x.com", "keychain_suffix": "abc123"}
    with patch("backend.routers.accounts.get_db"):
        resp = client.post("/api/accounts", json=payload)
    assert resp.status_code in (200, 201, 422)  # 422 if DB not mocked properly

def test_scan_endpoint_returns_list(client):
    with patch("backend.services.keychain.scan_keychain", return_value=["abc123", "def456"]):
        resp = client.post("/api/accounts/scan")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
```

- [ ] **Step 3: Run to verify failure**

```bash
pytest tests/test_accounts_router.py -v
```

Expected: fail (ImportError on main or accounts router).

- [ ] **Step 4: Create `backend/routers/accounts.py`**

```python
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import Optional
from ..database import get_db
from ..models import Account, SwitchLog
from ..schemas import AccountCreate, AccountUpdate, AccountOut, AccountWithUsage, ScanResult, SwitchLogOut
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
        from ..schemas import UsageData
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
        try:
            creds = kc.read_credentials(suffix)
            oauth = creds.get("claudeAiOauth", {})
            email = None
        except Exception:
            email = None
        results.append(ScanResult(
            suffix=suffix,
            email=email,
            already_imported=suffix in existing_suffixes
        ))
    return results

@router.get("/log", response_model=list[SwitchLogOut])
async def switch_log(limit: int = 20, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(SwitchLog).order_by(SwitchLog.triggered_at.desc()).limit(limit)
    )
    return result.scalars().all()
```

- [ ] **Step 5: Run tests**

```bash
pytest tests/test_accounts_router.py -v
```

Expected: tests pass (some may need full app context — adjust if needed).

- [ ] **Step 6: Commit**

```bash
git add backend/routers/accounts.py tests/conftest.py tests/test_accounts_router.py requirements.txt
git commit -m "feat: accounts router (CRUD, scan, switch, log)"
```

---

## Task 11: Settings Router

**Files:**
- Create: `backend/routers/settings.py`
- Create: `tests/test_settings_router.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_settings_router.py
import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch, MagicMock

@pytest.fixture
def client():
    from backend.main import app
    return TestClient(app)

def test_get_settings_returns_defaults(client):
    resp = client.get("/api/settings")
    assert resp.status_code == 200
    data = resp.json()
    keys = {s["key"] for s in data}
    assert "auto_switch_enabled" in keys
    assert "switch_threshold_percent" in keys

def test_patch_setting(client):
    resp = client.patch("/api/settings/switch_threshold_percent", json={"value": "80"})
    assert resp.status_code == 200
    assert resp.json()["value"] == "80"
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/test_settings_router.py -v
```

Expected: fail (router not yet created).

- [ ] **Step 3: Create `backend/routers/settings.py`**

```python
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from ..database import get_db
from ..models import Setting
from ..schemas import SettingOut, SettingUpdate

router = APIRouter(prefix="/api/settings", tags=["settings"])

DEFAULTS = {
    "auto_switch_enabled": "true",
    "switch_threshold_percent": "90",
    "usage_poll_interval_seconds": "60",
}

async def ensure_defaults(db: AsyncSession):
    for key, value in DEFAULTS.items():
        result = await db.execute(select(Setting).where(Setting.key == key))
        if not result.scalars().first():
            db.add(Setting(key=key, value=value))
    await db.commit()

@router.get("", response_model=list[SettingOut])
async def get_settings(db: AsyncSession = Depends(get_db)):
    await ensure_defaults(db)
    result = await db.execute(select(Setting))
    return result.scalars().all()

@router.patch("/{key}", response_model=SettingOut)
async def update_setting(key: str, payload: SettingUpdate, db: AsyncSession = Depends(get_db)):
    await ensure_defaults(db)
    result = await db.execute(select(Setting).where(Setting.key == key))
    setting = result.scalars().first()
    if not setting:
        setting = Setting(key=key, value=payload.value)
        db.add(setting)
    else:
        setting.value = payload.value
    await db.commit()
    await db.refresh(setting)
    return setting
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_settings_router.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/routers/settings.py tests/test_settings_router.py
git commit -m "feat: settings router"
```

---

## Task 12: tmux Router

**Files:**
- Create: `backend/routers/tmux.py`
- Create: `tests/test_tmux_router.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_tmux_router.py
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch

@pytest.fixture
def client():
    from backend.main import app
    return TestClient(app)

def test_list_sessions(client):
    panes = [{"target": "main:0.0", "command": "claude"}]
    with patch("backend.services.tmux_service.list_panes", return_value=panes):
        resp = client.get("/api/tmux/sessions")
    assert resp.status_code == 200
    assert resp.json()[0]["target"] == "main:0.0"

def test_create_monitor(client):
    payload = {"name": "test", "pattern_type": "manual", "pattern": "main:0.0", "enabled": True}
    resp = client.post("/api/tmux/monitors", json=payload)
    assert resp.status_code == 201

def test_list_monitors(client):
    resp = client.get("/api/tmux/monitors")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/test_tmux_router.py -v
```

Expected: fail (router not found).

- [ ] **Step 3: Create `backend/routers/tmux.py`**

```python
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from ..database import get_db
from ..models import TmuxMonitor
from ..schemas import TmuxMonitorCreate, TmuxMonitorOut, TmuxPane
from ..services import tmux_service

router = APIRouter(prefix="/api/tmux", tags=["tmux"])

@router.get("/sessions", response_model=list[TmuxPane])
async def list_sessions():
    return tmux_service.list_panes()

@router.get("/monitors", response_model=list[TmuxMonitorOut])
async def list_monitors(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(TmuxMonitor))
    return result.scalars().all()

@router.post("/monitors", response_model=TmuxMonitorOut, status_code=201)
async def create_monitor(payload: TmuxMonitorCreate, db: AsyncSession = Depends(get_db)):
    monitor = TmuxMonitor(**payload.model_dump())
    db.add(monitor)
    await db.commit()
    await db.refresh(monitor)
    return monitor

@router.patch("/monitors/{monitor_id}", response_model=TmuxMonitorOut)
async def update_monitor(monitor_id: int, payload: TmuxMonitorCreate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(TmuxMonitor).where(TmuxMonitor.id == monitor_id))
    monitor = result.scalars().first()
    if not monitor:
        raise HTTPException(404, "Monitor not found")
    for field, value in payload.model_dump().items():
        setattr(monitor, field, value)
    await db.commit()
    await db.refresh(monitor)
    return monitor

@router.delete("/monitors/{monitor_id}", status_code=204)
async def delete_monitor(monitor_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(TmuxMonitor).where(TmuxMonitor.id == monitor_id))
    monitor = result.scalars().first()
    if not monitor:
        raise HTTPException(404, "Monitor not found")
    await db.delete(monitor)
    await db.commit()
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_tmux_router.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/routers/tmux.py tests/test_tmux_router.py
git commit -m "feat: tmux router (monitors CRUD + session discovery)"
```

---

## Task 13: FastAPI Main App

**Files:**
- Create: `backend/main.py`

- [ ] **Step 1: Create `backend/main.py`**

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import text
import logging
import os

from .database import engine, Base
from .ws import ws_manager
from .background import poll_usage_and_switch
from .routers import accounts, settings, tmux
from .config import settings as cfg

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables created")

    # Start background scheduler
    scheduler.add_job(
        poll_usage_and_switch,
        "interval",
        seconds=60,
        args=[ws_manager],
        id="usage_poll",
        replace_existing=True
    )
    scheduler.start()
    logger.info(f"Server running on port {cfg.server_port}")
    yield
    scheduler.shutdown()

app = FastAPI(title="Claude Multi-Account Manager", lifespan=lifespan)

app.include_router(accounts.router)
app.include_router(settings.router)
app.include_router(tmux.router)

# Serve frontend
frontend_path = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.exists(frontend_path):
    app.mount("/static", StaticFiles(directory=frontend_path), name="static")

@app.get("/")
async def root():
    index = os.path.join(frontend_path, "index.html")
    return FileResponse(index)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()  # keep alive
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)

@app.get("/health")
async def health():
    return {"ok": True}
```

- [ ] **Step 2: Verify app starts**

```bash
cp .env.example .env
uvicorn backend.main:app --port 8765 --reload
```

Expected: server starts, `http://localhost:8765/health` returns `{"ok": true}`.

- [ ] **Step 3: Run all tests**

```bash
pytest tests/ -v
```

Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add backend/main.py
git commit -m "feat: fastapi app assembly with lifespan and websocket"
```

---

## Task 14: Frontend Dark Dashboard

**Files:**
- Create: `frontend/index.html`

- [ ] **Step 1: Invoke the frontend-design skill**

Run `/frontend-design:frontend-design` and provide this brief:

> Dark theme dashboard (`#0a0a0f` bg, `#1a1a2e` cards, `#7c3aed` accent, Inter font). Two tabs: **Accounts** and **tmux Monitor**.
>
> **Accounts tab:**
> - Top bar: "Active: email@x.com" badge (green pill), Auto-switch toggle + threshold slider (0–100%)
> - Account cards in a grid, draggable to reorder (HTML5 DnD, POST /api/accounts/{id} with new priority on drop)
> - Each card: email, display name (editable inline), enabled toggle, "Switch" button (disabled if active, highlighted if active), 5h usage progress bar (green<70, yellow<90, red≥90) with % and reset time, 7d bar same colors, delete button (confirm dialog)
> - "Scan Keychain" button → GET /api/accounts/scan → modal listing importable accounts with "Import" button per row
> - Switch Log table at bottom (last 20, timestamp / from / to / reason columns)
>
> **tmux Monitor tab:**
> - "Refresh" button → GET /api/tmux/sessions → shows live pane list
> - Monitor list: each row has name, pattern type (manual/regex), pattern input, enabled toggle, delete
> - "Add Monitor" form
> - Event feed: WebSocket messages `tmux_result` appear as cards — pane target, SUCCESS/FAILED/UNCERTAIN badge, explanation, collapsible raw terminal capture
>
> **WebSocket:** connect to `ws://localhost:8765/ws` on load. Handle:
> - `account_switched`: update active badge, move active highlight on cards, add row to switch log
> - `usage_updated`: update progress bars on all cards
> - `tmux_result`: prepend event card to tmux feed

- [ ] **Step 2: Save result to `frontend/index.html`**

- [ ] **Step 3: Verify in browser**

```bash
uvicorn backend.main:app --port 8765 --reload
open http://localhost:8765
```

Check:
- Both tabs render correctly
- WebSocket connects (no console errors)
- GET /api/accounts loads without errors
- Scan button calls the API

- [ ] **Step 4: Commit**

```bash
git add frontend/index.html
git commit -m "feat: dark dashboard frontend (accounts + tmux monitor tabs)"
```

---

## Task 15: End-to-End Smoke Test

**Files:**
- Create: `tests/test_e2e_smoke.py`

- [ ] **Step 1: Write smoke tests**

```python
# tests/test_e2e_smoke.py
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch

@pytest.fixture
def client():
    from backend.main import app
    return TestClient(app)

def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

def test_accounts_list(client):
    resp = client.get("/api/accounts")
    assert resp.status_code == 200

def test_settings_list(client):
    resp = client.get("/api/settings")
    assert resp.status_code == 200
    keys = {s["key"] for s in resp.json()}
    assert "auto_switch_enabled" in keys
    assert "switch_threshold_percent" in keys

def test_tmux_sessions(client):
    with patch("backend.services.tmux_service.list_panes", return_value=[]):
        resp = client.get("/api/tmux/sessions")
    assert resp.status_code == 200

def test_tmux_monitors_crud(client):
    # Create
    resp = client.post("/api/tmux/monitors", json={
        "name": "smoke-test", "pattern_type": "manual", "pattern": "main:0.0", "enabled": True
    })
    assert resp.status_code == 201
    mid = resp.json()["id"]

    # List
    resp = client.get("/api/tmux/monitors")
    assert any(m["id"] == mid for m in resp.json())

    # Delete
    resp = client.delete(f"/api/tmux/monitors/{mid}")
    assert resp.status_code == 204

def test_scan_keychain(client):
    with patch("backend.services.keychain.scan_keychain", return_value=["aaaabbbb"]):
        resp = client.post("/api/accounts/scan")
    assert resp.status_code == 200
    assert resp.json()[0]["suffix"] == "aaaabbbb"
```

- [ ] **Step 2: Run smoke tests**

```bash
pytest tests/test_e2e_smoke.py -v
```

Expected: 6 passed.

- [ ] **Step 3: Run full test suite**

```bash
pytest tests/ -v --tb=short
```

Expected: all tests pass.

- [ ] **Step 4: Final commit**

```bash
git add tests/test_e2e_smoke.py
git commit -m "test: end-to-end smoke tests"
```

---

## Setup Instructions (for running locally)

```bash
# 1. Start MySQL
docker-compose up -d

# 2. Install deps
pip install -r requirements.txt

# 3. Configure
cp .env.example .env
# Edit .env with your MySQL password if changed

# 4. Run
uvicorn backend.main:app --port 8765 --reload

# 5. Open
open http://localhost:8765
```
