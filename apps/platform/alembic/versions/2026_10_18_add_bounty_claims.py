"""Add paid bounty claims: holds, fee payments and refund obligations.

A signed claim holds one open slot; a verified fee receipt turns the hold
into a claimed slot. A payment that cannot win its slot is recorded with a
full fee-refund obligation, never forfeited. Claims, nonces, payments and
obligations are append-only; a slot may now also change its current claim
and deadline.
"""

import sqlalchemy as sa

from alembic import op

revision = "c5f81d2a9e47"
down_revision = "bb24cc6f573b"
branch_labels = None
depends_on = None

P1_EVENT_KINDS = ("revision_published", "round_opened")
EVENT_KINDS = (
    *P1_EVENT_KINDS,
    "slot_held",
    "hold_expired",
    "hold_released",
    "slot_claimed",
    "fee_refund_owed",
    "claims_paused",
    "claims_resumed",
)
# Bounty-wide events; every other event is about one claim and names it.
CLAIMLESS_EVENT_KINDS = (*P1_EVENT_KINDS, "claims_paused", "claims_resumed")
REFUND_REASONS = ("hold_expired", "slot_lost", "identity_changed", "duplicate_payment")
APPEND_ONLY = (
    "bounty_claim_controls",
    "bounty_action_nonces",
    "bounty_claims",
    "bounty_fee_payments",
    "bounty_obligations",
)
# Slots that a claim occupies or once occupied; an open slot has no claim.
CLAIMED_STATUSES = (
    "held",
    "claimed",
    "submitted",
    "rework",
    "accepted",
    "rejected",
    "expired",
    "withdrawn",
)


def _quoted(values: tuple[str, ...]) -> str:
    return ",".join(f"'{value}'" for value in values)


def _slot_trigger(*columns: str) -> str:
    mutable = "".join(f" - '{column}'" for column in columns)
    return f"""CREATE OR REPLACE FUNCTION bounty_slot_status_only()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'bounty slots cannot be deleted';
          END IF;
          IF to_jsonb(NEW){mutable} IS DISTINCT FROM to_jsonb(OLD){mutable}
          THEN
            RAISE EXCEPTION 'only a bounty slot status can change';
          END IF;
          RETURN NEW;
        END $$"""


def upgrade() -> None:
    # New tables and nullable columns on tables with no hot traffic.
    # Operator pauses of new claims: the latest row per bounty is in force.
    op.create_table(
        "bounty_claim_controls",
        sa.Column(
            "control_id", sa.BigInteger(), sa.Identity(always=True), primary_key=True
        ),
        sa.Column("bounty_id", sa.UUID(), nullable=False),
        sa.Column("paused", sa.Boolean(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["bounty_id"], ["bounties.bounty_id"]),
    )
    op.create_index(
        "bounty_claim_controls_bounty_idx",
        "bounty_claim_controls",
        ["bounty_id", "control_id"],
    )
    op.create_table(
        "bounty_action_nonces",
        sa.Column("nonce", sa.UUID(), primary_key=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("action IN ('claim')", name="action"),
    )
    op.create_table(
        "bounty_claims",
        sa.Column("claim_id", sa.UUID(), primary_key=True),
        sa.Column("bounty_id", sa.UUID(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("spec_digest", sa.Text(), nullable=False),
        sa.Column("slot_id", sa.UUID(), nullable=False),
        sa.Column("netuid", sa.Integer(), nullable=False),
        sa.Column("hotkey", sa.Text(), nullable=False),
        sa.Column("payee_coldkey", sa.Text(), nullable=False),
        sa.Column("github_login", sa.Text(), nullable=False),
        sa.Column("team_digest", sa.Text(), nullable=False),
        sa.Column("nonce", sa.UUID(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("key_kind", sa.Text(), nullable=False),
        sa.Column("signer", sa.Text(), nullable=False),
        sa.Column("signature", sa.Text(), nullable=False),
        sa.Column("owner_block", sa.BigInteger(), nullable=False),
        sa.Column("owner_block_hash", sa.Text(), nullable=False),
        sa.Column("github_link_id", sa.UUID(), nullable=False),
        sa.Column("github_user_id", sa.BigInteger(), nullable=False),
        sa.Column("cla_signature_id", sa.UUID(), nullable=False),
        sa.Column("funding_mode", sa.Text(), nullable=False),
        sa.Column("fee_alpha_rao", sa.BigInteger(), nullable=False),
        sa.Column("fee_escrow_coldkey", sa.Text(), nullable=False),
        sa.Column("fee_escrow_hotkey", sa.Text(), nullable=False),
        sa.Column("held_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hold_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("nonce"),
        # The target of references that must stay within the claim's bounty.
        sa.UniqueConstraint("claim_id", "bounty_id"),
        sa.ForeignKeyConstraint(
            ["bounty_id", "revision"],
            ["bounty_revisions.bounty_id", "bounty_revisions.revision"],
        ),
        sa.ForeignKeyConstraint(
            ["slot_id", "bounty_id"], ["bounty_slots.slot_id", "bounty_slots.bounty_id"]
        ),
        sa.ForeignKeyConstraint(["nonce"], ["bounty_action_nonces.nonce"]),
        sa.ForeignKeyConstraint(["github_link_id"], ["miner_github_links.link_id"]),
        sa.ForeignKeyConstraint(["cla_signature_id"], ["cla_signatures.signature_id"]),
        sa.CheckConstraint(
            "key_kind IN ('hotkey','coldkey') AND fee_alpha_rao > 0"
            " AND hold_expires_at > held_at AND github_user_id > 0"
            " AND funding_mode IN ('rehearsal','live')"
            " AND (key_kind = 'coldkey' OR signer = hotkey)"
            " AND (key_kind = 'hotkey' OR signer = payee_coldkey)",
            name="claim",
        ),
    )
    op.create_index("bounty_claims_bounty_idx", "bounty_claims", ["bounty_id"])
    op.create_index("bounty_claims_slot_idx", "bounty_claims", ["slot_id"])
    op.add_column("bounty_slots", sa.Column("claim_id", sa.UUID(), nullable=True))
    op.add_column(
        "bounty_slots",
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_bounty_slots_claim_id_bounty_claims"),
        "bounty_slots",
        "bounty_claims",
        ["claim_id", "bounty_id"],
        ["claim_id", "bounty_id"],
    )
    op.create_check_constraint(
        op.f("ck_bounty_slots_claim"),
        "bounty_slots",
        f"(status <> 'open' OR claim_id IS NULL)"
        f" AND (status NOT IN ({_quoted(CLAIMED_STATUSES)}) OR claim_id IS NOT NULL)"
        " AND (status <> 'claimed' OR deadline_at IS NOT NULL)",
    )
    op.execute(_slot_trigger("status", "status_changed_at", "claim_id", "deadline_at"))
    op.create_table(
        "bounty_fee_payments",
        sa.Column("block_hash", sa.Text(), nullable=False),
        sa.Column("extrinsic_index", sa.Integer(), nullable=False),
        sa.Column("block_number", sa.BigInteger(), nullable=False),
        sa.Column("claim_id", sa.UUID(), nullable=False),
        sa.Column("payer_coldkey", sa.Text(), nullable=False),
        sa.Column("amount_alpha_rao", sa.BigInteger(), nullable=False),
        sa.Column("simulated", sa.Boolean(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("block_hash", "extrinsic_index"),
        sa.ForeignKeyConstraint(["claim_id"], ["bounty_claims.claim_id"]),
        # The target of an obligation's composite reference to its payment.
        sa.UniqueConstraint(
            "claim_id", "block_hash", "extrinsic_index", "payer_coldkey"
        ),
        sa.CheckConstraint(
            "amount_alpha_rao > 0 AND extrinsic_index >= 0 AND block_number >= 0"
            " AND outcome IN ('allocated','refund_owed')",
            name="payment",
        ),
    )
    # One fee wins a slot per claim; any further payment is refunded.
    op.create_index(
        "bounty_fee_payments_allocated_idx",
        "bounty_fee_payments",
        ["claim_id"],
        unique=True,
        postgresql_where=sa.text("outcome = 'allocated'"),
    )
    op.create_table(
        "bounty_obligations",
        sa.Column("obligation_id", sa.UUID(), primary_key=True),
        sa.Column("bounty_id", sa.UUID(), nullable=False),
        sa.Column("claim_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("payee_coldkey", sa.Text(), nullable=False),
        sa.Column("amount_alpha_rao", sa.BigInteger(), nullable=False),
        sa.Column("block_hash", sa.Text(), nullable=False),
        sa.Column("extrinsic_index", sa.Integer(), nullable=False),
        sa.Column("simulated", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        # One obligation per receipt: its refund, or its fee's one disposition.
        sa.UniqueConstraint("block_hash", "extrinsic_index"),
        # The target of an event's reference to an obligation of its claim.
        sa.UniqueConstraint("claim_id", "obligation_id"),
        sa.ForeignKeyConstraint(["bounty_id"], ["bounties.bounty_id"]),
        # An obligation's claim belongs to the obligation's own bounty.
        sa.ForeignKeyConstraint(
            ["claim_id", "bounty_id"],
            ["bounty_claims.claim_id", "bounty_claims.bounty_id"],
        ),
        # A fee obligation is the payment of its own claim, owed to its payer.
        sa.ForeignKeyConstraint(
            ["claim_id", "block_hash", "extrinsic_index", "payee_coldkey"],
            [
                "bounty_fee_payments.claim_id",
                "bounty_fee_payments.block_hash",
                "bounty_fee_payments.extrinsic_index",
                "bounty_fee_payments.payer_coldkey",
            ],
        ),
        sa.CheckConstraint(
            "kind IN ('fee_refund') AND amount_alpha_rao > 0"
            f" AND reason IN ({_quoted(REFUND_REASONS)})",
            name="obligation",
        ),
    )
    op.create_index("bounty_obligations_claim_idx", "bounty_obligations", ["claim_id"])
    op.add_column("bounty_events", sa.Column("claim_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        op.f("fk_bounty_events_claim_id_bounty_claims"),
        "bounty_events",
        "bounty_claims",
        ["claim_id", "bounty_id"],
        ["claim_id", "bounty_id"],
    )
    op.drop_constraint(op.f("ck_bounty_events_kind"), "bounty_events", type_="check")
    op.create_check_constraint(
        op.f("ck_bounty_events_kind"),
        "bounty_events",
        f"kind IN ({_quoted(EVENT_KINDS)})",
    )
    # A NULL claim would skip the composite key, so a claim's event needs one.
    op.create_check_constraint(
        op.f("ck_bounty_events_claim_scope"),
        "bounty_events",
        f"claim_id IS NOT NULL OR kind IN ({_quoted(CLAIMLESS_EVENT_KINDS)})",
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


def downgrade() -> None:
    bind = op.get_bind()
    # Refuse while anything this downgrade would drop, or the restored event
    # check would reject, is recorded.
    for recorded in (
        "SELECT 1 FROM bounty_claims",
        "SELECT 1 FROM bounty_claim_controls",
        "SELECT 1 FROM bounty_action_nonces",
        "SELECT 1 FROM bounty_fee_payments",
        "SELECT 1 FROM bounty_obligations",
        f"SELECT 1 FROM bounty_events WHERE kind NOT IN ({_quoted(P1_EVENT_KINDS)})",
    ):
        if bind.execute(sa.text(f"SELECT EXISTS ({recorded})")).scalar():
            raise RuntimeError(
                "refusing to drop recorded bounty claims, payments or controls"
            )
    op.drop_constraint(op.f("ck_bounty_events_kind"), "bounty_events", type_="check")
    op.create_check_constraint(
        op.f("ck_bounty_events_kind"),
        "bounty_events",
        f"kind IN ({_quoted(P1_EVENT_KINDS)})",
    )
    op.drop_constraint(
        op.f("ck_bounty_events_claim_scope"), "bounty_events", type_="check"
    )
    op.drop_constraint(
        op.f("fk_bounty_events_claim_id_bounty_claims"),
        "bounty_events",
        type_="foreignkey",
    )
    op.drop_column("bounty_events", "claim_id")
    op.drop_table("bounty_obligations")
    op.drop_table("bounty_fee_payments")
    op.execute(_slot_trigger("status", "status_changed_at"))
    op.drop_constraint(op.f("ck_bounty_slots_claim"), "bounty_slots", type_="check")
    op.drop_constraint(
        op.f("fk_bounty_slots_claim_id_bounty_claims"),
        "bounty_slots",
        type_="foreignkey",
    )
    op.drop_column("bounty_slots", "deadline_at")
    op.drop_column("bounty_slots", "claim_id")
    op.drop_table("bounty_claims")
    op.drop_table("bounty_action_nonces")
    op.drop_table("bounty_claim_controls")
