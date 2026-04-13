import json
import logging
import re
import asyncio
import time

from sqlalchemy import select

from .database import AsyncSessionLocal
from .models import Account, Setting, TmuxMonitor
from .services import account_service as ac
from .services import anthropic_api
from .services import switcher as sw
from .services import tmux_service
from .ws import WebSocketManager
from .config import settings

logger = logging.getLogger(__name__)

# In-memory usage cache: {email: usage_dict}
usage_cache: dict[str, dict] = {}

# Timestamp of the last completed poll (monotonic seconds)
last_poll_time: float = 0.0


async def poll_usage_and_switch(ws: WebSocketManager) -> None:
    global last_poll_time
    last_poll_time = time.monotonic()
    async with AsyncSessionLocal() as db:
        # ── Settings ──────────────────────────────────────────────────────────
        service_row = await db.execute(
            select(Setting).where(Setting.key == "service_enabled")
        )
        service_setting = service_row.scalars().first()
        try:
            service_enabled = json.loads(service_setting.value) if service_setting else False
        except (json.JSONDecodeError, TypeError):
            service_enabled = False

        # ── Poll usage for every account (always, even if service is OFF) ──────
        accounts_result = await db.execute(select(Account))
        accounts = accounts_result.scalars().all()

        updated = []
        for account in accounts:
            try:
                token = ac.get_access_token_from_config_dir(account.config_dir)
                if not token:
                    raise ValueError("No access token found in config directory")

                usage = await anthropic_api.probe_usage(token)
                usage_cache[account.email] = usage
                updated.append({
                    "id": account.id,
                    "email": account.email,
                    "usage": usage,
                    "error": None,
                })
            except Exception as e:
                import httpx as _httpx
                err_str = str(e)
                if isinstance(e, _httpx.HTTPStatusError):
                    err_str = f"HTTP {e.response.status_code}"
                    try:
                        body = e.response.json()
                        msg = (body.get("error") or {}).get("message") or body.get("message")
                        if msg:
                            err_str = msg
                    except Exception:
                        pass
                logger.warning("Usage fetch failed for %s: %s", account.email, err_str)
                is_rate_limited = "429" in str(e) or "rate_limit" in str(e).lower()
                prev = usage_cache.get(account.email, {})
                if is_rate_limited and prev and "error" not in prev:
                    usage_cache[account.email] = {**prev, "rate_limited": True}
                    err_str = "Rate limited"
                else:
                    usage_cache[account.email] = {"error": err_str}
                updated.append({
                    "id": account.id,
                    "email": account.email,
                    "usage": usage_cache[account.email],
                    "error": err_str if "error" in usage_cache.get(account.email, {}) else None,
                })

        await ws.broadcast({"type": "usage_updated", "accounts": updated})

        if not service_enabled:
            return  # service is OFF — skip auto-switch

        # ── Auto-switch logic ─────────────────────────────────────────────────
        auto_row = await db.execute(
            select(Setting).where(Setting.key == "auto_switch_enabled")
        )
        auto_setting = auto_row.scalars().first()
        try:
            auto_enabled = json.loads(auto_setting.value) if auto_setting else True
        except (json.JSONDecodeError, TypeError):
            auto_enabled = True

        if not auto_enabled:
            return

        current_email = ac.get_active_email()
        if not current_email:
            return

        # Find the current account to get its per-account threshold
        cur_result = await db.execute(
            select(Account).where(Account.email == current_email)
        )
        current_account = cur_result.scalars().first()
        if not current_account:
            return

        current_usage = usage_cache.get(current_email, {})
        five_hour_pct = (current_usage.get("five_hour") or {}).get("utilization", 0)
        threshold = current_account.threshold_pct

        if five_hour_pct >= threshold:
            next_account = await sw.get_next_account(current_email, db)
            if next_account:
                logger.info(
                    "Auto-switching %s → %s (usage %.1f%% ≥ threshold %.1f%%)",
                    current_email, next_account.email, five_hour_pct, threshold,
                )
                await sw.perform_switch(next_account, "threshold", db, ws)

                # Notify tmux monitors
                monitors_result = await db.execute(
                    select(TmuxMonitor).where(TmuxMonitor.enabled == True)
                )
                monitors = monitors_result.scalars().all()
                await _notify_tmux_monitors(monitors, ws, settings.haiku_model)
            else:
                logger.warning("No eligible account to switch to")
                await ws.broadcast({
                    "type": "error",
                    "message": "Rate limit reached — no eligible accounts to switch to",
                })


async def _notify_tmux_monitors(monitors, ws: WebSocketManager, model: str) -> None:
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
                    "capture": capture,
                })
            except Exception as e:
                await ws.broadcast({
                    "type": "tmux_result",
                    "monitor_id": monitor.id,
                    "target": pane["target"],
                    "status": "FAILED",
                    "explanation": str(e),
                    "capture": "",
                })
