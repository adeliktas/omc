"""add complexity, pm_lock, is_project_manager

Revision ID: d4f8e2a1b3c5
Revises: a9b1c2d3e4f7
Create Date: 2026-05-07 14:00:00.000000

Per STATE.md §13.A — owner-locked schema for ITIL-light task flow.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "d4f8e2a1b3c5"
down_revision = "a9b1c2d3e4f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("complexity", sa.SmallInteger(), nullable=True),
    )
    op.add_column(
        "tasks",
        sa.Column("pm_lock_agent_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "tasks",
        sa.Column("pm_lock_at", sa.DateTime(timezone=False), nullable=True),
    )
    op.create_foreign_key(
        "tasks_pm_lock_agent_id_fkey",
        "tasks",
        "agents",
        ["pm_lock_agent_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_tasks_pm_lock",
        "tasks",
        ["pm_lock_agent_id", "pm_lock_at"],
        unique=False,
        postgresql_where=sa.text("pm_lock_agent_id IS NOT NULL"),
    )

    op.add_column(
        "agents",
        sa.Column(
            "is_project_manager",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_index(
        "ix_agents_is_project_manager",
        "agents",
        ["is_project_manager"],
        unique=False,
        postgresql_where=sa.text("is_project_manager = true"),
    )


def downgrade() -> None:
    op.drop_index("ix_agents_is_project_manager", table_name="agents")
    op.drop_column("agents", "is_project_manager")
    op.drop_index("ix_tasks_pm_lock", table_name="tasks")
    op.drop_constraint("tasks_pm_lock_agent_id_fkey", "tasks", type_="foreignkey")
    op.drop_column("tasks", "pm_lock_at")
    op.drop_column("tasks", "pm_lock_agent_id")
    op.drop_column("tasks", "complexity")
