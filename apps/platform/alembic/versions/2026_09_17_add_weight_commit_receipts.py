"""Persist exact submitted-artifact commit provenance and collector progress.

Revision ID: b63d4c1f820a
Revises: a54c2b0e719f
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "b63d4c1f820a"
down_revision = "a54c2b0e719f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "validator_weight_requests",
        sa.Column("validator_hotkey", sa.Text(), primary_key=True),
        sa.Column("request_id", sa.Uuid(), primary_key=True),
        sa.Column("netuid", sa.Integer(), nullable=False),
        sa.Column("request_digest", sa.Text(), nullable=False),
        sa.Column("request", postgresql.JSONB(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "validator_weight_receipts",
        sa.Column("validator_hotkey", sa.Text(), primary_key=True),
        sa.Column("request_id", sa.Uuid(), primary_key=True),
        sa.Column("attempt_id", sa.Uuid(), primary_key=True),
        sa.Column("netuid", sa.Integer(), nullable=False),
        sa.Column("receipt_digest", sa.Text(), nullable=False, unique=True),
        sa.Column("ciphertext_hash", sa.Text(), nullable=False),
        sa.Column("receipt", postgresql.JSONB(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("signed_at", sa.BigInteger(), nullable=False),
        sa.Column("signature", sa.Text(), nullable=False),
    )
    op.create_index(
        "validator_weight_receipts_ciphertext_idx",
        "validator_weight_receipts",
        ["netuid", "validator_hotkey", "ciphertext_hash"],
    )
    op.create_table(
        "source_emission_collector_cursors",
        sa.Column("netuid", sa.Integer(), primary_key=True),
        sa.Column("block", sa.BigInteger(), nullable=False),
        sa.Column("block_hash", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("runtime_code_hash", sa.Text(), nullable=True),
        sa.Column("last_blocked_reason", sa.Text(), nullable=True),
    )
    op.create_table(
        "source_emission_vector_bindings",
        sa.Column("netuid", sa.Integer(), primary_key=True),
        sa.Column("validator_hotkey", sa.Text(), primary_key=True),
        sa.Column("receipt_digest", sa.Text(), nullable=True),
        sa.Column("block", sa.BigInteger(), nullable=False),
        sa.Column("reveal_block_hash", sa.Text(), nullable=False),
        sa.Column("vector_digest", sa.Text(), nullable=True),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
    )
    op.create_table(
        "source_emission_payouts",
        sa.Column("netuid", sa.Integer(), primary_key=True),
        sa.Column("block_hash", sa.Text(), primary_key=True),
        sa.Column("block", sa.BigInteger(), nullable=False),
        sa.Column("proof", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.add_column(
        "source_emission_payouts",
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "source_emission_payouts",
        sa.Column("terminal", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "source_emission_payouts", sa.Column("blocked_reason", sa.Text(), nullable=True)
    )
    op.create_table(
        "source_emission_payout_resolutions",
        sa.Column("netuid", sa.Integer(), primary_key=True),
        sa.Column("block_hash", sa.Text(), primary_key=True),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_sha256", sa.Text(), nullable=False),
        sa.Column("proof", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("source_emission_payout_resolutions")
    for name in (
        "source_emission_payouts",
        "source_emission_vector_bindings",
        "source_emission_collector_cursors",
        "validator_weight_receipts",
        "validator_weight_requests",
    ):
        op.drop_table(name)
