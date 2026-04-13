from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from apscheduler.schedulers.asyncio import AsyncIOScheduler
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
            await websocket.receive_text()  # keep alive
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)

@app.get("/health")
async def health():
    return {"ok": True}
