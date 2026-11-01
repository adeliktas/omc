"""Durable team execution: bounded roles, cycles and fenced worker attempts.

A cycle is one planned->developed->QA'd->published pass over a team-mode task.
The coordinator is deterministic host orchestration (app.services.team_cycle):
it has a member row so its identity is visible, but no attempt can ever be
created for it (CHECK below), so it cannot implement or approve QA.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, Index, UniqueConstraint, text
from sqlmodel import Field

from app.core.time import utcnow
from app.models.tenancy import TenantScoped

RUNTIME_ANNOTATION_TYPES = (datetime,)

ROLE_KINDS = ("coordinator", "pm", "developer", "specialist", "qa")
WORKER_KINDS = ("pm", "developer", "specialist", "qa")
ACTIVE_CYCLE_STATES = ("planning", "developing", "qa", "qa_passed", "publishing")


class TeamMember(TenantScoped, table=True):
    """A bounded, board-scoped worker profile with an explicit validated engine."""

    __tablename__ = "team_members"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint("board_id", "name", name="uq_team_member_board_name"),
        CheckConstraint(
            "role_kind IN ('coordinator','pm','developer','specialist','qa')",
            name="ck_team_member_kind",
        ),
        CheckConstraint(
            "(role_kind = 'coordinator' AND provider IS NULL AND model IS NULL)"
            " OR (role_kind <> 'coordinator' AND provider IS NOT NULL AND model IS NOT NULL)",
            name="ck_team_member_engine",
        ),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    board_id: UUID = Field(foreign_key="boards.id", index=True)
    name: str
    role_kind: str
    # Role-template slug the worker prompt is drawn from (e.g. qa-engineer).
    role_slug: str | None = None
    # Derived from role_kind by the server, never accepted from callers.
    capability: str = "read_only"
    provider: str | None = None
    model: str | None = None
    thinking_level: str = "low"
    enabled: bool = True
    created_by: str = "coordinator"
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class TeamCycle(TenantScoped, table=True):
    """One durable pass of the team over a task; `version` fences every transition."""

    __tablename__ = "team_cycles"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        Index(
            "uq_team_cycle_active_task",
            "task_id",
            unique=True,
            postgresql_where=text(
                "state IN ('planning','developing','qa','qa_passed','publishing')"
            ),
            sqlite_where=text("state IN ('planning','developing','qa','qa_passed','publishing')"),
        ),
        Index(
            "uq_team_publication",
            "state",
            unique=True,
            postgresql_where=text("state = 'publishing'"),
            sqlite_where=text("state = 'publishing'"),
        ),
        CheckConstraint(
            "round >= 1 AND max_rounds >= 1 AND round <= max_rounds",
            name="ck_cycle_round",
        ),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    board_id: UUID = Field(foreign_key="boards.id", index=True)
    task_id: UUID = Field(foreign_key="tasks.id", index=True)
    # planning|developing|qa|qa_passed|publishing|published|blocked|cancelled
    # 'delegated' = truthful terminal state for [backlog] manager tasks: the
    # PM pass decomposed the task into board children; no worker/QA/publish
    # attempt may ever run on such a cycle (not in ACTIVE_CYCLE_STATES).
    state: str = "planning"
    version: int = 1
    round: int = 1
    max_rounds: int = 2
    plan: str | None = None
    task_fingerprint: str | None = None
    base_sha: str | None = None
    integration_sha: str | None = None
    reviewed_sha: str | None = None
    published_sha: str | None = None
    qa_feedback: str | None = None
    block_reason: str | None = None
    deadline_at: datetime
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class TeamAttempt(TenantScoped, table=True):
    """One fenced worker run. A claim token is only valid while status='running'."""

    __tablename__ = "team_attempts"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint(
            "cycle_id", "stage_key", "round", "attempt_no", name="uq_team_attempt_slot"
        ),
        CheckConstraint(
            "role_kind IN ('pm','developer','specialist','qa')",
            name="ck_team_attempt_not_coordinator",
        ),
        CheckConstraint(
            "status IN ('queued','running','succeeded','failed','cancelled','expired')",
            name="ck_team_attempt_status",
        ),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    cycle_id: UUID = Field(foreign_key="team_cycles.id", index=True)
    board_id: UUID = Field(foreign_key="boards.id", index=True)
    task_id: UUID = Field(foreign_key="tasks.id", index=True)
    member_id: UUID = Field(foreign_key="team_members.id", index=True)
    role_kind: str
    stage_key: str
    round: int = 1
    attempt_no: int = 1
    status: str = "queued"
    # Engine snapshot taken at assignment so later edits cannot rewrite history.
    provider: str
    model: str
    thinking_level: str = "low"
    # Specialist/developer assignment from the PM plan (JSON: title, scope, instructions).
    assignment: str | None = None
    claim_hash: str | None = None
    claim_count: int = 0
    lease_expires_at: datetime | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    changed_files: str | None = None
    artifact: str | None = None
    verdict: str | None = None
    error: str | None = None
    # Observed usage: NULL means "not reported", never 0. cli_cost_estimate_usd is the
    # CLI's own estimate, explicitly NOT a billed amount.
    usage_reported: bool = False
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    total_tokens: int | None = None
    cli_cost_estimate_usd: float | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
