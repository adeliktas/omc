"""Consolidated migration: add all missing columns (idempotent — skips existing).

Adds columns that exist in SQLModel definitions but were never created
by prior alembic migrations. Uses IF NOT EXISTS to handle dev-mode
databases where SQLModel.create_all() already created them.

Revision ID: b8c9d0e1f2a3
Revises: a7b2c3d4e5f6
"""

from __future__ import annotations

from alembic import op

revision = "b8c9d0e1f2a3"
down_revision = "a7b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add missing columns (skip if exists)."""
    # tasks columns
    for col, ddl in [
        ("created_by_type", "ADD COLUMN IF NOT EXISTS created_by_type VARCHAR"),
        ("created_by_name", "ADD COLUMN IF NOT EXISTS created_by_name VARCHAR"),
        ("repeat_cron", "ADD COLUMN IF NOT EXISTS repeat_cron VARCHAR"),
        ("repeat_enabled", "ADD COLUMN IF NOT EXISTS repeat_enabled BOOLEAN NOT NULL DEFAULT false"),
    ]:
        op.execute(f"ALTER TABLE tasks {ddl}")

    # boards columns
    for col, ddl in [
        ("autonomous_tasks_enabled", "ADD COLUMN IF NOT EXISTS autonomous_tasks_enabled BOOLEAN NOT NULL DEFAULT true"),
        ("cron_tasks_enabled", "ADD COLUMN IF NOT EXISTS cron_tasks_enabled BOOLEAN NOT NULL DEFAULT true"),
        ("cloud_providers_enabled", "ADD COLUMN IF NOT EXISTS cloud_providers_enabled BOOLEAN NOT NULL DEFAULT true"),
    ]:
        op.execute(f"ALTER TABLE boards {ddl}")

    # provider_capacity table (create if not exists)
    op.execute("""
        CREATE TABLE IF NOT EXISTS provider_capacity (
            id UUID PRIMARY KEY,
            provider_name VARCHAR NOT NULL,
            max_concurrent INTEGER NOT NULL DEFAULT 1,
            current_usage INTEGER NOT NULL DEFAULT 0,
            is_unlimited BOOLEAN NOT NULL DEFAULT false,
            base_url VARCHAR,
            api_key VARCHAR,
            status VARCHAR NOT NULL DEFAULT 'offline',
            last_checked_at TIMESTAMP,
            quota_reset_at TIMESTAMP,
            created_at TIMESTAMP NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS ix_provider_capacity_provider_name
        ON provider_capacity (provider_name)
    """)


def downgrade() -> None:
    """Remove added columns/table."""
    op.drop_index("ix_provider_capacity_provider_name", table_name="provider_capacity")
    op.drop_table("provider_capacity")
    for tbl, cols in [
        ("boards", ["cloud_providers_enabled", "cron_tasks_enabled", "autonomous_tasks_enabled"]),
        ("tasks", ["repeat_enabled", "repeat_cron", "created_by_name", "created_by_type"]),
    ]:
        for col in cols:
            op.drop_column(tbl, col)
