"""Remember the owner-paused task's safe resume destination."""
from alembic import op
import sqlalchemy as sa

revision = "w7a8b9c0d1e2"
down_revision = "v6f7a8b9c0d1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("paused_from_status", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "paused_from_status")
