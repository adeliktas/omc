"""Schemas for task CRUD and task comment API payloads."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import StrictInt, field_validator, model_validator
from sqlmodel import Field, SQLModel
from sqlmodel._compat import SQLModelConfig

from app.schemas.common import NonEmptyStr
from app.schemas.tags import TagRef
from app.schemas.task_custom_fields import TaskCustomFieldValues

# T103 v12 (2026-05-19): added 'blocked' + 'cancelled' to support watchdog
# auto-block (T103 v10) + manual force-cancel paths. Board snapshot, task
# CRUD, and frontend column rendering all read this Literal.
TaskStatus = Literal["inbox", "in_progress", "review", "done", "blocked", "cancelled", "paused", "trash"]
STATUS_REQUIRED_ERROR = "status is required"
# Keep these symbols as runtime globals so Pydantic can resolve
# deferred annotations reliably.
RUNTIME_ANNOTATION_TYPES = (datetime, UUID, NonEmptyStr, TagRef)


ExecutionLane = Literal["harness", "agent"]
# "single" = legacy one-agent run; "team" = native coordinated team cycle.
ExecutionMode = Literal["single", "team"]
# Mirrors the harness CLI vocabulary (`pi --thinking`).
ThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"]


class TaskBase(SQLModel):
    """Shared task fields used by task create/read payloads."""

    title: str
    description: str | None = None
    status: TaskStatus = "inbox"
    priority: str = "medium"
    due_at: datetime | None = None
    assigned_agent_id: UUID | None = None
    depends_on_task_ids: list[UUID] = Field(default_factory=list)
    tag_ids: list[UUID] = Field(default_factory=list)
    created_by_type: str | None = None
    created_by_name: str | None = None
    # ITIL-light complexity 1..5 = L1..L5. Set by PM via [pm-plan] marker or
    # explicit API field. NULL means PM has not yet assessed.
    complexity: int | None = None
    # Which lane executes this task. "harness" = the coding-harness runner
    # (vendor CLIs, real file edits, its own checkout); "agent" = a gateway
    # agent. NULL keeps the legacy title-prefix convention.
    execution_lane: ExecutionLane | None = None
    # Absolute folder the harness runner should work in for this task.
    workspace_path: str | None = None
    harness_provider: str | None = None
    harness_model: str | None = None
    thinking_level: ThinkingLevel | None = None
    execution_mode: ExecutionMode = "single"


class TaskCreate(TaskBase):
    """Payload for creating a task."""

    model_config = SQLModelConfig(extra="forbid")

    created_by_user_id: UUID | None = None
    custom_field_values: TaskCustomFieldValues = Field(default_factory=dict)
    repeat_cron: str | None = None
    repeat_enabled: bool = False
    repeat_interval_minutes: int | None = None
    scheduled_at: datetime | None = None
    # T22 (E4 Phase 1, 2026-05-14): per-task budgets.
    max_seconds_budget: int | None = None
    max_tokens_budget: int | None = None
    # T24a (E1 Phase 2, 2026-05-14): owner-driven DAG. When set, on create the
    # API atomically materializes child tasks + TaskDependency edges and the
    # parent flips to `in_progress` immediately (skips the PM marker round-trip).
    # Same JSON shape as `[pm-plan].subtasks` so the dag_materialize service
    # path is reused.
    subtasks: list[dict] | None = None
    # T39 (2026-05-14): allow `depends_on_task_ids` to reference tasks on
    # OTHER boards. Caller must have read access to the referenced board(s)
    # — service-layer ACL checks accessible_board_ids. Default False so
    # legacy callers keep same-board safety.
    allow_cross_board_deps: bool = False


class TaskUpdate(SQLModel):
    """Payload for partial task updates."""

    model_config = SQLModelConfig(extra="forbid")

    title: str | None = None
    description: str | None = None
    status: TaskStatus | None = None
    # Explicit null disables this discretionary budget; omission leaves it unchanged.
    max_tokens_budget: StrictInt | None = Field(default=None, gt=0, le=2_147_483_647)
    max_seconds_budget: StrictInt | None = Field(default=None, gt=0, le=2_147_483_647)
    # Optional optimistic claim preconditions; never persisted on the task.
    expected_status: TaskStatus | None = None
    expected_updated_at: datetime | None = None
    priority: str | None = None
    due_at: datetime | None = None
    assigned_agent_id: UUID | None = None
    depends_on_task_ids: list[UUID] | None = None
    tag_ids: list[UUID] | None = None
    custom_field_values: TaskCustomFieldValues | None = None
    comment: NonEmptyStr | None = None
    repeat_cron: str | None = None
    repeat_enabled: bool | None = None
    repeat_interval_minutes: int | None = None
    scheduled_at: datetime | None = None
    complexity: int | None = None
    execution_lane: ExecutionLane | None = None
    workspace_path: str | None = None
    harness_provider: str | None = None
    harness_model: str | None = None
    thinking_level: ThinkingLevel | None = None
    execution_mode: ExecutionMode | None = None
    # T39 (2026-05-14): also allow on update so callers can add cross-board
    # edges after creation.
    allow_cross_board_deps: bool = False

    @field_validator("comment", mode="before")
    @classmethod
    def normalize_comment(cls, value: object) -> object | None:
        """Normalize blank comment strings to `None`."""
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def validate_status(self) -> Self:
        """Ensure explicitly supplied status is not null."""
        if "status" in self.model_fields_set and self.status is None:
            raise ValueError(STATUS_REQUIRED_ERROR)
        return self


class TaskRead(TaskBase):
    """Task payload returned from read endpoints."""

    id: UUID
    board_id: UUID | None
    created_by_user_id: UUID | None
    paused_from_status: TaskStatus | None = None
    auto_created: bool = False
    auto_reason: str | None = None
    created_by_type: str | None = None
    created_by_name: str | None = None
    in_progress_at: datetime | None
    created_at: datetime
    updated_at: datetime
    blocked_by_task_ids: list[UUID] = Field(default_factory=list)
    is_blocked: bool = False
    tags: list[TagRef] = Field(default_factory=list)
    custom_field_values: TaskCustomFieldValues | None = None
    pm_lock_agent_id: UUID | None = None
    pm_lock_at: datetime | None = None
    repeat_cron: str | None = None
    repeat_enabled: bool = False
    repeat_interval_minutes: int | None = None
    scheduled_at: datetime | None = None
    stage_timings: list[StageTimingRead] = Field(default_factory=list)
    # T62 (2026-05-15): surface budget + DAG + parent linkage so the UI
    # TaskCard can render every value the watchdog uses without opening
    # the detail drawer.
    max_seconds_budget: int | None = None
    max_tokens_budget: int | None = None
    tokens_used: int = 0
    budget_exhausted_at: datetime | None = None
    parent_task_id: UUID | None = None
    split_count: int = 0
    split_position: int | None = None
    split_type: str | None = None
    dag_done_count: int | None = None
    # Observed work, not an alternative input/status or fresh execution authority.
    work_state: Literal["running", "reviewing", "waiting"] | None = None
    work_detail: str | None = None
    work_started_at: datetime | None = None
    work_expires_at: datetime | None = None


class TaskCommentCreate(SQLModel):
    """Payload for creating a task comment."""

    message: NonEmptyStr


class TaskCommentRead(SQLModel):
    """Task comment payload returned from read endpoints."""

    id: UUID
    message: str | None
    agent_id: UUID | None
    task_id: UUID | None
    created_at: datetime


class StageTimingRead(SQLModel):
    """Per-stage timing record for a task."""

    id: UUID
    status: str
    agent_id: UUID | None
    agent_name: str | None = None
    entered_at: datetime
    exited_at: datetime | None
    duration_seconds: int | None


# Resolve forward references — StageTimingRead is defined after TaskRead
# but TaskRead references it via list[StageTimingRead].
TaskRead.model_rebuild()
