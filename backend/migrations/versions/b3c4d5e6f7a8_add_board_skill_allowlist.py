"""add board_skill_allowlist (T34, E3 skill marketplace per-board allowlist)

Revision ID: b3c4d5e6f7a8
Revises: g2b3c4d5e6f7
Create Date: 2026-05-14
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b3c4d5e6f7a8"
down_revision: Union[str, None] = "g2b3c4d5e6f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "board_skill_allowlist",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("board_id", sa.UUID(), nullable=False),
        sa.Column("skill_id", sa.UUID(), nullable=False),
        sa.Column("allowed", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.UniqueConstraint("board_id", "skill_id", name="uq_board_skill_allowlist_board_skill"),
    )
    op.create_index("ix_board_skill_allowlist_board_id", "board_skill_allowlist", ["board_id"])
    op.create_index("ix_board_skill_allowlist_skill_id", "board_skill_allowlist", ["skill_id"])


def downgrade() -> None:
    op.drop_index("ix_board_skill_allowlist_skill_id", table_name="board_skill_allowlist")
    op.drop_index("ix_board_skill_allowlist_board_id", table_name="board_skill_allowlist")
    op.drop_table("board_skill_allowlist")
