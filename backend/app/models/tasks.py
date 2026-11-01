"""Task model representing board work items and execution metadata."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Index, text
from sqlmodel import Field

from app.core.time import utcnow
from app.models.tenancy import TenantScoped

RUNTIME_ANNOTATION_TYPES = (datetime,)


class Task(TenantScoped, table=True):
    """Board-scoped task entity with ownership, status, and timing fields."""

    __tablename__ = "tasks"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        Index(
            "uq_board_chat_session",
            "board_id",
            "created_by_name",
            unique=True,
            postgresql_where=text(
                "created_by_type = 'board-chat' AND created_by_name IS NOT NULL"
            ),
            sqlite_where=text(
                "created_by_type = 'board-chat' AND created_by_name IS NOT NULL"
            ),
        ),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    board_id: UUID | None = Field(default=None, foreign_key="boards.id", index=True)

    title: str
    description: str | None = None
    status: str = Field(default="inbox", index=True)
    # Owner pause retains the safe resume destination, not a new attempt budget.
    paused_from_status: str | None = None
    priority: str = Field(default="medium", index=True)
    due_at: datetime | None = None
    in_progress_at: datetime | None = None
    previous_in_progress_at: datetime | None = None

    created_by_user_id: UUID | None = Field(
        default=None,
        foreign_key="users.id",
        index=True,
    )
    assigned_agent_id: UUID | None = Field(
        default=None,
        foreign_key="agents.id",
        index=True,
    )
    auto_created: bool = Field(default=False)
    auto_reason: str | None = None
    created_by_type: str | None = None
    created_by_name: str | None = None

    # Owner-locked ITIL-light fields (STATE.md §3 invariant 2 + §13.A).
    # complexity 1..5 = L1..L5; PM mutex prevents concurrent PMs from picking the
    # same task. Lock TTL = 300 s; expired locks treated as released.
    complexity: int | None = Field(default=None, index=True)
    # T83 (2026-05-16): JSON-encoded breakdown of how the assessor arrived
    # at this complexity — LLM verdict + reasoning, keyword floor, factor
    # contributions. Populated at task creation by complexity_assessor.
    # Read-back by the `/router/complexity_rationale` endpoint without
    # re-running the (non-deterministic) LLM call.
    complexity_rationale: str | None = None
    pm_lock_agent_id: UUID | None = Field(
        default=None,
        foreign_key="agents.id",
        index=True,
    )
    pm_lock_at: datetime | None = None

    # ── Scheduling ──────────────────────────────────────────────────────────
    # scheduled_at: run this task (or its clone) at the specified datetime.
    # Watchdog picks up inbox tasks whose scheduled_at has passed and routes them.
    scheduled_at: datetime | None = Field(default=None)
    # repeat_interval_minutes: auto-clone task N minutes after it's marked done.
    # Simpler than cron — use for "every 30m", "every 1d", "every 2w" etc.
    repeat_interval_minutes: int | None = Field(default=None)
    # repeat_cron (existing via migration d4f8e2a1b3c5): crontab expression for
    # recurring tasks. Processed separately from repeat_interval_minutes.
    repeat_cron: str | None = Field(default=None)
    repeat_enabled: bool = Field(default=False)

    # ── Task splitting / parallelization ────────────────────────────────────
    # parent_task_id: points to the parent task that was split into children.
    # NULL for normal tasks and for parent-of-split tasks themselves.
    parent_task_id: UUID | None = Field(
        default=None,
        foreign_key="tasks.id",
        index=True,
    )
    # split_count: number of children (set on parent after split operation).
    split_count: int = Field(default=0)
    # split_position: child index 0..N-1; NULL for parents and non-split tasks.
    split_position: int | None = Field(default=None)
    # split_type: how the split was performed ("by_count" or "by_sections").
    split_type: str | None = Field(default=None)

    # ── Per-task budget + auto-failover (T22 / E4, 2026-05-14) ──────────────
    # max_seconds_budget: hard wall-clock cap for in_progress duration. Watchdog
    # kicks the task back to inbox + drops assigned_agent_id when exceeded so
    # the role pool can rotate it to a different agent (possibly different
    # provider via fallback chain). NULL = no time cap.
    max_seconds_budget: int | None = Field(default=None)
    # max_tokens_budget: reserved for future token-usage gating (Phase 2).
    max_tokens_budget: int | None = Field(default=None)
    # tokens_used: rolling counter; populated by traffic ingest in Phase 2.
    tokens_used: int = Field(default=0)
    # budget_exhausted_at: timestamp when the watchdog last flagged this task
    # for budget exhaustion. Used by routing to avoid immediate rotation back
    # to the same agent.
    budget_exhausted_at: datetime | None = None

    # Which execution lane owns this task. "harness" = the coding-harness
    # runner (vendor CLIs, real file edits, its own checkout); "agent" = a
    # gateway agent. NULL means "decide by convention", which keeps older
    # boards working. Replaces an undocumented title prefix that lived in a
    # cron env var and that the UI had no way to set.
    execution_lane: str | None = Field(default=None, index=True)

    # Absolute folder this task operates on. The harness runner runs here
    # instead of its default checkout, so a task can point at, say, a website
    # directory. Ignored by the gateway-agent lane.
    workspace_path: str | None = Field(default=None)

    # Per-task harness engine. A pipeline step picks its own model and how
    # much it should think: image-to-HTML extraction and a careful HTML edit
    # want different engines, and most steps want a cheap model with thinking
    # off rather than whatever the ranking board picks for a generic task.
    # NULL falls back to the ranking board.
    harness_provider: str | None = Field(default=None)
    harness_model: str | None = Field(default=None)
    thinking_level: str | None = Field(default=None)

    # "single" = legacy one-agent run; "team" = native coordinated team cycle
    # (coordinator -> PM -> developers -> independent QA -> review). NOT NULL so
    # no row can bypass the mode check.
    execution_mode: str = Field(
        default="single", sa_column_kwargs={"server_default": "single"}
    )

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
