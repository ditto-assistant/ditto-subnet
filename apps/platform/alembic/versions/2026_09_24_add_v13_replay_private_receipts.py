"""Append authenticated replay-private execution reports without policy authority.

Revision ID: d7b4f150ae2c
Revises: cfa2be70013d
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d7b4f150ae2c"
down_revision: str | Sequence[str] | None = "cfa2be70013d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "screening_verification_replay_private_receipts",
        sa.Column("replay_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("receipt_sha256", sa.Text(), nullable=False),
        sa.Column("runner_hotkey", sa.Text(), nullable=False),
        sa.Column("observed_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("signature", sa.Text(), nullable=False),
        sa.Column("report", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["replay_id"],
            ["screening_verification_replays.replay_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["v13_replay_private_generation_groups.group_id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("length(receipt_sha256) = 64", name="svrpr_receipt_sha"),
        sa.CheckConstraint(
            "length(runner_hotkey) BETWEEN 1 AND 120", name="svrpr_runner"
        ),
        sa.CheckConstraint("length(signature) = 128", name="svrpr_signature"),
    )
    op.execute(
        "CREATE TRIGGER screening_verification_replay_private_receipts_immutable "
        "BEFORE UPDATE OR DELETE ON screening_verification_replay_private_receipts "
        "FOR EACH ROW EXECUTE FUNCTION reject_v13_private_generation_mutation()"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER screening_verification_replay_private_receipts_immutable "
        "ON screening_verification_replay_private_receipts"
    )
    op.drop_table("screening_verification_replay_private_receipts")
