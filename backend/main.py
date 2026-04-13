import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import background as bg
from .auth import TokenAuthMiddleware
from .config import settings as cfg
from .database import init_db, AsyncSessionLocal
from .routers import accounts, settings, tmux, service
from .services import account_service as ac
from .services import account_queries as aq
from .services import login_session_service as ls
from .services.account_service import build_usage
from .services import settings_service as ss
from .services.settings_service import ensure_defaults
from .background import cache as bg_cache
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
    try:
        await bg.poll_usage_and_switch(ws_manager)
    except Exception as exc:
        logger.exception("Initial poll failed: %s", exc)
    while True:
        try:
            if ws_manager.active_connections:
                await asyncio.sleep(cfg.poll_interval_active)
            else:
                # Re-read idle_interval from the DB so in-app setting changes
                # take effect without a server restart.
                current_idle = await _get_idle_interval()
                elapsed = 0
                while elapsed < current_idle and not ws_manager.active_connections:
                    await asyncio.sleep(5)
                    elapsed += 5
            await bg.poll_usage_and_switch(ws_manager)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("poll_usage_and_switch raised unexpectedly: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()

    # Seed default settings so background tasks always have rows to read.
    async with AsyncSessionLocal() as db:
        await ensure_defaults(db)

    # Sync ~/.claude-multi/active on startup so CLAUDE_CONFIG_DIR works in new terminals
    # even before the first switch event occurs.
    active_email = await asyncio.to_thread(ac.get_active_email)
    if active_email:
        async with AsyncSessionLocal() as db:
            acc = await aq.get_account_by_email(active_email, db)
            if acc:
                await asyncio.to_thread(ac.write_active_config_dir, acc.config_dir)
                logger.info("Synced ~/.claude-multi/active → %s", acc.config_dir)

    idle_interval = await _get_idle_interval()
    logger.info("Poll intervals — active: %ds, idle: %ds", cfg.poll_interval_active, idle_interval)

    async def _cleanup_sessions_loop() -> None:
        """Periodically clean up expired login sessions (every 5 minutes).
        _cleanup_expired_sessions does shutil.rmtree per stale session, so
        run it in a worker thread to keep the event loop responsive."""
        while True:
            await asyncio.sleep(300)
            await asyncio.to_thread(ls._cleanup_expired_sessions)

    tasks = [
        asyncio.create_task(_poll_loop(idle_interval)),
        asyncio.create_task(_cleanup_sessions_loop()),
    ]
    logger.info("Server running on port %d", cfg.server_port)
    yield
    # Shutdown: cancel all background tasks
    for task in tasks:
        task.cancel()
    for task in tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
    logger.info("Background tasks stopped")


app = FastAPI(title="Claude Multi-Account Manager", lifespan=lifespan)
app.add_middleware(TokenAuthMiddleware, api_token=cfg.api_token)

app.include_router(accounts.router)
app.include_router(settings.router)
app.include_router(tmux.router)
app.include_router(service.router)

# Serve frontend
frontend_path = os.path.join(os.path.dirname(__file__), "..", "frontend")

# Mount static assets (CSS, JS) under /src so index.html can reference them.
_src_path = os.path.join(frontend_path, "src")
if os.path.isdir(_src_path):
    app.mount("/src", StaticFiles(directory=_src_path), name="frontend_src")


@app.get("/")
async def root():
    index = os.path.join(frontend_path, "index.html")
    if os.path.exists(index):
        return FileResponse(index)
    return {"message": "Claude Multi-Account Manager API"}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, since: int = 0):
    await ws_manager.connect(websocket)
    try:
        # If the client supplies ?since=N, replay buffered events they missed.
        # Falls back to a full-state snapshot when the buffer does not cover the gap.
        if since > 0:
            missed = ws_manager.replay_since(since)
            if missed is None:
                # Buffer gap — send full snapshot so the client can resync.
                since = 0
            else:
                for text in missed:
                    await websocket.send_text(text)

        # Send the full state snapshot on first connect or after a buffer gap.
        if since == 0:
            cache_snapshot = await bg_cache.snapshot()
            if cache_snapshot:
                async with AsyncSessionLocal() as db:
                    id_map = await aq.get_email_to_id_map(db)
                snapshot = []
                for email, usage in cache_snapshot.items():
                    acct_id = id_map.get(email)
                    if acct_id is None:
                        continue
                    token_info = await bg_cache.get_token_info_async(email) or {}
                    flat = build_usage(usage, token_info)
                    snapshot.append({
                        "id": acct_id,
                        "email": email,
                        "usage": flat.model_dump() if flat else {},
                        "error": usage.get("error"),
                    })
                await websocket.send_text(
                    json.dumps({"type": "usage_updated", "accounts": snapshot})
                )

        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as _ws_err:
        logger.warning("WebSocket handler error: %s", _ws_err)
    finally:
        ws_manager.disconnect(websocket)


@app.get("/health")
async def health():
    return {"ok": True}
