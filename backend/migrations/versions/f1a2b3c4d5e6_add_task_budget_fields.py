"""add task budget fields (T22, E4 wall-clock budget)

Revision ID: f1a2b3c4d5e6
Revises: e8f1a9c3b2d4
Create Date: 2026-05-14
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, None] = "e8f1a9c3b2d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("max_seconds_budget", sa.Integer(), nullable=True))
    op.add_column("tasks", sa.Column("max_tokens_budget", sa.Integer(), nullable=True))
    op.add_column("tasks", sa.Column("tokens_used", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("tasks", sa.Column("budget_exhausted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "budget_exhausted_at")
    op.drop_column("tasks", "tokens_used")
    op.drop_column("tasks", "max_tokens_budget")
    op.drop_column("tasks", "max_seconds_budget")
