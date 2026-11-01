"""Per-task harness selection: provider, model, thinking level.

A pipeline step should choose its own engine. Image-to-HTML extraction and a
careful HTML edit want different models and different amounts of reasoning,
and most steps want a cheap model with thinking off rather than whatever the
ranking board would pick for a generic task.

Revision ID: t4d5e6f7a8b9
Revises: s3c4d5e6f7a8
"""

from alembic import op
import sqlalchemy as sa

revision = "t4d5e6f7a8b9"
down_revision = "s3c4d5e6f7a8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("harness_provider", sa.String(), nullable=True))
    op.add_column("tasks", sa.Column("harness_model", sa.String(), nullable=True))
    op.add_column("tasks", sa.Column("thinking_level", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "thinking_level")
    op.drop_column("tasks", "harness_model")
    op.drop_column("tasks", "harness_provider")
