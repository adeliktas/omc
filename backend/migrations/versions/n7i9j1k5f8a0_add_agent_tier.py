"""T105 Phase 2 — add `tier` column to agents (planner/executor/verifier/mixed).

Revision ID: n7i9j1k5f8a0
Revises: m6h8i0j4e7f9 ... wait, that was reverted. Real prev head = l5g7h9i3d6e8.
Create Date: 2026-05-25

Per the autonomous-tier design: agents are routed to specific LLM models
based on their tier. Smart-tier (planner / verifier) gets deepseek-v4-pro
/ kimi / glm fallback chain; executor gets vllm / lmstudio. `mixed` keeps
the current per-agent `preferred_provider` behavior.
"""
from alembic import op
import sqlalchemy as sa


revision = "n7i9j1k5f8a0"
down_revision = "l5g7h9i3d6e8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("tier", sa.String(length=16), nullable=False, server_default="mixed"),
    )
    op.create_index("ix_agents_tier", "agents", ["tier"])


def downgrade() -> None:
    op.drop_index("ix_agents_tier", table_name="agents")
    op.drop_column("agents", "tier")
