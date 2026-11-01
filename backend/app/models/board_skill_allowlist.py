"""Per-board skill allowlist (T34, E3 deeper surface, 2026-05-14).

Lets owners restrict which marketplace skills are available on a given board.
Empty allowlist for a board = ALL skills allowed (default).
Explicit row with `allowed=False` = blocked.
Explicit row with `allowed=True` = allowed (only meaningful when other rows
on the same board are False; otherwise no effect).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlmodel import Field, SQLModel

RUNTIME_ANNOTATION_TYPES = (datetime,)


class BoardSkillAllowlist(SQLModel, table=True):
    """One row per (board, skill) gating decision."""

    __tablename__ = "board_skill_allowlist"  # pyright: ignore[reportAssignmentType]

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    board_id: UUID = Field(foreign_key="boards.id", index=True)
    skill_id: UUID = Field(foreign_key="marketplace_skills.id", index=True)
    allowed: bool = Field(default=True)
    notes: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
