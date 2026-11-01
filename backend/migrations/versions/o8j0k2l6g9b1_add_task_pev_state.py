"""T105 Phase 3 — PEV (Planner-Executor-Verifier) loop state per task.

Revision ID: o8j0k2l6g9b1
Revises: n7i9j1k5f8a0
Create Date: 2026-05-25

One row per task with PEV enabled. Tracks the plan, step cursor, retry
counters, and the snapshot id captured at loop start (for rollback in
Phase 4).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "o8j0k2l6g9b1"
down_revision = "n7i9j1k5f8a0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_pev_state",
        sa.Column("task_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="planning"),
        # planning | executing | verifying | done | rolled_back | escalated
        sa.Column("plan", postgresql.JSONB(), nullable=False, server_default="[]"),
        # Plan = list[{id, description, verification, executor_hint?}]
        sa.Column("step_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("step_retries", sa.Integer(), nullable=False, server_default="0"),
        # Last verdict for the current step
        sa.Column("last_verdict_pass", sa.Boolean(), nullable=True),
        sa.Column("last_verdict_reason", sa.Text(), nullable=True),
        # Snapshot id captured before loop start (Phase 4 rollback target)
        sa.Column("pre_loop_snapshot_id", sa.String(length=128), nullable=True),
        # Stats
        sa.Column("planner_model", sa.String(length=128), nullable=True),
        sa.Column("executor_model", sa.String(length=128), nullable=True),
        sa.Column("verifier_model", sa.String(length=128), nullable=True),
        sa.Column("total_calls_planner", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_calls_executor", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_calls_verifier", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=False), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=False), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_task_pev_state_status", "task_pev_state", ["status"])


def downgrade() -> None:
    op.drop_index("ix_task_pev_state_status", table_name="task_pev_state")
    op.drop_table("task_pev_state")
