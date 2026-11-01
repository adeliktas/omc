"""Add scheduled_at and repeat_interval_minutes to tasks.

Revision ID: d056a6dee823
Revises: d4f8e2a1b3c5
Create Date: 2026-05-10 07:25:04

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d056a6dee823"
down_revision = "d4f8e2a1b3c5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("scheduled_at", sa.DateTime(), nullable=True))
    op.add_column(
        "tasks",
        sa.Column("repeat_interval_minutes", sa.Integer(), nullable=True),
    )
    op.create_index("ix_tasks_scheduled_at", "tasks", ["scheduled_at"])


def downgrade() -> None:
    op.drop_index("ix_tasks_scheduled_at", table_name="tasks")
    op.drop_column("tasks", "repeat_interval_minutes")
    op.drop_column("tasks", "scheduled_at")
