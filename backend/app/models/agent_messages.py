"""Typed inter-agent message bus (T25, E2, 2026-05-14).

Replaces ad-hoc marker scanning for handoff / peer-review / vote signals.
Each row is a single typed message addressed agent→agent (or broadcast via
NULL `to_agent_id`). PMs use it for routing; workers use it for handoff.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel

RUNTIME_ANNOTATION_TYPES = (datetime,)


class AgentMessage(SQLModel, table=True):
    """One typed message between agents (or broadcast)."""

    __tablename__ = "agent_messages"  # pyright: ignore[reportAssignmentType]

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    from_agent_id: UUID | None = Field(default=None, foreign_key="agents.id", index=True)
    to_agent_id: UUID | None = Field(default=None, foreign_key="agents.id", index=True)
    board_id: UUID | None = Field(default=None, foreign_key="boards.id", index=True)
    task_id: UUID | None = Field(default=None, foreign_key="tasks.id", index=True)
    message_type: str = Field(index=True)
    # Generic JSON for cross-DB (SQLite tests + Postgres prod). Postgres stores
    # this as JSON, not JSONB, but functionally equivalent for our query usage.
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    status: str = Field(default="unread", index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    read_at: datetime | None = None
