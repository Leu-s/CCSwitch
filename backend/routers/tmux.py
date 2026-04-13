import re

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel

from ..database import get_db
from ..models import TmuxMonitor
from ..schemas import TmuxMonitorCreate, TmuxMonitorOut, TmuxMonitorUpdate, TmuxPane
from ..services import tmux_service

router = APIRouter(prefix="/api/tmux", tags=["tmux"])

@router.get("/sessions", response_model=list[TmuxPane])
async def list_sessions():
    return tmux_service.list_panes()

@router.get("/capture")
async def capture_session(target: str, lines: int = Query(50, ge=1, le=500)):
    try:
        output = tmux_service.capture_pane(target, lines)
        return {"target": target, "output": output}
    except Exception as e:
        return {"target": target, "output": f"(error: {e})"}

class SendKeysPayload(BaseModel):
    target: str
    text: str
    press_enter: bool = True

@router.post("/send")
async def send_keys(payload: SendKeysPayload):
    try:
        tmux_service.send_keys(payload.target, payload.text, payload.press_enter)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(400, str(e))

@router.get("/monitors", response_model=list[TmuxMonitorOut])
async def list_monitors(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(TmuxMonitor))
    return result.scalars().all()

@router.post("/monitors", response_model=TmuxMonitorOut, status_code=201)
async def create_monitor(payload: TmuxMonitorCreate, db: AsyncSession = Depends(get_db)):
    monitor = TmuxMonitor(**payload.model_dump())
    db.add(monitor)
    await db.commit()
    await db.refresh(monitor)
    return monitor

@router.patch("/monitors/{monitor_id}", response_model=TmuxMonitorOut)
async def update_monitor(monitor_id: int, payload: TmuxMonitorUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(TmuxMonitor).where(TmuxMonitor.id == monitor_id))
    monitor = result.scalars().first()
    if not monitor:
        raise HTTPException(404, "Monitor not found")
    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(monitor, field, value)
    # Re-validate the pattern if the resulting monitor is a regex monitor.
    if monitor.pattern_type == "regex":
        try:
            re.compile(monitor.pattern)
        except re.error as e:
            raise HTTPException(400, f"Invalid regex pattern: {e}")
    await db.commit()
    await db.refresh(monitor)
    return monitor

@router.delete("/monitors/{monitor_id}", status_code=204)
async def delete_monitor(monitor_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(TmuxMonitor).where(TmuxMonitor.id == monitor_id))
    monitor = result.scalars().first()
    if not monitor:
        raise HTTPException(404, "Monitor not found")
    await db.delete(monitor)
    await db.commit()
