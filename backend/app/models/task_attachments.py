"""Files attached to a task: an image to analyse, a spec to read.

Stored on disk under a task-scoped directory the coding-harness runner can
read directly, so an agent works on the real file rather than a copy pasted
into a comment.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlmodel import Field, SQLModel

from app.core.time import utcnow


class TaskAttachment(SQLModel, table=True):
    __tablename__ = "task_attachments"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    # Matches migration s3c4d5e6f7a8 (ON DELETE CASCADE).
    task_id: UUID = Field(index=True, foreign_key="tasks.id", ondelete="CASCADE")
    board_id: UUID
    filename: str
    content_type: str
    size_bytes: int
    # Absolute path on the shared volume. The harness runner reads this path
    # directly; nothing streams file bytes through the board.
    storage_path: str
    sha256: str
    created_by_type: str | None = None
    created_by_name: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
