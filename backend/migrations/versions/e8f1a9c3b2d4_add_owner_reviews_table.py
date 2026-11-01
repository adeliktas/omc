"""add owner_reviews table (T18, 2026-05-14 — replaces OWNER_REVIEW.json)

Revision ID: e8f1a9c3b2d4
Revises: d1e2f3a4b5c6
Create Date: 2026-05-14
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "e8f1a9c3b2d4"
down_revision: Union[str, None] = "d1e2f3a4b5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "owner_reviews",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("type", sa.String(), nullable=False, server_default="other"),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("proposed_action", sa.Text(), nullable=True),
        sa.Column("submitted_by", sa.String(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewer_note", sa.Text(), nullable=True),
        sa.Column("escalated_to_board_id", sa.UUID(), nullable=True),
        sa.Column("task_id", sa.UUID(), nullable=True, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_index("ix_owner_reviews_status", "owner_reviews", ["status"])
    op.create_index("ix_owner_reviews_type", "owner_reviews", ["type"])
    op.create_index("ix_owner_reviews_submitted_by", "owner_reviews", ["submitted_by"])
    op.create_index("ix_owner_reviews_submitted_at", "owner_reviews", ["submitted_at"])


def downgrade() -> None:
    op.drop_index("ix_owner_reviews_submitted_at", table_name="owner_reviews")
    op.drop_index("ix_owner_reviews_submitted_by", table_name="owner_reviews")
    op.drop_index("ix_owner_reviews_type", table_name="owner_reviews")
    op.drop_index("ix_owner_reviews_status", table_name="owner_reviews")
    op.drop_table("owner_reviews")
