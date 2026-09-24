"""Store authenticated V13 replay observations separately from policy receipts.

Revision ID: 0e7a1c9b4d62
Revises: 9d8c2e4a7f10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0e7a1c9b4d62"
down_revision: str | Sequence[str] | None = "9d8c2e4a7f10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "screening_verification_replay_signed_observations",
        sa.Column("observation_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("replay_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("check_code", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("evidence_sha256", sa.Text(), nullable=False),
        sa.Column("runner_hotkey", sa.Text(), nullable=False),
        sa.Column("observed_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("signature", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["replay_id"],
            ["screening_verification_replays.replay_id"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "length(check_code) BETWEEN 1 AND 64", name="svrso_check_code"
        ),
        sa.CheckConstraint(
            "status IN ('passed', 'failed', 'inconclusive')", name="svrso_status"
        ),
        sa.CheckConstraint("length(evidence_sha256) = 64", name="svrso_evidence_sha"),
        sa.CheckConstraint(
            "length(runner_hotkey) BETWEEN 1 AND 120", name="svrso_runner"
        ),
        sa.CheckConstraint("length(signature) = 128", name="svrso_signature"),
    )
    op.create_index(
        "svrso_replay_check_idx",
        "screening_verification_replay_signed_observations",
        ["replay_id", "check_code"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "svrso_replay_check_idx",
        table_name="screening_verification_replay_signed_observations",
    )
    op.drop_table("screening_verification_replay_signed_observations")
