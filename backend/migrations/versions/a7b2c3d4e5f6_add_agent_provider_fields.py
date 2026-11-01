"""Add preferred_provider + preferred_model columns to agents table.

Revision ID: a7b2c3d4e5f6
Revises: f9a2b1c4d5e7
Create Date: 2026-05-11 01:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a7b2c3d4e5f6"
down_revision = "f9a2b1c4d5e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add provider/model columns to agents."""
    op.add_column(
        "agents",
        sa.Column("preferred_provider", sa.String(), nullable=True),
    )
    op.add_column(
        "agents",
        sa.Column("preferred_model", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_agents_preferred_provider",
        "agents",
        ["preferred_provider"],
    )


def downgrade() -> None:
    """Drop provider/model columns from agents."""
    op.drop_index("ix_agents_preferred_provider", table_name="agents")
    op.drop_column("agents", "preferred_model")
    op.drop_column("agents", "preferred_provider")
