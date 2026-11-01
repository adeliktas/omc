"""P1 (2026-09-01) — add nullable `tier` column to provider_capacity.

Revision ID: r2b3c4d5e6f7
Revises: q1a2b3c4d5e6
Create Date: 2026-09-01

GRAND-PLAN P1 (multi-provider scaffolding, owner directive "keep vllm-only for
now"). Adds a provider-level `tier` label so difficulty→tier→provider-pool
routing can be built (P2) and combo ordering can prefer a tier band. Column is
NULLABLE with NO server_default → every existing row stays NULL = DORMANT, so
combo ordering + routing behaviour is unchanged until an owner sets tiers.
"""
from alembic import op
import sqlalchemy as sa


revision = "r2b3c4d5e6f7"
down_revision = "q1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "provider_capacity",
        sa.Column("tier", sa.String(length=32), nullable=True),
    )
    op.create_index("ix_provider_capacity_tier", "provider_capacity", ["tier"])


def downgrade() -> None:
    op.drop_index("ix_provider_capacity_tier", table_name="provider_capacity")
    op.drop_column("provider_capacity", "tier")
