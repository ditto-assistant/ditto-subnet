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
        sa.Column("finalized_event_id", sa.BigInteger(), nullable=True),
        sa.Column("event_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "recorded_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("policy_revision", sa.Integer(), nullable=False),
        sa.Column("burn_revision", sa.Integer(), nullable=False),
        sa.Column("burn_share_micros", sa.Integer(), nullable=False),
        sa.Column("denominator", sa.Text(), nullable=False),
        sa.Column("maintenance_bps", sa.Integer(), nullable=False),
        sa.Column("gm_bps", sa.Integer(), nullable=False),
        sa.Column("allocation_bps", sa.Integer(), nullable=False),
        sa.Column("allocated_alpha_rao", sa.BigInteger(), nullable=False),
        sa.Column("source_alpha_rao", sa.BigInteger(), nullable=False),
        sa.Column("route", sa.Text(), nullable=False),
        sa.Column("deposit_asset", sa.Text(), nullable=False),
        sa.Column("deposit_amount_atomic", sa.BigInteger(), nullable=False),
        sa.Column("credited_usd_micros", sa.BigInteger(), nullable=True),
        sa.Column("public_sender", sa.Text(), nullable=False),
        sa.Column("public_recipient", sa.Text(), nullable=False),
        sa.Column("block_hash", sa.Text(), nullable=False),
        sa.Column("extrinsic_index", sa.Integer(), nullable=False),
        sa.Column("event_index", sa.Integer(), nullable=False),
        sa.Column("actor_provenance", sa.Text(), nullable=False),
        sa.Column("actor_public_id", sa.Text(), nullable=False),
        sa.Column("verification_source", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["finalized_event_id"], ["treasury_public_events.id"]),
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
            "(event_kind = 'gm_token_deposit' AND state = 'chain_finalized' "
            "AND finalized_event_id IS NULL AND credited_usd_micros IS NULL) OR "
            "(event_kind = 'gm_credit_purchase' AND state = 'reconciled' "
            "AND finalized_event_id IS NOT NULL AND credited_usd_micros IS NOT NULL "
            "AND credited_usd_micros > 0) OR "
            "(event_kind = 'maintenance_bounty' AND state = 'chain_finalized' "
            "AND finalized_event_id IS NULL AND credited_usd_micros IS NULL)",
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
            "maintenance_bps BETWEEN 0 AND 10000 AND "
            "gm_bps BETWEEN 0 AND 10000 AND "
            "allocation_bps BETWEEN 0 AND 10000 AND "
            "allocated_alpha_rao >= 0 AND source_alpha_rao > 0 AND "
            "burn_revision >= 0 AND burn_share_micros BETWEEN 0 AND 1000000",
            name="treasury_public_allocation",
        ),
        sa.CheckConstraint(
            "(event_kind = 'maintenance_bounty' AND allocation_bps = maintenance_bps) "
            "OR (event_kind <> 'maintenance_bounty' AND allocation_bps = gm_bps)",
            name="treasury_public_purpose_allocation",
        ),
        sa.CheckConstraint(
            "deposit_amount_atomic > 0 AND "
            "deposit_asset IN ('TAO', 'SN28_ALPHA', 'SN118_ALPHA')",
            name="treasury_public_amounts",
        ),
        sa.CheckConstraint(
            "route IN ('alpha_to_tao', 'alpha_to_gm_alpha', "
            "'alpha_transfer', 'alpha_to_tao_bounty')",
            name="treasury_public_route",
        ),
        sa.CheckConstraint(
            "extrinsic_index >= 0 AND event_index >= 0", name="treasury_public_indexes"
        ),
        sa.CheckConstraint(
            "actor_provenance IN "
            "('treasury_signer', 'gm_reconciler', 'bounty_executor')",
            name="treasury_public_actor_provenance",
        ),
        sa.CheckConstraint(
            "length(actor_public_id) BETWEEN 3 AND 120",
            name="treasury_public_actor_id",
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
        """CREATE FUNCTION verify_treasury_public_reconciliation() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
          IF NEW.state = 'reconciled' THEN
            PERFORM 1 FROM treasury_public_events finalized
            WHERE finalized.id = NEW.finalized_event_id
              AND finalized.payment_id = NEW.payment_id
              AND finalized.event_kind = 'gm_token_deposit'
              AND finalized.state = 'chain_finalized'
              AND finalized.block_hash = NEW.block_hash
              AND finalized.extrinsic_index = NEW.extrinsic_index
              AND finalized.event_index = NEW.event_index
              AND finalized.policy_revision = NEW.policy_revision
              AND finalized.burn_revision = NEW.burn_revision
              AND finalized.allocation_bps = NEW.allocation_bps
              AND finalized.source_alpha_rao = NEW.source_alpha_rao
              AND finalized.deposit_asset = NEW.deposit_asset
              AND finalized.deposit_amount_atomic = NEW.deposit_amount_atomic
              AND finalized.public_sender = NEW.public_sender
              AND finalized.public_recipient = NEW.public_recipient
              AND finalized.event_at <= NEW.event_at;
            IF NOT FOUND THEN
              RAISE EXCEPTION
                'GM credit reconciliation lacks matching finalized deposit';
            END IF;
          END IF;
          RETURN NEW;
        END $$"""
    )
    op.execute(
        """CREATE TRIGGER treasury_public_reconciliation_verified
        BEFORE INSERT ON treasury_public_events
        FOR EACH ROW EXECUTE FUNCTION verify_treasury_public_reconciliation()"""
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
        "DROP TRIGGER treasury_public_reconciliation_verified ON treasury_public_events"
    )
    op.execute("DROP FUNCTION verify_treasury_public_reconciliation()")
    op.execute(
        "DROP TRIGGER treasury_public_events_immutable ON treasury_public_events"
    )
    op.execute("DROP FUNCTION reject_treasury_public_event_mutation()")
    op.drop_index("treasury_public_event_at_idx", table_name="treasury_public_events")
    op.drop_table("treasury_public_events")
