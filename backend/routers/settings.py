import os

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from ..database import get_db
from ..models import Setting
from ..schemas import SettingOut, SettingUpdate

router = APIRouter(prefix="/api/settings", tags=["settings"])

DEFAULTS = {
    "auto_switch_enabled": "true",
    "usage_poll_interval_seconds": "300",
    # service_enabled / default_account_id / original_credentials_backup
    # are managed by the /api/service router — not initialised here.
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

@router.get("/shell-status")
async def shell_status():
    """
    Check whether the shell is configured to use ~/.claude-multi/active
    and whether the active config file exists.
    """
    active_file = os.path.expanduser("~/.claude-multi/active")
    active_file_exists = os.path.isfile(active_file)

    shell_configured = False
    for rc_name in [".zshrc", ".bashrc"]:
        path = os.path.expanduser(f"~/{rc_name}")
        if os.path.exists(path):
            try:
                with open(path) as f:
                    if "claude-multi/active" in f.read():
                        shell_configured = True
                        break
            except OSError:
                pass

    return {"active_file_exists": active_file_exists, "shell_configured": shell_configured}


@router.post("/setup-shell")
async def setup_shell():
    """
    Append the CLAUDE_CONFIG_DIR one-liner to ~/.zshrc and/or ~/.bashrc
    if not already present. Returns per-file status.
    """
    one_liner = '_d=$(cat ~/.claude-multi/active 2>/dev/null); [ -n "$_d" ] && export CLAUDE_CONFIG_DIR="$_d"; unset _d'
    block = f'\n# Claude Code multi-account — active account isolation\n{one_liner}\n'

    results = {}
    for rc_name in [".zshrc", ".bashrc"]:
        path = os.path.expanduser(f"~/{rc_name}")
        if not os.path.exists(path):
            results[rc_name] = "not_found"
            continue
        try:
            with open(path) as f:
                content = f.read()
            if "claude-multi/active" in content:
                results[rc_name] = "already_configured"
                continue
            with open(path, "a") as f:
                f.write(block)
            results[rc_name] = "applied"
        except OSError as e:
            results[rc_name] = f"error: {e.strerror}"

    return {"results": results}


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
