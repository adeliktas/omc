"""add task split fields for parallel execution

Revision ID: d1e2f3a4b5c6
Revises: c0d1e2f3a4b5
Create Date: 2026-05-13 02:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "d1e2f3a4b5c6"
down_revision: Union[str, None] = "c0d1e2f3a4b5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("parent_task_id", sa.UUID(), nullable=True))
    op.add_column("tasks", sa.Column("split_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("tasks", sa.Column("split_position", sa.Integer(), nullable=True))
    op.add_column("tasks", sa.Column("split_type", sa.String(), nullable=True))
    op.create_index("ix_tasks_parent_task_id", "tasks", ["parent_task_id"])


def downgrade() -> None:
    op.drop_index("ix_tasks_parent_task_id")
    op.drop_column("tasks", "split_type")
    op.drop_column("tasks", "split_position")
    op.drop_column("tasks", "split_count")
    op.drop_column("tasks", "parent_task_id")
