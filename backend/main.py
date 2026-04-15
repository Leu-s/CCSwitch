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
from .routers import accounts, service, settings
from .services import account_service as ac
from .services import credential_provider as cp
from .services import login_session_service as ls
from .services import settings_service as ss
from .services.settings_service import ensure_defaults
from .ws import ws_manager


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def _get_idle_interval() -> int:
    """Read usage_poll_interval_seconds from DB for the no-client fallback."""
    try:
        async with AsyncSessionLocal() as db:
            val = await ss.get_int(
                "usage_poll_interval_seconds", cfg.poll_interval_idle, db
            )
            return max(val, cfg.poll_interval_min)
    except Exception as e:
        logger.warning("Could not read poll interval from DB: %s", e)
    return cfg.poll_interval_idle


async def _wait_for_keychain() -> None:
    """Wait until the login keychain is unlocked before starting the poll
    loop.  On a fresh boot, FileVault + Touch ID can delay unlock for
    several seconds — polling a locked keychain surfaces spurious "no
    access token" errors on every account.
    """
    delays = [5, 10, 30, 60, 120, 300]
    for idx, delay in enumerate(delays):
        if await asyncio.to_thread(cp.probe_keychain_available):
            if idx > 0:
                logger.info("Keychain unlocked — resuming normal operation")
            return
        logger.warning(
            "Keychain not available (attempt %d) — retrying in %ds",
            idx + 1, delay,
        )
        await asyncio.sleep(delay)
    logger.error(
        "Keychain still unavailable after exhausting retry schedule — "
        "starting poll loop anyway; probes will fail until unlock"
    )


async def _poll_loop(idle_interval: int) -> None:
    """Single polling loop:

    1. Poll immediately at startup so the cache is warm on first WS connect.
    2. While any client is connected, poll every ``poll_interval_active``.
    3. When nobody is watching, sleep in 5 s chunks up to ``idle_interval``
       so the loop reacts quickly when a client reconnects.
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

    async with AsyncSessionLocal() as db:
        await ensure_defaults(db)

    # Wait for the login keychain before running any Keychain-touching code.
    await _wait_for_keychain()

    # Reconcile a crash-mid-swap state if one occurred.  The check is a
    # single Keychain read + identity file read — fast and side-effect
    # free when no mismatch exists.
    await asyncio.to_thread(ac.startup_integrity_check)

    idle_interval = await _get_idle_interval()
    logger.info(
        "Poll intervals — active: %ds, idle: %ds",
        cfg.poll_interval_active, idle_interval,
    )

    async def _cleanup_sessions_loop() -> None:
        while True:
            await asyncio.sleep(300)
            await asyncio.to_thread(ls._cleanup_expired_sessions)

    tasks = [
        asyncio.create_task(_poll_loop(idle_interval)),
        asyncio.create_task(_cleanup_sessions_loop()),
    ]
    logger.info("Server running on http://%s:%d", cfg.server_host, cfg.server_port)
    yield
    for task in tasks:
        task.cancel()
    for task in tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
    logger.info("Background tasks stopped")


app = FastAPI(title="CCSwitch", lifespan=lifespan)
app.add_middleware(TokenAuthMiddleware, api_token=cfg.api_token)

app.include_router(accounts.router)
app.include_router(settings.router)
app.include_router(service.router)

# Serve frontend
frontend_path = os.path.join(os.path.dirname(__file__), "..", "frontend")

_src_path = os.path.join(frontend_path, "src")
if os.path.isdir(_src_path):
    app.mount("/src", StaticFiles(directory=_src_path), name="frontend_src")


@app.get("/")
async def root():
    index = os.path.join(frontend_path, "index.html")
    if os.path.exists(index):
        return FileResponse(index)
    return {"message": "CCSwitch API"}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, since: int = 0):
    await ws_manager.connect(websocket)
    try:
        if since > 0:
            missed = ws_manager.replay_since(since)
            if missed is None:
                since = 0
            else:
                for text in missed:
                    await websocket.send_text(text)

        if since == 0:
            async with AsyncSessionLocal() as db:
                snapshot = await ac.build_ws_snapshot(db)
            if snapshot:
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
