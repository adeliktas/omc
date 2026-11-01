"""Board model for organization workspaces and goal configuration."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, Column
from sqlmodel import Field

from app.core.time import utcnow
from app.models.tenancy import TenantScoped

RUNTIME_ANNOTATION_TYPES = (datetime,)


class Board(TenantScoped, table=True):
    """Primary board entity grouping tasks, agents, and goal metadata."""

    __tablename__ = "boards"  # pyright: ignore[reportAssignmentType]

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    organization_id: UUID = Field(foreign_key="organizations.id", index=True)
    name: str
    slug: str = Field(index=True)
    description: str = Field(default="")
    gateway_id: UUID | None = Field(default=None, foreign_key="gateways.id", index=True)
    board_group_id: UUID | None = Field(
        default=None,
        foreign_key="board_groups.id",
        index=True,
    )
    board_type: str = Field(default="goal", index=True)
    objective: str | None = None
    success_metrics: dict[str, object] | None = Field(
        default=None,
        sa_column=Column(JSON),
    )
    target_date: datetime | None = None
    goal_confirmed: bool = Field(default=False)
    goal_source: str | None = None
    require_approval_for_done: bool = Field(default=True)
    require_review_before_done: bool = Field(default=False)
    comment_required_for_review: bool = Field(default=False)
    block_status_changes_with_pending_approval: bool = Field(default=False)
    only_lead_can_change_status: bool = Field(default=False)
    max_agents: int = Field(default=1)
    autonomous_tasks_enabled: bool = Field(default=True)
    cron_tasks_enabled: bool = Field(default=True)
    # T105 Phase 5 (2026-05-25): per-board Planner/Executor/Verifier loop
    # toggle. When true, NEW tasks on this board auto-start PEV (smart
    # planner + dumb executor + smart verifier with rollback safety net).
    # Existing tasks created before the toggle was flipped are unaffected.
    autonomous_enabled: bool = Field(default=False, index=True)
    # DEPRECATED 2026-05-13: cloud-toggle is global per owner decision;
    # column retained for migration compatibility but not read by toggle path.
    cloud_providers_enabled: bool = Field(default=True)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
