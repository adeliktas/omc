"""add agent_messages table (T25, E2 inter-agent message bus)

Revision ID: g2b3c4d5e6f7
Revises: f1a2b3c4d5e6
Create Date: 2026-05-14
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "g2b3c4d5e6f7"
down_revision: Union[str, None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_messages",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("from_agent_id", sa.UUID(), nullable=True, index=True),
        sa.Column("to_agent_id", sa.UUID(), nullable=True, index=True),
        sa.Column("board_id", sa.UUID(), nullable=True, index=True),
        sa.Column("task_id", sa.UUID(), nullable=True, index=True),
        sa.Column("message_type", sa.String(), nullable=False, index=True),
        sa.Column("payload", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(), nullable=False, server_default="unread", index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_agent_messages_to_status_created",
        "agent_messages",
        ["to_agent_id", "status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_messages_to_status_created", table_name="agent_messages")
    op.drop_table("agent_messages")
