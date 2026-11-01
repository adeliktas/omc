"""Task stage timing model — measures how long a task spends in each status stage.

Each row records one continuous stay in a stage. When a task moves out of a
stage, `exited_at` is set and `duration_seconds` is computed. Re-entries into
the same stage (e.g. rework cycles) create new rows; durations accumulate on
read via aggregation.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlmodel import Field

from app.core.time import utcnow
from app.models.base import QueryModel

RUNTIME_ANNOTATION_TYPES = (datetime,)


class TaskStageTiming(QueryModel, table=True):
    """Accumulated timing record for one continuous stay in a task status stage."""

    __tablename__ = "task_stage_timings"  # pyright: ignore[reportAssignmentType]

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    task_id: UUID = Field(foreign_key="tasks.id", index=True)
    status: str = Field(index=True)  # inbox / in_progress / review
    agent_id: UUID | None = Field(
        default=None,
        foreign_key="agents.id",
        index=True,
    )  # agent active during this stage
    entered_at: datetime = Field(default_factory=utcnow)
    exited_at: datetime | None = Field(default=None)
    duration_seconds: int | None = Field(
        default=None,
    )  # whole seconds between entered_at and exited_at
    created_at: datetime = Field(default_factory=utcnow)
