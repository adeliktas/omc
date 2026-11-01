"""Owner Review queue model (T18, 2026-05-14).

Replaces the legacy `/traffic-data/OWNER_REVIEW.json` file. Backed by Postgres
for atomic writes + UI joins. Used by `services/watchdog_loop.py` (escalation,
infra incidents, reconciliation) and `services/owner_reviews.py` (CRUD).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlmodel import Field, SQLModel

RUNTIME_ANNOTATION_TYPES = (datetime,)


class OwnerReview(SQLModel, table=True):
    """Owner Review queue item — surfaces stuck tasks / infra incidents to the human owner."""

    __tablename__ = "owner_reviews"  # pyright: ignore[reportAssignmentType]

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    title: str
    type: str = Field(default="other", index=True)
    summary: str | None = None
    reason: str | None = None
    proposed_action: str | None = None
    submitted_by: str = Field(index=True)
    submitted_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    status: str = Field(default="pending", index=True)
    reviewed_at: datetime | None = None
    reviewer_note: str | None = None
    escalated_to_board_id: UUID | None = None
    task_id: UUID | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
