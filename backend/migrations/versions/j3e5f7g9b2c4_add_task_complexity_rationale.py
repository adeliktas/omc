"""T83 — add tasks.complexity_rationale (JSON-as-text)

Revision ID: j3e5f7g9b2c4
Revises: i2d4e6f8a1b3
Create Date: 2026-05-16
"""
from alembic import op
import sqlalchemy as sa


revision = "j3e5f7g9b2c4"
down_revision = "i2d4e6f8a1b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("complexity_rationale", sa.Text, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tasks", "complexity_rationale")
