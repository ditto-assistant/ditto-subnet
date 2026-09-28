"""Add shadow-first, auditable submission attempt controls.

Revision ID: 9ec438fa12b7
Revises: b6f3d0c7a915
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "9ec438fa12b7"
down_revision: str | Sequence[str] | None = "b6f3d0c7a915"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    timestamp = sa.TIMESTAMP(timezone=True)
    uuid = postgresql.UUID(as_uuid=True)
    # This small, bounded reservation table is not a hot inference/ticket table.
    op.add_column(
        "upload_admission_reservations",
        sa.Column("attempt_context", postgresql.JSONB(), nullable=True),
    )
    op.create_table(
        "submission_attempts",
        sa.Column("agent_id", uuid, primary_key=True),
        sa.Column("lineage_agent_id", uuid, nullable=False),
        sa.Column("reference_agent_id", uuid, nullable=True),
        sa.Column("profile", postgresql.JSONB(), nullable=False),
        sa.Column("guidance", postgresql.JSONB(), nullable=False),
        sa.Column("runtime_hash", sa.Text(), nullable=False),
        sa.Column("classification", sa.Text(), nullable=False),
        sa.Column("fast_repair", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", timestamp, nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.agent_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["lineage_agent_id"], ["agents.agent_id"]),
        sa.ForeignKeyConstraint(["reference_agent_id"], ["agents.agent_id"]),
        sa.CheckConstraint(
            "length(runtime_hash) = 64",
            name=op.f("ck_submission_attempts_runtime_hash"),
        ),
    )
    op.create_index(
        "submission_attempts_runtime_idx", "submission_attempts", ["runtime_hash"]
    )
    op.create_index(
        "submission_attempts_lineage_idx",
        "submission_attempts",
        ["lineage_agent_id", "created_at"],
    )
    op.create_table(
        "submission_attempt_settings_revisions",
        sa.Column("revision", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("parent_revision", sa.Integer(), nullable=False, unique=True),
        sa.Column("settings", postgresql.JSONB(), nullable=False),
        sa.Column("calibration_id", uuid, nullable=True),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at", timestamp, nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "submission_attempt_calibrations",
        sa.Column("calibration_id", uuid, primary_key=True),
        sa.Column("settings_digest", sa.Text(), nullable=False),
        sa.Column("report", postgresql.JSONB(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at", timestamp, nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "submission_attempt_appeals",
        sa.Column("appeal_id", uuid, primary_key=True),
        sa.Column("agent_id", uuid, nullable=False),
        sa.Column("policy_revision", sa.Integer(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at", timestamp, nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.agent_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("agent_id", "policy_revision"),
    )


def downgrade() -> None:
    op.drop_table("submission_attempt_appeals")
    op.drop_table("submission_attempt_calibrations")
    op.drop_table("submission_attempt_settings_revisions")
    op.drop_table("submission_attempts")
    op.drop_column("upload_admission_reservations", "attempt_context")
