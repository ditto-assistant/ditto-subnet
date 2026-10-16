"""Add bounty submissions, deadlines, review decisions and appeals.

A claimant submits the exact commit of a pull request; the reviewer accepts,
requests changes with a recorded deadline, or rejects that commit. Missed
delivery and rework deadlines expire the slot, stamped at the deadline. A
claimant may appeal a rejection or a missed deadline. Operators can reassign
the reviewer, cancel a slot (refunding a paid fee) and reopen a closed one.
Every record is append-only; a slot also tracks its review due date.
"""

import sqlalchemy as sa

from alembic import op

revision = "d7a3e9b2c641"
down_revision = "c5f81d2a9e47"
branch_labels = None
depends_on = None

P2_EVENT_KINDS = (
    "revision_published",
    "round_opened",
    "slot_held",
    "hold_expired",
    "hold_released",
    "slot_claimed",
    "fee_refund_owed",
    "claims_paused",
    "claims_resumed",
)
EVENT_KINDS = (
    *P2_EVENT_KINDS,
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
P2_REFUND_REASONS = (
    "hold_expired",
    "slot_lost",
    "identity_changed",
    "duplicate_payment",
)
REFUND_REASONS = (*P2_REFUND_REASONS, "cancelled", "system_fault")
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
APPEND_ONLY = (
    "bounty_withdrawals",
    "bounty_submissions",
    "bounty_reviews",
    "bounty_reviewer_assignments",
    "bounty_appeals",
    "bounty_appeal_decisions",
    "bounty_slot_actions",
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


def _slot_claim_check(*, with_review: bool) -> str:
    check = (
        "(status <> 'open' OR claim_id IS NULL)"
        f" AND (status NOT IN ({_quoted(CLAIMED_STATUSES)}) OR claim_id IS NOT NULL)"
        " AND (status <> 'claimed' OR deadline_at IS NOT NULL)"
    )
    if with_review:
        check += (
            " AND (status <> 'rework' OR deadline_at IS NOT NULL)"
            " AND (status <> 'submitted' OR review_due_at IS NOT NULL)"
        )
    return check


def _replace_check(table: str, name: str, condition: str) -> None:
    op.drop_constraint(op.f(name), table, type_="check")
    op.create_check_constraint(op.f(name), table, condition)


def upgrade() -> None:
    # New tables and nullable columns on tables with no hot traffic.
    _replace_check(
        "bounty_action_nonces",
        "ck_bounty_action_nonces_action",
        "action IN ('claim','submit','withdraw','appeal')",
    )
    op.add_column(
        "bounty_slots",
        sa.Column("review_due_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        _slot_trigger(
            "status", "status_changed_at", "claim_id", "deadline_at", "review_due_at"
        )
    )
    _replace_check(
        "bounty_slots", "ck_bounty_slots_claim", _slot_claim_check(with_review=True)
    )
    op.create_table(
        "bounty_submissions",
        sa.Column("submission_id", sa.UUID(), primary_key=True),
        sa.Column("claim_id", sa.UUID(), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=False),
        sa.Column("head_sha", sa.Text(), nullable=False),
        sa.Column("nonce", sa.UUID(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("key_kind", sa.Text(), nullable=False),
        sa.Column("signer", sa.Text(), nullable=False),
        sa.Column("signature", sa.Text(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("review_due_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("nonce"),
        # The target of a review's composite reference to its submission.
        sa.UniqueConstraint("claim_id", "submission_id"),
        sa.ForeignKeyConstraint(["claim_id"], ["bounty_claims.claim_id"]),
        sa.ForeignKeyConstraint(["nonce"], ["bounty_action_nonces.nonce"]),
        sa.CheckConstraint(
            "pr_number > 0 AND head_sha ~ '^[0-9a-f]{40}$'"
            " AND key_kind IN ('hotkey','coldkey') AND review_due_at > submitted_at",
            name="submission",
        ),
    )
    op.create_index("bounty_submissions_claim_idx", "bounty_submissions", ["claim_id"])
    op.create_table(
        "bounty_withdrawals",
        sa.Column("claim_id", sa.UUID(), primary_key=True),
        sa.Column("nonce", sa.UUID(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("key_kind", sa.Text(), nullable=False),
        sa.Column("signer", sa.Text(), nullable=False),
        sa.Column("signature", sa.Text(), nullable=False),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("nonce"),
        sa.ForeignKeyConstraint(["claim_id"], ["bounty_claims.claim_id"]),
        sa.ForeignKeyConstraint(["nonce"], ["bounty_action_nonces.nonce"]),
        sa.CheckConstraint("key_kind IN ('hotkey','coldkey')", name="withdrawal"),
    )
    op.create_table(
        "bounty_reviews",
        sa.Column("submission_id", sa.UUID(), primary_key=True),
        sa.Column("claim_id", sa.UUID(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("attribution", sa.Text(), nullable=True),
        sa.Column("rework_deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewer_actor", sa.Text(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        # A review decides a submission of its own claim.
        sa.ForeignKeyConstraint(
            ["claim_id", "submission_id"],
            ["bounty_submissions.claim_id", "bounty_submissions.submission_id"],
        ),
        sa.ForeignKeyConstraint(["claim_id"], ["bounty_claims.claim_id"]),
        sa.CheckConstraint(
            "decision IN ('accept','changes_requested','reject')"
            " AND (decision = 'changes_requested') = (rework_deadline_at IS NOT NULL)"
            " AND (decision = 'accept' OR attribution IS NULL)",
            name="review",
        ),
    )
    op.create_table(
        "bounty_reviewer_assignments",
        sa.Column(
            "assignment_id",
            sa.BigInteger(),
            sa.Identity(always=True),
            primary_key=True,
        ),
        sa.Column("bounty_id", sa.UUID(), nullable=False),
        sa.Column("reviewer_actor", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["bounty_id"], ["bounties.bounty_id"]),
    )
    op.create_index(
        "bounty_reviewer_assignments_bounty_idx",
        "bounty_reviewer_assignments",
        ["bounty_id", "assignment_id"],
    )
    # The target of an appeal's reference to an event of its own claim.
    op.create_unique_constraint(
        op.f("uq_bounty_events_event_id"), "bounty_events", ["event_id", "claim_id"]
    )
    op.create_table(
        "bounty_appeals",
        sa.Column("appeal_id", sa.UUID(), primary_key=True),
        sa.Column("claim_id", sa.UUID(), nullable=False),
        sa.Column("subject_event_id", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("reason_digest", sa.Text(), nullable=False),
        sa.Column("nonce", sa.UUID(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("key_kind", sa.Text(), nullable=False),
        sa.Column("signer", sa.Text(), nullable=False),
        sa.Column("signature", sa.Text(), nullable=False),
        sa.Column("filed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("nonce"),
        # One appeal per decision.
        sa.UniqueConstraint("subject_event_id"),
        sa.ForeignKeyConstraint(["claim_id"], ["bounty_claims.claim_id"]),
        # The appealed decision must be an event of the appeal's own claim.
        sa.ForeignKeyConstraint(
            ["subject_event_id", "claim_id"],
            ["bounty_events.event_id", "bounty_events.claim_id"],
        ),
        sa.ForeignKeyConstraint(["nonce"], ["bounty_action_nonces.nonce"]),
        sa.CheckConstraint(
            "length(reason_digest) = 64 AND key_kind IN ('hotkey','coldkey')",
            name="appeal",
        ),
    )
    op.create_table(
        "bounty_appeal_decisions",
        sa.Column("appeal_id", sa.UUID(), primary_key=True),
        sa.Column("upheld", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        # Upholding restores a delivery deadline; it is part of the decision.
        sa.Column("restored_deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["appeal_id"], ["bounty_appeals.appeal_id"]),
        sa.CheckConstraint(
            "upheld = (restored_deadline_at IS NOT NULL)", name="restored_deadline"
        ),
    )
    op.create_table(
        "bounty_slot_actions",
        sa.Column(
            "action_id", sa.BigInteger(), sa.Identity(always=True), primary_key=True
        ),
        sa.Column("slot_id", sa.UUID(), nullable=False),
        sa.Column("claim_id", sa.UUID(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("cancel_kind", sa.Text(), nullable=True),
        sa.Column("prior_status", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["slot_id"], ["bounty_slots.slot_id"]),
        sa.ForeignKeyConstraint(["claim_id"], ["bounty_claims.claim_id"]),
        sa.CheckConstraint(
            "action IN ('cancel','reopen')"
            " AND (action = 'cancel') = (cancel_kind IS NOT NULL)"
            " AND (cancel_kind IS NULL OR cancel_kind IN ('team','system_fault'))"
            " AND (action <> 'cancel'"
            " OR prior_status IN ('open','claimed','submitted','rework'))"
            " AND (action <> 'reopen'"
            " OR prior_status IN ('expired','withdrawn','rejected','cancelled'))",
            name="slot_action",
        ),
    )
    _replace_check(
        "bounty_obligations",
        "ck_bounty_obligations_obligation",
        "kind IN ('fee_refund') AND amount_alpha_rao > 0"
        f" AND reason IN ({_quoted(REFUND_REASONS)})",
    )
    op.add_column("bounty_events", sa.Column("submission_id", sa.UUID(), nullable=True))
    op.add_column("bounty_events", sa.Column("appeal_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        op.f("fk_bounty_events_submission_id_bounty_submissions"),
        "bounty_events",
        "bounty_submissions",
        ["submission_id"],
        ["submission_id"],
    )
    op.create_foreign_key(
        op.f("fk_bounty_events_appeal_id_bounty_appeals"),
        "bounty_events",
        "bounty_appeals",
        ["appeal_id"],
        ["appeal_id"],
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


def downgrade() -> None:
    bind = op.get_bind()
    # Refuse while anything this downgrade would drop, or a restored check
    # would reject, is recorded.
    for recorded in (
        *(f"SELECT 1 FROM {table}" for table in APPEND_ONLY),
        f"SELECT 1 FROM bounty_events WHERE kind NOT IN ({_quoted(P2_EVENT_KINDS)})",
        "SELECT 1 FROM bounty_obligations "
        f"WHERE reason NOT IN ({_quoted(P2_REFUND_REASONS)})",
        "SELECT 1 FROM bounty_action_nonces WHERE action <> 'claim'",
        "SELECT 1 FROM bounty_slots WHERE review_due_at IS NOT NULL "
        "OR status IN ('submitted','rework','accepted','rejected','expired',"
        "'withdrawn','cancelled')",
    ):
        if bind.execute(sa.text(f"SELECT EXISTS ({recorded})")).scalar():
            raise RuntimeError(
                "refusing to drop recorded bounty submissions, reviews or slot actions"
            )
    _replace_check(
        "bounty_events",
        "ck_bounty_events_kind",
        f"kind IN ({_quoted(P2_EVENT_KINDS)})",
    )
    op.drop_constraint(
        op.f("fk_bounty_events_appeal_id_bounty_appeals"),
        "bounty_events",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("fk_bounty_events_submission_id_bounty_submissions"),
        "bounty_events",
        type_="foreignkey",
    )
    op.drop_column("bounty_events", "appeal_id")
    op.drop_column("bounty_events", "submission_id")
    _replace_check(
        "bounty_obligations",
        "ck_bounty_obligations_obligation",
        "kind IN ('fee_refund') AND amount_alpha_rao > 0"
        f" AND reason IN ({_quoted(P2_REFUND_REASONS)})",
    )
    for table in (
        "bounty_slot_actions",
        "bounty_appeal_decisions",
        "bounty_appeals",
        "bounty_reviewer_assignments",
        "bounty_reviews",
        "bounty_submissions",
        "bounty_withdrawals",
    ):
        op.drop_table(table)
    op.drop_constraint(
        op.f("uq_bounty_events_event_id"), "bounty_events", type_="unique"
    )
    _replace_check(
        "bounty_slots", "ck_bounty_slots_claim", _slot_claim_check(with_review=False)
    )
    op.execute(_slot_trigger("status", "status_changed_at", "claim_id", "deadline_at"))
    op.drop_column("bounty_slots", "review_due_at")
    _replace_check(
        "bounty_action_nonces",
        "ck_bounty_action_nonces_action",
        "action IN ('claim')",
    )
