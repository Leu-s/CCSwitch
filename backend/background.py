import json
import logging
import time
import re
import asyncio
from sqlalchemy import select
from .database import AsyncSessionLocal
from .models import Account, Setting, TmuxMonitor
from .services import keychain as kc, anthropic_api, switcher as sw
from .services import tmux_service
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
        try:
            auto_enabled = json.loads(auto_setting.value) if auto_setting else True
        except (json.JSONDecodeError, TypeError):
            auto_enabled = True

        threshold_row = await db.execute(select(Setting).where(Setting.key == "switch_threshold_percent"))
        threshold_setting = threshold_row.scalars().first()
        try:
            threshold = json.loads(threshold_setting.value) if threshold_setting else 90
        except (json.JSONDecodeError, TypeError):
            threshold = 90

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
                expires_at = oauth.get("expiresAt") or 0
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
                monitors_result = await db.execute(select(TmuxMonitor).where(TmuxMonitor.enabled == True))
                monitors = monitors_result.scalars().all()
                await notify_tmux_monitors(monitors, ws, settings.haiku_model)
            else:
                logger.warning("No eligible account to switch to")
                await ws.broadcast({"type": "error", "message": "Rate limit reached — no eligible accounts to switch to"})

async def notify_tmux_monitors(monitors, ws: WebSocketManager, model: str) -> None:
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
