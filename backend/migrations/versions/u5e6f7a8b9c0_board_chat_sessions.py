"""Uniquely bind each Pi session to one task on each board.

Revision ID: u5e6f7a8b9c0
Revises: t4d5e6f7a8b9
"""

from alembic import op
import sqlalchemy as sa

revision = "u5e6f7a8b9c0"
down_revision = "t4d5e6f7a8b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_board_chat_session", "tasks", ["board_id", "created_by_name"], unique=True,
        postgresql_where=sa.text("created_by_type = 'board-chat' AND created_by_name IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_board_chat_session", table_name="tasks")
