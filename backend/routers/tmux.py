from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from ..database import get_db
from ..models import TmuxMonitor
from ..schemas import TmuxMonitorCreate, TmuxMonitorOut, TmuxPane
from ..services import tmux_service

router = APIRouter(prefix="/api/tmux", tags=["tmux"])

@router.get("/sessions", response_model=list[TmuxPane])
async def list_sessions():
    return tmux_service.list_panes()

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
async def update_monitor(monitor_id: int, payload: TmuxMonitorCreate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(TmuxMonitor).where(TmuxMonitor.id == monitor_id))
    monitor = result.scalars().first()
    if not monitor:
        raise HTTPException(404, "Monitor not found")
    for field, value in payload.model_dump().items():
        setattr(monitor, field, value)
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
