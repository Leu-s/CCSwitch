"""
Credential-targets router — auto-discovered ``.claude.json`` mirror list.

The dashboard auto-discovers every known ``.claude.json`` location on the
machine and surfaces it here with a checkbox.  Nothing outside the isolated
account dirs is written on a switch unless the user explicitly opts in.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..schemas import CredentialTargetOut, CredentialTargetUpdate
from ..services import credential_targets as ct
from ..services import switcher as sw

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/credential-targets", tags=["credential-targets"])


@router.get("", response_model=list[CredentialTargetOut])
async def list_credential_targets(db: AsyncSession = Depends(get_db)):
    """Auto-discover + persisted enabled state, joined."""
    return await ct.list_targets(db)


@router.post("/rescan", response_model=list[CredentialTargetOut])
async def rescan_credential_targets(db: AsyncSession = Depends(get_db)):
    """Re-run discovery and return the refreshed list.  Equivalent to the
    GET endpoint — discovery runs on every request — but exposed as POST so
    UIs can distinguish a user-initiated rescan from a routine reload."""
    return await ct.list_targets(db)


@router.patch("", response_model=list[CredentialTargetOut])
async def set_credential_target(
    payload: CredentialTargetUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Toggle the ``enabled`` flag for a single canonical target."""
    canonical = (payload.canonical or "").strip()
    if not canonical:
        raise HTTPException(status_code=400, detail="canonical path required")
    try:
        await ct.set_target_enabled(canonical, payload.enabled, db)
    except Exception as e:
        logger.exception("failed to update credential target %s", canonical)
        raise HTTPException(status_code=500, detail=str(e)) from e
    return await ct.list_targets(db)


@router.post("/sync")
async def sync_credential_targets(db: AsyncSession = Depends(get_db)):
    """Re-mirror the currently active account into every enabled credential
    target.  Use this after ticking new boxes to backfill them immediately,
    instead of waiting until the next account switch."""
    summary = await sw.perform_sync_to_targets(db)
    targets = await ct.list_targets(db)
    return {"summary": summary, "targets": targets}
