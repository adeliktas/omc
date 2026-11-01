"""add provider_type to provider_capacity

Revision ID: c0d1e2f3a4b5
Revises: b8c9d0e1f2a3
Create Date: 2026-05-13 01:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c0d1e2f3a4b5"
down_revision: Union[str, None] = "b8c9d0e1f2a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "provider_capacity",
        sa.Column("provider_type", sa.String(), nullable=False, server_default="openai-compatible"),
    )


def downgrade() -> None:
    op.drop_column("provider_capacity", "provider_type")
