"""Task execution lane, workspace, and attachments.

Three gaps blocked task pipelining:

- Which lane owns a task was an undocumented title prefix (`[agent]`) read
  from a cron env var, invisible to the UI. A task created in the UI silently
  went to the gateway-agent lane whether or not that lane could do the work.
- A task could not name the folder it operates on, so the coding-harness
  runner always ran in one fixed checkout.
- There was no way to attach a file (an image to analyse, a spec to read) to
  a task at all.

Revision ID: s3c4d5e6f7a8
Revises: r2b3c4d5e6f7
"""

from alembic import op
import sqlalchemy as sa

revision = "s3c4d5e6f7a8"
down_revision = "r2b3c4d5e6f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("execution_lane", sa.String(), nullable=True))
    op.add_column("tasks", sa.Column("workspace_path", sa.String(), nullable=True))
    op.create_index("ix_tasks_execution_lane", "tasks", ["execution_lane"])

    # Backfill from the title-prefix convention the runner used, so existing
    # boards keep working without anyone relabelling their tasks.
    op.execute(
        r"""
        UPDATE tasks
        SET execution_lane = 'harness'
        WHERE title ~ '^\[(agent|claude|codex)\]'
        """
    )

    op.create_table(
        "task_attachments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("task_id", sa.Uuid(), nullable=False, index=True),
        sa.Column("board_id", sa.Uuid(), nullable=False),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("content_type", sa.String(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("storage_path", sa.String(), nullable=False),
        sa.Column("sha256", sa.String(), nullable=False),
        sa.Column("created_by_type", sa.String(), nullable=True),
        sa.Column("created_by_name", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("NOW()")),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    op.drop_table("task_attachments")
    op.drop_index("ix_tasks_execution_lane", table_name="tasks")
    op.drop_column("tasks", "workspace_path")
    op.drop_column("tasks", "execution_lane")
