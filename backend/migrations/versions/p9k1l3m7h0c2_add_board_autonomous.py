"""T105 Phase 5 — per-board autonomous toggle.

Revision ID: p9k1l3m7h0c2
Revises: o8j0k2l6g9b1
Create Date: 2026-05-25

Adds `boards.autonomous_enabled` (bool, default false). When true:
  - Newly created tasks auto-start PEV (planner-executor-verifier)
  - Watchdog step `_advance_pev_tasks` picks up tasks ONLY from boards
    where this flag is true
  - Owner can toggle per-board to enable domain-specific autonomy
    (e.g. legal-research board → law-change tracking + plan updates).
"""
from alembic import op
import sqlalchemy as sa


revision = "p9k1l3m7h0c2"
down_revision = "o8j0k2l6g9b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "boards",
        sa.Column("autonomous_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index(
        "ix_boards_autonomous_enabled",
        "boards",
        ["autonomous_enabled"],
        postgresql_where=sa.text("autonomous_enabled = true"),
    )


def downgrade() -> None:
    op.drop_index("ix_boards_autonomous_enabled", table_name="boards")
    op.drop_column("boards", "autonomous_enabled")
