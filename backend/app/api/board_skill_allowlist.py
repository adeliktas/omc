"""Per-board skill allowlist API (T34, E3 deeper surface, 2026-05-14).

Endpoints:
  GET  /boards/{board_id}/skill-allowlist
       → list current allow/deny entries for the board
  PUT  /boards/{board_id}/skill-allowlist/{skill_id}
       → set explicit allow/deny entry (body: {"allowed": bool, "notes": str?})
  DELETE /boards/{board_id}/skill-allowlist/{skill_id}
       → remove the entry (board defaults to "allow" for this skill again)

Semantics:
  - Empty allowlist → ALL skills allowed (default).
  - Any row with `allowed=False` for a (board, skill) pair → that skill is denied.
  - Any row with `allowed=True` → that skill is explicitly allowed (useful when
    paired with other False rows on the same board, OR when the board is in
    "deny-by-default" mode via env `SKILL_ALLOWLIST_DEFAULT=deny`).

Runtime check helper `is_skill_allowed(...)` lives in this module — both the
API surface and any agent-side gating code import the same function.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import delete as sa_delete, select as sa_select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_user_or_agent, get_session as _db_dep
from app.core.logging import get_logger
from app.models.board_skill_allowlist import BoardSkillAllowlist

logger = get_logger(__name__)
router = APIRouter(prefix="/boards/{board_id}/skill-allowlist", tags=["skill-allowlist"])

_DEFAULT_MODE = os.environ.get("SKILL_ALLOWLIST_DEFAULT", "allow").lower()


class AllowlistEntryRead(BaseModel):
    id: UUID
    board_id: UUID
    skill_id: UUID
    allowed: bool
    notes: str | None
    created_at: datetime
    updated_at: datetime


class AllowlistEntryWrite(BaseModel):
    allowed: bool = True
    notes: str | None = None


def _to_read(row: BoardSkillAllowlist) -> AllowlistEntryRead:
    return AllowlistEntryRead(
        id=row.id,
        board_id=row.board_id,
        skill_id=row.skill_id,
        allowed=row.allowed,
        notes=row.notes,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def is_skill_allowed(
    db: AsyncSession,
    *,
    board_id: UUID,
    skill_id: UUID,
) -> bool:
    """Return True if the skill is permitted on the board.

    Default-mode (`SKILL_ALLOWLIST_DEFAULT=allow`):
      - Explicit `allowed=False` row → blocked
      - Anything else → allowed
    Deny-mode (`SKILL_ALLOWLIST_DEFAULT=deny`):
      - Explicit `allowed=True` row → allowed
      - Anything else → blocked
    """
    res = await db.execute(
        sa_select(BoardSkillAllowlist).where(
            BoardSkillAllowlist.board_id == board_id,
            BoardSkillAllowlist.skill_id == skill_id,
        ),
    )
    row = res.scalar_one_or_none()
    if row is None:
        return _DEFAULT_MODE == "allow"
    return bool(row.allowed)


@router.get("", response_model=list[AllowlistEntryRead])
async def list_entries(
    board_id: UUID,
    db: AsyncSession = Depends(_db_dep),
    _actor=Depends(require_user_or_agent),
) -> list[AllowlistEntryRead]:
    res = await db.execute(
        sa_select(BoardSkillAllowlist).where(
            BoardSkillAllowlist.board_id == board_id,
        ).order_by(BoardSkillAllowlist.created_at.asc()),
    )
    return [_to_read(r) for r in res.scalars().all()]


@router.put("/{skill_id}", response_model=AllowlistEntryRead)
async def set_entry(
    board_id: UUID,
    skill_id: UUID,
    payload: AllowlistEntryWrite,
    db: AsyncSession = Depends(_db_dep),
    _actor=Depends(require_user_or_agent),
) -> AllowlistEntryRead:
    res = await db.execute(
        sa_select(BoardSkillAllowlist).where(
            BoardSkillAllowlist.board_id == board_id,
            BoardSkillAllowlist.skill_id == skill_id,
        ),
    )
    row = res.scalar_one_or_none()
    if row is None:
        row = BoardSkillAllowlist(
            board_id=board_id,
            skill_id=skill_id,
            allowed=payload.allowed,
            notes=payload.notes,
        )
        db.add(row)
    else:
        row.allowed = payload.allowed
        row.notes = payload.notes
        row.updated_at = datetime.utcnow()
        db.add(row)
    await db.commit()
    await db.refresh(row)
    logger.info(
        "skill_allowlist.set board=%s skill=%s allowed=%s",
        str(board_id)[:8], str(skill_id)[:8], payload.allowed,
    )
    return _to_read(row)


@router.delete("/{skill_id}")
async def delete_entry(
    board_id: UUID,
    skill_id: UUID,
    db: AsyncSession = Depends(_db_dep),
    _actor=Depends(require_user_or_agent),
) -> dict[str, Any]:
    res = await db.execute(
        sa_delete(BoardSkillAllowlist).where(
            BoardSkillAllowlist.board_id == board_id,
            BoardSkillAllowlist.skill_id == skill_id,
        ),
    )
    await db.commit()
    return {"deleted": res.rowcount, "board_id": str(board_id), "skill_id": str(skill_id)}
