"""Add bounty settlement: awards, dispositions, approvals, transfers, facts.

Every confirmed fee ends in exactly one disposition: refunded in full
(accepted, cancelled, system fault) or retained (rejected, expired, withdrawn
once its slot is reopened). Awards are obligations bounded by the reward
pool. An obligation moves money only after approvals bound to its payee,
amount and the payout policy revision, through a transfer whose uncertain
outcome must be reconciled before any retry. Merge, deployment and adoption
are public facts, never payment conditions. All but a transfer's own outcome
are append-only.
"""

import sqlalchemy as sa

from alembic import op

revision = "e1b8c4f7a209"
down_revision = "d7a3e9b2c641"
branch_labels = None
depends_on = None

P3_EVENT_KINDS = (
    "revision_published",
    "round_opened",
    "slot_held",
    "hold_expired",
    "hold_released",
    "slot_claimed",
    "fee_refund_owed",
    "claims_paused",
    "claims_resumed",
    "submitted",
    "withdrawn",
    "deadline_missed",
    "review_overdue",
    "rework_requested",
    "accepted",
    "rejected",
    "reviewer_reassigned",
    "appeal_filed",
    "appeal_upheld",
    "appeal_denied",
    "slot_cancelled",
    "slot_reopened",
)
EVENT_KINDS = (
    *P3_EVENT_KINDS,
    "fee_retained",
    "award_recorded",
    "obligation_approved",
    "transfer_dispatched",
    "transfer_confirmed",
    "transfer_failed",
    "transfer_uncertain",
    "fact_recorded",
)
P3_REASONS = (
    "hold_expired",
    "slot_lost",
    "identity_changed",
    "duplicate_payment",
    "cancelled",
    "system_fault",
)
REASONS = (*P3_REASONS, "accepted", "rejected", "expired", "withdrawn", "accepted_work")
TRANSFER_STATES = ("dispatched", "confirmed", "failed", "expired", "uncertain")
# A transfer in these states blocks another for the same obligation.
LIVE_TRANSFER_STATES = ("dispatched", "uncertain", "confirmed")
APPEND_ONLY = (
    "bounty_payout_policies",
    "bounty_obligation_approvals",
    "bounty_delivery_facts",
    "bounty_transfer_reconciliations",
)
RECONCILE_OUTCOMES = ("confirmed", "failed", "expired", "unknown")


def _quoted(values: tuple[str, ...]) -> str:
    return ",".join(f"'{value}'" for value in values)


def _replace_check(table: str, name: str, condition: str) -> None:
    op.drop_constraint(op.f(name), table, type_="check")
    op.create_check_constraint(op.f(name), table, condition)


def _obligation_check(*, settled: bool) -> str:
    if not settled:
        return (
            "kind IN ('fee_refund') AND amount_alpha_rao > 0"
            f" AND reason IN ({_quoted(P3_REASONS)})"
        )
    return (
        "kind IN ('fee_refund','fee_retained','award') AND amount_alpha_rao > 0"
        f" AND reason IN ({_quoted(REASONS)})"
        # Awards pay for accepted work; fee dispositions follow one receipt.
        " AND (kind = 'award') = (block_hash IS NULL)"
        " AND (block_hash IS NULL) = (extrinsic_index IS NULL)"
        " AND (kind = 'award') = (submission_id IS NOT NULL)"
        " AND (kind = 'award') = (actor IS NOT NULL)"
        " AND (kind = 'award') = (decision_reason IS NOT NULL)"
        " AND (kind = 'award' OR attribution IS NULL)"
    )


def upgrade() -> None:
    # New tables and nullable columns on tables with no hot traffic.
    op.alter_column("bounty_obligations", "block_hash", nullable=True)
    op.alter_column("bounty_obligations", "extrinsic_index", nullable=True)
    op.add_column(
        "bounty_obligations", sa.Column("submission_id", sa.UUID(), nullable=True)
    )
    op.add_column("bounty_obligations", sa.Column("attribution", sa.Text()))
    op.add_column("bounty_obligations", sa.Column("actor", sa.Text()))
    # Private: why the operator decided the award.
    op.add_column("bounty_obligations", sa.Column("decision_reason", sa.Text()))
    op.create_foreign_key(
        op.f("fk_bounty_obligations_submission_id_bounty_submissions"),
        "bounty_obligations",
        "bounty_submissions",
        ["submission_id"],
        ["submission_id"],
    )
    _replace_check(
        "bounty_obligations",
        "ck_bounty_obligations_obligation",
        _obligation_check(settled=True),
    )
    # One award per accepted claim; a shared award awards each claim.
    op.create_index(
        "bounty_obligations_award_idx",
        "bounty_obligations",
        ["claim_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'award'"),
    )
    op.create_table(
        "bounty_payout_policies",
        sa.Column("revision", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("single_approver_max_alpha_rao", sa.BigInteger(), nullable=False),
        sa.Column("dual_approver_max_alpha_rao", sa.BigInteger(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "revision > 0 AND single_approver_max_alpha_rao >= 0"
            " AND dual_approver_max_alpha_rao >= single_approver_max_alpha_rao",
            name="policy",
        ),
    )
    op.create_table(
        "bounty_obligation_approvals",
        sa.Column("approval_id", sa.UUID(), primary_key=True),
        sa.Column("obligation_id", sa.UUID(), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False),
        sa.Column("payee_coldkey", sa.Text(), nullable=False),
        sa.Column("amount_alpha_rao", sa.BigInteger(), nullable=False),
        sa.Column("policy_revision", sa.Integer(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        # An approval counts only under the policy revision in force.
        sa.UniqueConstraint("obligation_id", "round", "actor", "policy_revision"),
        sa.ForeignKeyConstraint(
            ["obligation_id"], ["bounty_obligations.obligation_id"]
        ),
        sa.ForeignKeyConstraint(
            ["policy_revision"], ["bounty_payout_policies.revision"]
        ),
        sa.CheckConstraint("round > 0 AND amount_alpha_rao > 0", name="approval"),
    )
    op.create_table(
        "bounty_transfers",
        sa.Column("transfer_id", sa.UUID(), primary_key=True),
        sa.Column("obligation_id", sa.UUID(), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False),
        sa.Column("executor", sa.Text(), nullable=False),
        sa.Column("simulated", sa.Boolean(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("block_hash", sa.Text(), nullable=True),
        sa.Column("block_number", sa.BigInteger(), nullable=True),
        sa.Column("extrinsic_index", sa.Integer(), nullable=True),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("obligation_id", "round"),
        sa.ForeignKeyConstraint(
            ["obligation_id"], ["bounty_obligations.obligation_id"]
        ),
        sa.CheckConstraint(
            f"state IN ({_quoted(TRANSFER_STATES)}) AND round > 0"
            " AND (state = 'confirmed') = (block_hash IS NOT NULL)"
            " AND (block_hash IS NULL) = (extrinsic_index IS NULL)"
            " AND (block_hash IS NULL) = (block_number IS NULL)"
            " AND (state IN ('dispatched','uncertain')) = (settled_at IS NULL)",
            name="transfer",
        ),
    )
    # Never two transfers in flight, or a second after one confirmed.
    op.create_index(
        "bounty_transfers_live_idx",
        "bounty_transfers",
        ["obligation_id"],
        unique=True,
        postgresql_where=sa.text(f"state IN ({_quoted(LIVE_TRANSFER_STATES)})"),
    )
    # Who reconciled a transfer, what they asked for, and what it showed.
    op.create_table(
        "bounty_transfer_reconciliations",
        sa.Column(
            "reconciliation_id",
            sa.BigInteger(),
            sa.Identity(always=True),
            primary_key=True,
        ),
        sa.Column("transfer_id", sa.UUID(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("requested", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["transfer_id"], ["bounty_transfers.transfer_id"]),
        sa.CheckConstraint(
            f"state IN ({_quoted(TRANSFER_STATES)})"
            f" AND (requested IS NULL OR requested IN ({_quoted(RECONCILE_OUTCOMES)}))",
            name="reconciliation",
        ),
    )
    op.create_index(
        "bounty_transfer_reconciliations_transfer_idx",
        "bounty_transfer_reconciliations",
        ["transfer_id"],
    )
    op.create_table(
        "bounty_delivery_facts",
        sa.Column(
            "fact_id", sa.BigInteger(), sa.Identity(always=True), primary_key=True
        ),
        sa.Column("claim_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("reference", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("claim_id", "kind"),
        sa.ForeignKeyConstraint(["claim_id"], ["bounty_claims.claim_id"]),
        sa.CheckConstraint(
            "kind IN ('merged','deployed','adopted') AND length(reference) > 0",
            name="fact",
        ),
    )
    op.add_column("bounty_events", sa.Column("obligation_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        op.f("fk_bounty_events_obligation_id_bounty_obligations"),
        "bounty_events",
        "bounty_obligations",
        ["obligation_id"],
        ["obligation_id"],
    )
    # An event's obligation belongs to the event's own claim.
    op.create_foreign_key(
        op.f("fk_bounty_events_claim_id_bounty_obligations"),
        "bounty_events",
        "bounty_obligations",
        ["claim_id", "obligation_id"],
        ["claim_id", "obligation_id"],
    )
    _replace_check(
        "bounty_events", "ck_bounty_events_kind", f"kind IN ({_quoted(EVENT_KINDS)})"
    )
    for table in APPEND_ONLY:
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION bounty_history_immutable()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION bounty_history_immutable()"
        )
    # A transfer keeps its identity; only its outcome is recorded, forward.
    op.execute("""CREATE FUNCTION bounty_transfer_outcome_only()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'bounty transfers cannot be deleted';
          END IF;
          IF OLD.state NOT IN ('dispatched', 'uncertain') THEN
            RAISE EXCEPTION 'a settled bounty transfer cannot change';
          END IF;
          IF to_jsonb(NEW) - 'state' - 'block_hash' - 'block_number'
               - 'extrinsic_index' - 'settled_at'
             IS DISTINCT FROM to_jsonb(OLD) - 'state' - 'block_hash'
               - 'block_number' - 'extrinsic_index' - 'settled_at'
          THEN
            RAISE EXCEPTION 'only a bounty transfer outcome can change';
          END IF;
          RETURN NEW;
        END $$""")
    op.execute(
        "CREATE TRIGGER bounty_transfers_outcome_only BEFORE UPDATE OR DELETE "
        "ON bounty_transfers FOR EACH ROW "
        "EXECUTE FUNCTION bounty_transfer_outcome_only()"
    )
    op.execute(
        "CREATE TRIGGER bounty_transfers_no_truncate BEFORE TRUNCATE ON "
        "bounty_transfers FOR EACH STATEMENT "
        "EXECUTE FUNCTION bounty_history_immutable()"
    )


def downgrade() -> None:
    bind = op.get_bind()
    # Refuse while anything this downgrade would drop, or a restored check
    # would reject, is recorded.
    for recorded in (
        "SELECT 1 FROM bounty_delivery_facts",
        "SELECT 1 FROM bounty_transfer_reconciliations",
        "SELECT 1 FROM bounty_transfers",
        "SELECT 1 FROM bounty_obligation_approvals",
        "SELECT 1 FROM bounty_payout_policies",
        f"SELECT 1 FROM bounty_events WHERE kind NOT IN ({_quoted(P3_EVENT_KINDS)})",
        "SELECT 1 FROM bounty_obligations WHERE kind <> 'fee_refund' "
        f"OR reason NOT IN ({_quoted(P3_REASONS)})",
    ):
        if bind.execute(sa.text(f"SELECT EXISTS ({recorded})")).scalar():
            raise RuntimeError("refusing to drop recorded bounty settlement")
    _replace_check(
        "bounty_events",
        "ck_bounty_events_kind",
        f"kind IN ({_quoted(P3_EVENT_KINDS)})",
    )
    for constraint in (
        "fk_bounty_events_claim_id_bounty_obligations",
        "fk_bounty_events_obligation_id_bounty_obligations",
    ):
        op.drop_constraint(op.f(constraint), "bounty_events", type_="foreignkey")
    op.drop_column("bounty_events", "obligation_id")
    for table in (
        "bounty_delivery_facts",
        "bounty_transfer_reconciliations",
        "bounty_transfers",
        "bounty_obligation_approvals",
        "bounty_payout_policies",
    ):
        op.drop_table(table)
    op.execute("DROP FUNCTION bounty_transfer_outcome_only()")
    op.drop_index("bounty_obligations_award_idx", table_name="bounty_obligations")
    _replace_check(
        "bounty_obligations",
        "ck_bounty_obligations_obligation",
        _obligation_check(settled=False),
    )
    op.drop_constraint(
        op.f("fk_bounty_obligations_submission_id_bounty_submissions"),
        "bounty_obligations",
        type_="foreignkey",
    )
    for column in ("decision_reason", "actor", "attribution", "submission_id"):
        op.drop_column("bounty_obligations", column)
    op.alter_column("bounty_obligations", "extrinsic_index", nullable=False)
    op.alter_column("bounty_obligations", "block_hash", nullable=False)
