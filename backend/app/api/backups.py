"""Backups API — full-system snapshot + restore.

T105 (2026-05-25). Backs `/backups` UI.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import require_user_or_agent
from app.services.backup import (
    ALL_COMPONENTS,
    delete_snapshot,
    diff_snapshots,
    list_snapshots,
    read_manifest,
    restore_snapshot,
    take_snapshot,
)

router = APIRouter(prefix="/backups", tags=["backups"])


class CreateBackup(BaseModel):
    label: str = ""
    components: list[str] | None = None
    reason: str = "manual"


class RestoreBackup(BaseModel):
    components: list[str] | None = None
    confirm: bool = False  # owner must explicitly set true


@router.get("", summary="List snapshots")
async def list_(
    _: None = Depends(require_user_or_agent),
) -> dict[str, Any]:
    return {
        "snapshots": list_snapshots(),
        "components": ALL_COMPONENTS,
    }


@router.get("/{snapshot_id}", summary="Get one snapshot manifest")
async def get_(
    snapshot_id: str,
    _: None = Depends(require_user_or_agent),
) -> dict[str, Any]:
    m = read_manifest(snapshot_id)
    if not m:
        raise HTTPException(status_code=404, detail="snapshot not found")
    return m


@router.post("", summary="Create a snapshot (full or partial)")
async def create_(
    payload: CreateBackup,
    _: None = Depends(require_user_or_agent),
) -> dict[str, Any]:
    return await take_snapshot(
        label=payload.label,
        components=payload.components,
        reason=payload.reason,
    )


@router.post("/{snapshot_id}/restore", summary="Restore from a snapshot")
async def restore_(
    snapshot_id: str,
    payload: RestoreBackup,
    _: None = Depends(require_user_or_agent),
) -> dict[str, Any]:
    if not payload.confirm:
        raise HTTPException(
            status_code=400,
            detail="restore requires `confirm: true` — destructive operation",
        )
    return await restore_snapshot(snapshot_id, components=payload.components)


@router.delete("/{snapshot_id}", summary="Delete a snapshot")
async def delete_(
    snapshot_id: str,
    _: None = Depends(require_user_or_agent),
) -> dict[str, Any]:
    ok = await delete_snapshot(snapshot_id)
    if not ok:
        raise HTTPException(status_code=404, detail="not found")
    return {"ok": True}


@router.get("/diff/{from_id}/{to_id}", summary="Component-level diff")
async def diff_(
    from_id: str,
    to_id: str,
    _: None = Depends(require_user_or_agent),
) -> dict[str, Any]:
    return diff_snapshots(from_id, to_id)
