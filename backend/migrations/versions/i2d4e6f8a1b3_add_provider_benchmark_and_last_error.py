"""T76 — add provider_benchmark table + provider_capacity.last_error column

Revision ID: i2d4e6f8a1b3
Revises: h1c2d3e4f5a6
Create Date: 2026-05-16
"""
from alembic import op
import sqlalchemy as sa


revision = "i2d4e6f8a1b3"
down_revision = "h1c2d3e4f5a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "provider_capacity",
        sa.Column("last_error", sa.String(length=512), nullable=True),
    )

    op.create_table(
        "provider_benchmark",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("provider_name", sa.String(length=64), nullable=False, index=True),
        sa.Column("model_id", sa.String(length=256), nullable=False, index=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("duration_ms", sa.Integer, nullable=False),
        sa.Column("prompt_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("total_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("tps", sa.Float, nullable=False, server_default="0"),
        sa.Column("ok", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("error", sa.String(length=512), nullable=True),
    )
    op.create_index(
        "ix_provider_benchmark_provider_model_ts",
        "provider_benchmark",
        ["provider_name", "model_id", "ts"],
    )


def downgrade() -> None:
    op.drop_index("ix_provider_benchmark_provider_model_ts", table_name="provider_benchmark")
    op.drop_table("provider_benchmark")
    op.drop_column("provider_capacity", "last_error")
