"""Private canonical receipt ingress and explicit service/payment public stages."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "b391f0e8c2d6"
down_revision = "9c41d7e2b6a3"
branch_labels = None
depends_on = None


def upgrade():
    for name in ("bucket_id", "policy_digest"):
        op.add_column(
            "treasury_public_events", sa.Column(name, sa.Text(), nullable=True)
        )
    op.add_column(
        "treasury_public_events",
        sa.Column("epoch_index", sa.BigInteger(), nullable=True),
    )
    op.drop_constraint("treasury_public_kind", "treasury_public_events", type_="check")
    op.create_check_constraint(
        "treasury_public_kind",
        "treasury_public_events",
        (
            "(event_kind IN ('service_distribution', 'vendor_payment') "
            "AND state = 'chain_finalized' AND finalized_event_id IS "
            "NULL AND credited_usd_nano IS NULL AND bounty_award_id IS "
            "NULL AND accepted_work_ref IS NULL) OR (event_kind = "
            "'gm_token_deposit' AND state = 'chain_finalized' AND "
            "finalized_event_id IS NULL AND credited_usd_nano IS NULL "
            "AND bounty_award_id IS NULL AND accepted_work_ref IS NULL) "
            "OR (event_kind = 'gm_credit_purchase' AND state = "
            "'reconciled' AND finalized_event_id IS NOT NULL AND "
            "credited_usd_nano IS NOT NULL AND credited_usd_nano > 0 AND "
            "bounty_award_id IS NULL AND accepted_work_ref IS NULL) OR "
            "(event_kind = 'maintenance_bounty' AND state = "
            "'chain_finalized' AND finalized_event_id IS NULL AND "
            "credited_usd_nano IS NULL AND bounty_award_id IS NOT NULL "
            "AND accepted_work_ref IS NOT NULL AND "
            "length(bounty_award_id) BETWEEN 8 AND 120 AND "
            "length(accepted_work_ref) BETWEEN 8 AND 240)"
        ),
    )
    op.drop_constraint(
        "treasury_public_denominator", "treasury_public_events", type_="check"
    )
    op.create_check_constraint(
        "treasury_public_denominator",
        "treasury_public_events",
        (
            "denominator IN ('miner_emission', "
            "'released_miner_emission', 'collector_liquid_emission', "
            "'not_attributed')"
        ),
    )
    op.drop_constraint(
        "treasury_public_allocation", "treasury_public_events", type_="check"
    )
    op.create_check_constraint(
        "treasury_public_allocation",
        "treasury_public_events",
        (
            "maintenance_bps BETWEEN 0 AND 10000 AND gm_bps BETWEEN 0 "
            "AND 10000 AND allocation_bps BETWEEN 0 AND 10000 AND "
            "allocated_alpha_rao >= 0 AND ((source_alpha_rao > 0 AND "
            "denominator <> 'not_attributed') OR (event_kind = "
            "'vendor_payment' AND denominator = 'not_attributed' AND "
            "source_alpha_rao = 0 AND allocated_alpha_rao = 0)) AND "
            "burn_revision >= 0 AND burn_share_micros BETWEEN 0 AND "
            "1000000"
        ),
    )
    op.drop_constraint(
        "treasury_public_purpose_allocation", "treasury_public_events", type_="check"
    )
    op.create_check_constraint(
        "treasury_public_purpose_allocation",
        "treasury_public_events",
        (
            "event_kind IN ('service_distribution', 'vendor_payment') OR "
            "(event_kind = 'maintenance_bounty' AND allocation_bps = "
            "maintenance_bps) OR (event_kind NOT IN "
            "('service_distribution', 'vendor_payment', "
            "'maintenance_bounty') AND allocation_bps = gm_bps)"
        ),
    )
    op.drop_constraint("treasury_public_route", "treasury_public_events", type_="check")
    op.create_check_constraint(
        "treasury_public_route",
        "treasury_public_events",
        (
            "route IN ('alpha_to_tao', 'alpha_to_gm_alpha', "
            "'alpha_transfer', 'alpha_to_tao_bounty', 'tao_transfer')"
        ),
    )
    op.drop_constraint(
        "treasury_public_actor_provenance", "treasury_public_events", type_="check"
    )
    op.create_check_constraint(
        "treasury_public_actor_provenance",
        "treasury_public_events",
        (
            "actor_provenance IN ('treasury_signer', 'gm_reconciler', "
            "'bounty_executor', 'treasury_observer')"
        ),
    )
    op.create_table(
        "treasury_verified_receipts",
        sa.Column("receipt_id", sa.Text(), nullable=False, primary_key=True),
        sa.Column("request_digest", sa.Text(), nullable=False),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("parent_receipt_id", sa.Text(), nullable=True),
        sa.Column("policy_digest", sa.Text(), nullable=False),
        sa.Column("collector_policy_digest", sa.Text(), nullable=False),
        sa.Column("bucket_id", sa.Text(), nullable=False),
        sa.Column("block_hash", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("epoch_index", sa.BigInteger(), nullable=False),
        sa.Column("source_block", sa.BigInteger(), nullable=True),
        sa.Column("amount_atomic", sa.BigInteger(), nullable=False),
        sa.Column("public_event_id", sa.BigInteger(), nullable=True),
        sa.Column("settings_revision", sa.Integer(), nullable=False),
        sa.Column("extrinsic_index", sa.Integer(), nullable=False),
        sa.Column("event_index", sa.Integer(), nullable=False),
        sa.Column("proof", postgresql.JSONB(), nullable=False),
        sa.Column("published", sa.Boolean(), nullable=False),
        sa.Column(
            "recorded_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["parent_receipt_id"], ["treasury_verified_receipts.receipt_id"]
        ),
        sa.ForeignKeyConstraint(
            ["settings_revision"], ["treasury_settings_revisions.revision"]
        ),
        sa.ForeignKeyConstraint(["public_event_id"], ["treasury_public_events.id"]),
        sa.UniqueConstraint(
            "block_hash",
            "extrinsic_index",
            "event_index",
            name="treasury_verified_chain_effect",
        ),
        sa.CheckConstraint(
            "stage IN ('service_distribution', 'vendor_payment')",
            name="treasury_verified_stage",
        ),
        sa.CheckConstraint(
            (
                "amount_atomic > 0 AND epoch_index >= 0 AND (source_block IS "
                "NULL OR source_block > 0) AND extrinsic_index >= 0 AND "
                "event_index >= 0"
            ),
            name="treasury_verified_bounds",
        ),
        sa.CheckConstraint(
            "published = (public_event_id IS NOT NULL)",
            name="treasury_verified_publication",
        ),
        sa.CheckConstraint(
            (
                "(stage = 'service_distribution' AND parent_receipt_id IS "
                "NULL AND source_block IS NOT NULL) OR (stage = "
                "'vendor_payment' AND ((parent_receipt_id IS NULL AND "
                "source_block IS NULL) OR (parent_receipt_id IS NOT NULL AND "
                "source_block IS NOT NULL)))"
            ),
            name="treasury_verified_parent",
        ),
    )
    op.create_index(
        "treasury_verified_distribution_once",
        "treasury_verified_receipts",
        ["epoch_index", "source_block", "bucket_id"],
        unique=True,
        postgresql_where=sa.text("stage = 'service_distribution'"),
    )
    op.execute(
        "CREATE TRIGGER treasury_verified_receipts_immutable BEFORE "
        "UPDATE OR DELETE ON treasury_verified_receipts FOR EACH ROW "
        "EXECUTE FUNCTION reject_treasury_public_event_mutation()"
    )


def downgrade():
    # A never-used schema can round-trip. Retained financial facts must never
    # be silently deleted or lose additive provenance during rollback. Hold
    # both locks through the guard and DDL so a concurrent writer cannot race.
    op.execute(
        "LOCK TABLE treasury_public_events, treasury_verified_receipts "
        "IN ACCESS EXCLUSIVE MODE"
    )
    retained = op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM treasury_verified_receipts) OR "
            "EXISTS (SELECT 1 FROM treasury_public_events WHERE "
            "bucket_id IS NOT NULL OR policy_digest IS NOT NULL OR "
            "epoch_index IS NOT NULL OR event_kind IN "
            "('service_distribution', 'vendor_payment') OR denominator IN "
            "('collector_liquid_emission', 'not_attributed') OR "
            "route = 'tao_transfer' OR actor_provenance = 'treasury_observer')"
        )
    )
    if retained:
        raise RuntimeError(
            "retained treasury financial history requires a reviewed forward repair"
        )
    op.drop_table("treasury_verified_receipts")
    previous = {
        "treasury_public_kind": (
            "(event_kind = 'gm_token_deposit' AND state = 'chain_finalized' "
            "AND finalized_event_id IS NULL AND credited_usd_nano IS NULL "
            "AND bounty_award_id IS NULL AND accepted_work_ref IS NULL) OR "
            "(event_kind = 'gm_credit_purchase' AND state = 'reconciled' "
            "AND finalized_event_id IS NOT NULL AND credited_usd_nano IS NOT NULL "
            "AND credited_usd_nano > 0 AND bounty_award_id IS NULL "
            "AND accepted_work_ref IS NULL) OR "
            "(event_kind = 'maintenance_bounty' AND state = 'chain_finalized' "
            "AND finalized_event_id IS NULL AND credited_usd_nano IS NULL "
            "AND bounty_award_id IS NOT NULL AND accepted_work_ref IS NOT NULL "
            "AND length(bounty_award_id) BETWEEN 8 AND 120 "
            "AND length(accepted_work_ref) BETWEEN 8 AND 240)"
        ),
        "treasury_public_denominator": (
            "denominator IN ('miner_emission', 'released_miner_emission')"
        ),
        "treasury_public_allocation": (
            "maintenance_bps BETWEEN 0 AND 10000 AND "
            "gm_bps BETWEEN 0 AND 10000 AND allocation_bps BETWEEN 0 AND 10000 "
            "AND allocated_alpha_rao >= 0 AND source_alpha_rao > 0 AND "
            "burn_revision >= 0 AND burn_share_micros BETWEEN 0 AND 1000000"
        ),
        "treasury_public_purpose_allocation": (
            "(event_kind = 'maintenance_bounty' AND allocation_bps = maintenance_bps) "
            "OR (event_kind <> 'maintenance_bounty' AND allocation_bps = gm_bps)"
        ),
        "treasury_public_route": (
            "route IN ('alpha_to_tao', 'alpha_to_gm_alpha', "
            "'alpha_transfer', 'alpha_to_tao_bounty')"
        ),
        "treasury_public_actor_provenance": (
            "actor_provenance IN "
            "('treasury_signer', 'gm_reconciler', 'bounty_executor')"
        ),
    }
    for name, expression in previous.items():
        op.drop_constraint(name, "treasury_public_events", type_="check")
        op.create_check_constraint(name, "treasury_public_events", expression)
    for name in ("epoch_index", "policy_digest", "bucket_id"):
        op.drop_column("treasury_public_events", name)
