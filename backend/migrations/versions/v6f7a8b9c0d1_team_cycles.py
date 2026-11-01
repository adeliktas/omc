"""Native team execution: members, cycles, fenced attempts, task execution_mode.

Revision ID: v6f7a8b9c0d1
Revises: u5e6f7a8b9c0
"""

from alembic import op
import sqlalchemy as sa

revision = "v6f7a8b9c0d1"
down_revision = "u5e6f7a8b9c0"
branch_labels = None
depends_on = None

_ACTIVE = "state IN ('planning','developing','qa','qa_passed','publishing')"


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column(
            "execution_mode", sa.String(), nullable=False, server_default="single"
        ),
    )
    op.create_check_constraint(
        "ck_task_execution_mode", "tasks", "execution_mode IN ('single','team')"
    )
    op.create_table(
        "team_members",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "board_id",
            sa.Uuid(),
            sa.ForeignKey("boards.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("role_kind", sa.String(), nullable=False),
        sa.Column("role_slug", sa.String(), nullable=True),
        sa.Column(
            "capability", sa.String(), nullable=False, server_default="read_only"
        ),
        sa.Column("provider", sa.String(), nullable=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("thinking_level", sa.String(), nullable=False, server_default="low"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_by", sa.String(), nullable=False, server_default="coordinator"
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("board_id", "name", name="uq_team_member_board_name"),
        sa.CheckConstraint(
            "role_kind IN ('coordinator','pm','developer','specialist','qa')",
            name="ck_team_member_kind",
        ),
        sa.CheckConstraint(
            "(role_kind = 'coordinator' AND provider IS NULL AND model IS NULL)"
            " OR (role_kind <> 'coordinator' AND provider IS NOT NULL AND model IS NOT NULL)",
            name="ck_team_member_engine",
        ),
    )
    op.create_table(
        "team_cycles",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "board_id",
            sa.Uuid(),
            sa.ForeignKey("boards.id"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "task_id", sa.Uuid(), sa.ForeignKey("tasks.id"), nullable=False, index=True
        ),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False),
        sa.Column("max_rounds", sa.Integer(), nullable=False),
        sa.Column("plan", sa.Text(), nullable=True),
        sa.Column("task_fingerprint", sa.String(64), nullable=True),
        sa.Column("base_sha", sa.String(), nullable=True),
        sa.Column("integration_sha", sa.String(), nullable=True),
        sa.Column("reviewed_sha", sa.String(), nullable=True),
        sa.Column("published_sha", sa.String(), nullable=True),
        sa.Column("qa_feedback", sa.Text(), nullable=True),
        sa.Column("block_reason", sa.Text(), nullable=True),
        sa.Column("deadline_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "round >= 1 AND max_rounds >= 1 AND round <= max_rounds",
            name="ck_cycle_round",
        ),
    )
    op.create_index(
        "uq_team_cycle_active_task",
        "team_cycles",
        ["task_id"],
        unique=True,
        postgresql_where=sa.text(_ACTIVE),
    )
    op.create_index(
        "uq_team_publication",
        "team_cycles",
        ["state"],
        unique=True,
        postgresql_where=sa.text("state = 'publishing'"),
    )
    op.create_table(
        "team_attempts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "cycle_id",
            sa.Uuid(),
            sa.ForeignKey("team_cycles.id"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "board_id",
            sa.Uuid(),
            sa.ForeignKey("boards.id"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "task_id", sa.Uuid(), sa.ForeignKey("tasks.id"), nullable=False, index=True
        ),
        sa.Column(
            "member_id",
            sa.Uuid(),
            sa.ForeignKey("team_members.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("role_kind", sa.String(), nullable=False),
        sa.Column("stage_key", sa.String(), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("thinking_level", sa.String(), nullable=False),
        sa.Column("assignment", sa.Text(), nullable=True),
        sa.Column("claim_hash", sa.String(), nullable=True),
        sa.Column("claim_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("base_sha", sa.String(), nullable=True),
        sa.Column("head_sha", sa.String(), nullable=True),
        sa.Column("changed_files", sa.Text(), nullable=True),
        sa.Column("artifact", sa.Text(), nullable=True),
        sa.Column("verdict", sa.String(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "usage_reported", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("cli_cost_estimate_usd", sa.Float(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "cycle_id", "stage_key", "round", "attempt_no", name="uq_team_attempt_slot"
        ),
        sa.CheckConstraint(
            "role_kind IN ('pm','developer','specialist','qa')",
            name="ck_team_attempt_not_coordinator",
        ),
        sa.CheckConstraint(
            "status IN ('queued','running','succeeded','failed','cancelled','expired')",
            name="ck_team_attempt_status",
        ),
    )


def downgrade() -> None:
    op.drop_table("team_attempts")
    op.drop_index("uq_team_cycle_active_task", table_name="team_cycles")
    op.drop_table("team_cycles")
    op.drop_table("team_members")
    op.drop_constraint("ck_task_execution_mode", "tasks", type_="check")
    op.drop_column("tasks", "execution_mode")
