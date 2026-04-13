import asyncio
import os

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from ..database import get_db
from ..models import Setting
from ..schemas import SettingOut, SettingUpdate
from ..services.settings_service import ensure_defaults
from ..services import account_service as ac
from ..config import settings as cfg

router = APIRouter(prefix="/api/settings", tags=["settings"])

INTERNAL_KEYS = {"original_credentials_backup"}

ALLOWED_KEYS = {
    "auto_switch_enabled",
    "usage_poll_interval_seconds",
}


def _shell_snippet_path() -> str:
    """Unexpanded path string embedded in the shell snippet — keeps the tilde
    so the snippet sourced from a user's rc file resolves at runtime."""
    return f"{cfg.state_dir.rstrip('/')}/active"


@router.get("", response_model=list[SettingOut])
async def get_settings(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Setting).where(Setting.key.notin_(INTERNAL_KEYS)))
    return result.scalars().all()

@router.get("/shell-status")
async def shell_status():
    """
    Check whether the shell is configured to use the active-dir pointer and
    whether that pointer file currently exists.
    """
    def _check() -> dict:
        active_file_exists = os.path.isfile(ac.active_dir_pointer_path())
        snippet_path = _shell_snippet_path()
        shell_configured = False
        for rc_name in [".zshrc", ".bashrc"]:
            path = os.path.expanduser(f"~/{rc_name}")
            if os.path.exists(path):
                try:
                    with open(path) as f:
                        if snippet_path in f.read():
                            shell_configured = True
                            break
                except OSError:
                    pass
        return {"active_file_exists": active_file_exists, "shell_configured": shell_configured}

    return await asyncio.to_thread(_check)


@router.post("/setup-shell")
async def setup_shell():
    """
    Append the CLAUDE_CONFIG_DIR one-liner to ~/.zshrc and/or ~/.bashrc
    if not already present. Returns per-file status.
    """
    snippet_path = _shell_snippet_path()
    one_liner = (
        f'_d=$(cat {snippet_path} 2>/dev/null); '
        f'[ -n "$_d" ] && export CLAUDE_CONFIG_DIR="$_d"; unset _d'
    )
    block = f'\n# Claude Code multi-account — active account isolation\n{one_liner}\n'

    def _apply() -> dict:
        results = {}
        for rc_name in [".zshrc", ".bashrc"]:
            path = os.path.expanduser(f"~/{rc_name}")
            if not os.path.exists(path):
                results[rc_name] = "not_found"
                continue
            try:
                with open(path) as f:
                    content = f.read()
                if snippet_path in content:
                    results[rc_name] = "already_configured"
                    continue
                with open(path, "a") as f:
                    f.write(block)
                results[rc_name] = "applied"
            except OSError as e:
                results[rc_name] = f"error: {e.strerror}"
        return results

    return {"results": await asyncio.to_thread(_apply)}


@router.patch("/{key}", response_model=SettingOut)
async def update_setting(key: str, payload: SettingUpdate, db: AsyncSession = Depends(get_db)):
    if key not in ALLOWED_KEYS:
        raise HTTPException(status_code=403, detail="Setting key not allowed")
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
