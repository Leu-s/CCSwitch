import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from sqlalchemy import select

from .config import settings as cfg
from .database import init_db, AsyncSessionLocal
from .models import Account
from .ws import ws_manager
from .services import account_service as ac
from .services import settings_service as ss
import backend.background as bg
from .background import poll_usage_and_switch
from .routers import accounts, settings, tmux, service

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_MIN_POLL_INTERVAL = 120    # floor when nobody is watching
_DEFAULT_POLL_INTERVAL = 300
_ACTIVE_POLL_INTERVAL = 15  # poll every 15 s while any client is connected


async def _get_idle_interval() -> int:
    """Read usage_poll_interval_seconds from DB for the no-client fallback."""
    try:
        async with AsyncSessionLocal() as db:
            val = await ss.get_int("usage_poll_interval_seconds", _DEFAULT_POLL_INTERVAL, db)
            return max(val, _MIN_POLL_INTERVAL)
    except Exception as e:
        logger.warning("Could not read poll interval from DB: %s", e)
    return _DEFAULT_POLL_INTERVAL


async def _poll_loop(idle_interval: int) -> None:
    """
    Smart polling loop:
    - While clients are connected: poll every _ACTIVE_POLL_INTERVAL seconds.
    - While no client is watching: poll every idle_interval seconds (fallback).
    """
    while True:
        if ws_manager.active_connections:
            await poll_usage_and_switch(ws_manager)
            await asyncio.sleep(_ACTIVE_POLL_INTERVAL)
        else:
            # Nobody watching — sleep in small chunks so we react quickly
            # when a client connects, but still refresh data occasionally.
            elapsed = 0
            while elapsed < idle_interval and not ws_manager.active_connections:
                await asyncio.sleep(5)
                elapsed += 5
            # If a client just connected the WS handler will send cached data;
            # do a fresh poll so the next broadcast has up-to-date numbers.
            await poll_usage_and_switch(ws_manager)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()

    # Sync ~/.claude-multi/active on startup so CLAUDE_CONFIG_DIR works in new terminals
    # even before the first switch event occurs.
    active_email = ac.get_active_email()
    if active_email:
        async with AsyncSessionLocal() as db:
            row = await db.execute(select(Account).where(Account.email == active_email))
            acc = row.scalars().first()
            if acc:
                ac._write_active_config_dir(acc.config_dir)
                logger.info("Synced ~/.claude-multi/active → %s", acc.config_dir)

    idle_interval = await _get_idle_interval()
    logger.info("Poll intervals — active: %ds, idle: %ds", _ACTIVE_POLL_INTERVAL, idle_interval)

    tasks = [
        asyncio.create_task(poll_usage_and_switch(ws_manager)),
        asyncio.create_task(_poll_loop(idle_interval)),
    ]
    logger.info("Server running on port %d", cfg.server_port)
    yield
    # Shutdown: cancel background tasks
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    logger.info("Background tasks stopped")


app = FastAPI(title="Claude Multi-Account Manager", lifespan=lifespan)

app.include_router(accounts.router)
app.include_router(settings.router)
app.include_router(tmux.router)
app.include_router(service.router)

# Serve frontend
frontend_path = os.path.join(os.path.dirname(__file__), "..", "frontend")


@app.get("/")
async def root():
    index = os.path.join(frontend_path, "index.html")
    if os.path.exists(index):
        return FileResponse(index)
    return {"message": "Claude Multi-Account Manager API"}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        # Send cached data immediately so the UI renders without waiting 15 s
        if bg.usage_cache:
            # Build an email→id map so the frontend can match by id (not just email)
            async with AsyncSessionLocal() as db:
                rows = await db.execute(select(Account.id, Account.email))
                id_map = {row[1]: row[0] for row in rows.all()}
            snapshot = [
                {
                    "id": id_map[email],
                    "email": email,
                    "usage": usage,
                    "error": usage.get("error"),
                }
                for email, usage in bg.usage_cache.items()
                if id_map.get(email) is not None
            ]
            await websocket.send_text(
                json.dumps({"type": "usage_updated", "accounts": snapshot})
            )
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)


@app.get("/health")
async def health():
    return {"ok": True}
