"""Add public append-only treasury receipt projection.

Revision ID: d18b0f5a72c9
Revises: b7e2a4d961c0
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d18b0f5a72c9"
down_revision: str | Sequence[str] | None = "b7e2a4d961c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "treasury_public_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("payment_id", sa.Text(), nullable=False),
        sa.Column("event_kind", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("event_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "recorded_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("policy_revision", sa.Integer(), nullable=False),
        sa.Column("burn_revision", sa.Text(), nullable=False),
        sa.Column("denominator", sa.Text(), nullable=False),
        sa.Column("allocation_bps", sa.Integer(), nullable=False),
        sa.Column("allocated_alpha_rao", sa.BigInteger(), nullable=False),
        sa.Column("route", sa.Text(), nullable=False),
        sa.Column("asset", sa.Text(), nullable=False),
        sa.Column("gross_amount_atomic", sa.BigInteger(), nullable=False),
        sa.Column("realized_amount_atomic", sa.BigInteger(), nullable=True),
        sa.Column("public_sender", sa.Text(), nullable=False),
        sa.Column("public_recipient", sa.Text(), nullable=False),
        sa.Column("block_hash", sa.Text(), nullable=False),
        sa.Column("extrinsic_index", sa.Integer(), nullable=False),
        sa.Column("event_index", sa.Integer(), nullable=False),
        sa.Column("actor_provenance", sa.Text(), nullable=False),
        sa.Column("verification_source", sa.Text(), nullable=False),
        sa.UniqueConstraint(
            "payment_id", "state", name="treasury_public_payment_state"
        ),
        sa.UniqueConstraint(
            "block_hash",
            "extrinsic_index",
            "event_index",
            "state",
            name="treasury_public_chain_event_state",
        ),
        sa.CheckConstraint(
            "event_kind IN ('gm_credit_purchase', 'maintenance_bounty')",
            name="treasury_public_kind",
        ),
        sa.CheckConstraint(
            "state IN ('chain_finalized', 'reconciled')", name="treasury_public_state"
        ),
        sa.CheckConstraint(
            "denominator IN ('miner_emission', 'released_miner_emission')",
            name="treasury_public_denominator",
        ),
        sa.CheckConstraint(
            "allocation_bps BETWEEN 0 AND 10000 AND allocated_alpha_rao >= 0",
            name="treasury_public_allocation",
        ),
        sa.CheckConstraint(
            "gross_amount_atomic >= 0 AND "
            "(realized_amount_atomic IS NULL OR realized_amount_atomic >= 0)",
            name="treasury_public_amounts",
        ),
        sa.CheckConstraint(
            "extrinsic_index >= 0 AND event_index >= 0", name="treasury_public_indexes"
        ),
        sa.CheckConstraint(
            "actor_provenance IN ('authenticated_operator', 'automated_planner')",
            name="treasury_public_actor_provenance",
        ),
        sa.CheckConstraint(
            "verification_source IN "
            "('finalized_chain_rpc', 'chain_and_provider_reconciliation')",
            name="treasury_public_verification_source",
        ),
    )
    op.create_index(
        "treasury_public_event_at_idx", "treasury_public_events", ["event_at", "id"]
    )
    op.execute(
        """CREATE FUNCTION reject_treasury_public_event_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
          RAISE EXCEPTION 'treasury public receipt history is append only';
        END $$"""
    )
    op.execute(
        """CREATE TRIGGER treasury_public_events_immutable
        BEFORE UPDATE OR DELETE ON treasury_public_events
        FOR EACH ROW EXECUTE FUNCTION reject_treasury_public_event_mutation()"""
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER treasury_public_events_immutable ON treasury_public_events"
    )
    op.execute("DROP FUNCTION reject_treasury_public_event_mutation()")
    op.drop_index("treasury_public_event_at_idx", table_name="treasury_public_events")
    op.drop_table("treasury_public_events")
