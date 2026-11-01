"""PEV API — manual control of the Planner / Executor / Verifier loop.

T105 Phase 3 (2026-05-25).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_user_or_agent
from app.core.auth import SESSION_DEP
from app.services.pev_loop import (
    advance_pev,
    list_active_pev_tasks,
    start_pev,
)

router = APIRouter(prefix="/pev", tags=["pev"])


@router.get("/tasks", summary="List PEV-active tasks")
async def list_(
    session: AsyncSession = SESSION_DEP,
    _: None = Depends(require_user_or_agent),
) -> list[dict[str, Any]]:
    return await list_active_pev_tasks(session)


@router.get("/tasks/{task_id}", summary="Get PEV state for a task")
async def get_(
    task_id: UUID,
    session: AsyncSession = SESSION_DEP,
    _: None = Depends(require_user_or_agent),
) -> dict[str, Any]:
    res = await session.execute(text(
        "SELECT * FROM task_pev_state WHERE task_id = :tid"
    ), {"tid": str(task_id)})
    r = res.first()
    if not r:
        raise HTTPException(status_code=404, detail="no PEV state for this task")
    return dict(r._mapping)


@router.post("/tasks/{task_id}/start", summary="Initialize PEV loop for a task")
async def start_(
    task_id: UUID,
    session: AsyncSession = SESSION_DEP,
    _: None = Depends(require_user_or_agent),
) -> dict[str, Any]:
    # Verify task exists
    res = await session.execute(text("SELECT id FROM tasks WHERE id = :tid"), {"tid": str(task_id)})
    if not res.first():
        raise HTTPException(status_code=404, detail="task not found")
    return await start_pev(session, task_id)


@router.post("/tasks/{task_id}/advance", summary="Drive PEV loop one phase forward")
async def advance_(
    task_id: UUID,
    session: AsyncSession = SESSION_DEP,
    _: None = Depends(require_user_or_agent),
) -> dict[str, Any]:
    return await advance_pev(session, task_id)


@router.delete("/tasks/{task_id}", summary="Clear PEV state for a task")
async def delete_(
    task_id: UUID,
    session: AsyncSession = SESSION_DEP,
    _: None = Depends(require_user_or_agent),
) -> dict[str, Any]:
    await session.execute(
        text("DELETE FROM task_pev_state WHERE task_id = :tid"),
        {"tid": str(task_id)},
    )
    await session.commit()
    return {"ok": True}
