from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime
import logging
import os

from sqlalchemy import select

from .database import init_db, AsyncSessionLocal
from .models import Setting
from .ws import ws_manager
from .background import poll_usage_and_switch
from .routers import accounts, settings, tmux, service
from .config import settings as cfg

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_MIN_POLL_INTERVAL = 120   # never faster than 2 min — Anthropic API rate-limits
_DEFAULT_POLL_INTERVAL = 300  # 5 minutes default

scheduler = AsyncIOScheduler()


async def _get_poll_interval() -> int:
    """Read usage_poll_interval_seconds from DB, clamped to a safe minimum."""
    try:
        async with AsyncSessionLocal() as db:
            row = await db.execute(
                select(Setting).where(Setting.key == "usage_poll_interval_seconds")
            )
            s = row.scalars().first()
            if s:
                val = int(s.value)
                if val < _MIN_POLL_INTERVAL:
                    logger.warning(
                        "usage_poll_interval_seconds=%d is below minimum %d; using %d",
                        val, _MIN_POLL_INTERVAL, _MIN_POLL_INTERVAL,
                    )
                    return _MIN_POLL_INTERVAL
                return val
    except Exception as e:
        logger.warning("Could not read poll interval from DB: %s", e)
    return _DEFAULT_POLL_INTERVAL


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()

    poll_interval = await _get_poll_interval()
    logger.info("Usage poll interval: %d seconds", poll_interval)

    scheduler.add_job(
        poll_usage_and_switch,
        "interval",
        seconds=poll_interval,
        args=[ws_manager],
        id="usage_poll",
        replace_existing=True,
        next_run_time=datetime.now(),  # poll immediately on startup
    )
    scheduler.start()
    logger.info("Server running on port %d", cfg.server_port)
    yield
    scheduler.shutdown()


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
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)


@app.get("/health")
async def health():
    return {"ok": True}
