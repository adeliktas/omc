"""Add task_stage_timings table for per-stage duration tracking.

Revision ID: f9a2b1c4d5e7
Revises: d056a6dee823
Create Date: 2026-05-10 18:40:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f9a2b1c4d5e7"
down_revision = "d056a6dee823"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create task_stage_timings table."""
    op.create_table(
        "task_stage_timings",
        sa.Column("id", sa.Uuid(), nullable=False, primary_key=True),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), sa.ForeignKey("agents.id"), nullable=True),
        sa.Column("entered_at", sa.DateTime(), nullable=False),
        sa.Column("exited_at", sa.DateTime(), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_task_stage_timings_task_id", "task_stage_timings", ["task_id"])
    op.create_index("ix_task_stage_timings_status", "task_stage_timings", ["status"])


def downgrade() -> None:
    """Drop task_stage_timings table."""
    op.drop_index("ix_task_stage_timings_status", table_name="task_stage_timings")
    op.drop_index("ix_task_stage_timings_task_id", table_name="task_stage_timings")
    op.drop_table("task_stage_timings")
