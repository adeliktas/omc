"""T88 — chat tab: add `backend` column to chat_sessions

Revision ID: l5g7h9i3d6e8
Revises: k4f6g8h2c5d7
Create Date: 2026-05-16
"""
from alembic import op
import sqlalchemy as sa


revision = "l5g7h9i3d6e8"
down_revision = "k4f6g8h2c5d7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_sessions",
        sa.Column("backend", sa.String(length=32), nullable=False, server_default="direct"),
    )


def downgrade() -> None:
    op.drop_column("chat_sessions", "backend")
