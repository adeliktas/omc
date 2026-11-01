"""add owner_reviews.snooze_until (T56, 2026-05-15)

Revision ID: h1c2d3e4f5a6
Revises: b3c4d5e6f7a8
Create Date: 2026-05-15
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "h1c2d3e4f5a6"
down_revision: Union[str, None] = "b3c4d5e6f7a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "owner_reviews",
        sa.Column("snooze_until", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("owner_reviews", "snooze_until")
