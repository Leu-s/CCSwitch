import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from sqlalchemy import select

from . import background as bg
from .config import settings as cfg
from .database import init_db, AsyncSessionLocal
from .models import Account
from .routers import accounts, settings, tmux, service
from .services import account_service as ac
from .services import settings_service as ss
from .services.settings_service import ensure_defaults
from .ws import ws_manager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def _get_idle_interval() -> int:
    """Read usage_poll_interval_seconds from DB for the no-client fallback."""
    try:
        async with AsyncSessionLocal() as db:
            val = await ss.get_int("usage_poll_interval_seconds", cfg.poll_interval_idle, db)
            return max(val, cfg.poll_interval_min)
    except Exception as e:
        logger.warning("Could not read poll interval from DB: %s", e)
    return cfg.poll_interval_idle


async def _poll_loop(idle_interval: int) -> None:
    """
    Single polling loop:
    1. Poll immediately at startup so the cache is warm on first WS connect.
    2. While any client is connected, poll every poll_interval_active seconds.
    3. When nobody is watching, sleep in 5s chunks up to idle_interval so we
       react quickly when a client reconnects, then poll once.
    """
    await bg.poll_usage_and_switch(ws_manager)
    while True:
        if ws_manager.active_connections:
            await asyncio.sleep(cfg.poll_interval_active)
        else:
            elapsed = 0
            while elapsed < idle_interval and not ws_manager.active_connections:
                await asyncio.sleep(5)
                elapsed += 5
        await bg.poll_usage_and_switch(ws_manager)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()

    # Seed default settings so background tasks always have rows to read.
    async with AsyncSessionLocal() as db:
        await ensure_defaults(db)

    # Sync ~/.claude-multi/active on startup so CLAUDE_CONFIG_DIR works in new terminals
    # even before the first switch event occurs.
    active_email = ac.get_active_email()
    if active_email:
        async with AsyncSessionLocal() as db:
            row = await db.execute(select(Account).where(Account.email == active_email))
            acc = row.scalars().first()
            if acc:
                ac.write_active_config_dir(acc.config_dir)
                logger.info("Synced ~/.claude-multi/active → %s", acc.config_dir)

    idle_interval = await _get_idle_interval()
    logger.info("Poll intervals — active: %ds, idle: %ds", cfg.poll_interval_active, idle_interval)

    poll_task = asyncio.create_task(_poll_loop(idle_interval))
    logger.info("Server running on port %d", cfg.server_port)
    yield
    # Shutdown: cancel the background poll task
    poll_task.cancel()
    try:
        await poll_task
    except asyncio.CancelledError:
        pass
    logger.info("Background poll task stopped")


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
        # Send cached data immediately so the UI renders without waiting 15 s.
        # Snapshot under the cache lock to avoid "dictionary changed size during
        # iteration" when a poll fires concurrently.
        cache_snapshot = await bg.snapshot_usage_cache()
        if cache_snapshot:
            async with AsyncSessionLocal() as db:
                id_map = await ac.get_email_to_id_map(db)
            snapshot = [
                {
                    "id": id_map[email],
                    "email": email,
                    "usage": usage,
                    "error": usage.get("error"),
                }
                for email, usage in cache_snapshot.items()
                if id_map.get(email) is not None
            ]
            await websocket.send_text(
                json.dumps({"type": "usage_updated", "accounts": snapshot})
            )
        while True:
            await websocket.receive_text()
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        ws_manager.disconnect(websocket)


@app.get("/health")
async def health():
    return {"ok": True}
