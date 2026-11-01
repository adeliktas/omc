"""provider_capacity.priority exists in the model but no migration created it.

Databases that gained the column by hand keep it; fresh installs get it here.
"""
from alembic import op

revision = "x8b9c0d1e2f3"
down_revision = "w7a8b9c0d1e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE provider_capacity ADD COLUMN IF NOT EXISTS priority INTEGER DEFAULT 0")


def downgrade() -> None:
    op.execute("ALTER TABLE provider_capacity DROP COLUMN IF EXISTS priority")
