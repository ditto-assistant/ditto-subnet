"""Record which validators published each generator round's commitment.

Platform no longer publishes round seed commitments from its own Pylon
identity. It elects a few validators per committed round to publish
``dgr1:{round}:{commitment}`` with ``Commitments.set_commitment`` and records
each claim and report here, so an operator can see who published the
commitment and at which block.

Revision ID: c3f8a1d5e7b9
Revises: b7e4c2a9d1f3
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c3f8a1d5e7b9"
down_revision: str | Sequence[str] | None = "b7e4c2a9d1f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _tz() -> sa.types.TypeEngine:
    return sa.TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.create_table(
        "generator_round_commitment_publications",
        sa.Column("round_index", sa.BigInteger(), primary_key=True),
        sa.Column("validator_hotkey", sa.Text(), primary_key=True),
        sa.Column("commitment", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("claimed_at", _tz(), nullable=False),
        sa.Column("claim_expires_at", _tz(), nullable=False),
        sa.Column("reported_at", _tz()),
        sa.Column("block_number", sa.BigInteger()),
        sa.Column("extrinsic_hash", sa.Text()),
        sa.Column("detail", sa.Text()),
        sa.ForeignKeyConstraint(
            ["round_index"], ["generator_rounds.round_index"], ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "status IN ('claimed', 'confirmed', 'failed')",
            name="publication_status_check",
        ),
        sa.CheckConstraint(
            "status <> 'confirmed' OR block_number IS NOT NULL",
            name="publication_block_check",
        ),
    )


def downgrade() -> None:
    op.drop_table("generator_round_commitment_publications")
