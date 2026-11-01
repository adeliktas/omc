"""Add per-agent tool_config column.

Revision ID: q1a2b3c4d5e6
Revises: p9k1l3m7h0c2
Create Date: 2026-08-14 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "q1a2b3c4d5e6"
down_revision = "p9k1l3m7h0c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add per-agent tool/skill override JSON column."""
    op.add_column("agents", sa.Column("tool_config", sa.JSON(), nullable=True))


def downgrade() -> None:
    """Remove per-agent tool/skill override JSON column."""
    op.drop_column("agents", "tool_config")
