"""Add build bounties: signed revisions, rounds, slots and a public trail.

No bounty is seeded. Operators publish each bounty's terms as an append-only
revision; slots are the only rows whose status changes.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "bb24cc6f573b"
down_revision = "dc41b7a6e205"
branch_labels = None
depends_on = None

SLOT_STATUSES = (
    "open",
    "held",
    "claimed",
    "submitted",
    "rework",
    "accepted",
    "rejected",
    "expired",
    "withdrawn",
    "cancelled",
)
EVENT_KINDS = ("revision_published", "round_opened")
APPEND_ONLY = ("bounties", "bounty_revisions", "bounty_rounds", "bounty_events")


def _quoted(values: tuple[str, ...]) -> str:
    return ",".join(f"'{value}'" for value in values)


def upgrade() -> None:
    # New tables only: no backfill or scan of existing hot tables.
    op.create_table(
        "bounties",
        sa.Column("bounty_id", sa.UUID(), primary_key=True),
        sa.Column("repo", sa.Text(), nullable=False),
        sa.Column("issue_number", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("repo", "issue_number"),
        sa.CheckConstraint("issue_number > 0", name="issue"),
        sa.CheckConstraint("repo = lower(repo)", name="repo"),
    )
    op.create_table(
        "bounty_revisions",
        sa.Column("bounty_id", sa.UUID(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("public_interfaces", sa.Text(), nullable=False),
        sa.Column("acceptance_evidence", sa.Text(), nullable=False),
        sa.Column(
            "dependencies", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("reviewer_login", sa.Text(), nullable=False),
        sa.Column("entry_fee_alpha_rao", sa.BigInteger(), nullable=False),
        sa.Column("reward_pool_alpha_rao", sa.BigInteger(), nullable=False),
        sa.Column("attempts_per_round", sa.Integer(), nullable=False),
        sa.Column("delivery_days", sa.Integer(), nullable=False),
        sa.Column("review_days", sa.Integer(), nullable=False),
        sa.Column("hold_minutes", sa.Integer(), nullable=False),
        sa.Column("funding_mode", sa.Text(), nullable=False),
        sa.Column("fee_escrow_coldkey", sa.Text(), nullable=False),
        sa.Column("fee_escrow_hotkey", sa.Text(), nullable=False),
        sa.Column("spec_digest", sa.Text(), nullable=False),
        sa.Column("reviewer_actor", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("bounty_id", "revision"),
        sa.ForeignKeyConstraint(["bounty_id"], ["bounties.bounty_id"]),
        sa.CheckConstraint(
            "revision > 0 AND length(spec_digest) = 64"
            " AND entry_fee_alpha_rao >= 0 AND reward_pool_alpha_rao >= 0"
            " AND attempts_per_round BETWEEN 1 AND 10"
            " AND delivery_days BETWEEN 1 AND 60 AND review_days BETWEEN 1 AND 60"
            " AND hold_minutes BETWEEN 1 AND 1440"
            " AND funding_mode IN ('rehearsal','live')",
            name="terms",
        ),
    )
    op.create_table(
        "bounty_rounds",
        sa.Column("bounty_id", sa.UUID(), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False),
        sa.Column("opened_revision", sa.Integer(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("bounty_id", "round"),
        sa.ForeignKeyConstraint(
            ["bounty_id", "opened_revision"],
            ["bounty_revisions.bounty_id", "bounty_revisions.revision"],
        ),
        sa.CheckConstraint("round > 0", name="round"),
    )
    op.create_table(
        "bounty_slots",
        sa.Column("slot_id", sa.UUID(), primary_key=True),
        sa.Column("bounty_id", sa.UUID(), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("bounty_id", "round", "position"),
        # The target of references that must stay within the slot's bounty.
        sa.UniqueConstraint("slot_id", "bounty_id"),
        sa.ForeignKeyConstraint(
            ["bounty_id", "round"], ["bounty_rounds.bounty_id", "bounty_rounds.round"]
        ),
        sa.CheckConstraint("position > 0", name="position"),
        sa.CheckConstraint(f"status IN ({_quoted(SLOT_STATUSES)})", name="status"),
    )
    op.create_table(
        "bounty_events",
        sa.Column(
            "event_id",
            sa.BigInteger(),
            sa.Identity(always=True),
            primary_key=True,
        ),
        sa.Column("bounty_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=True),
        sa.Column("round", sa.Integer(), nullable=True),
        sa.Column("slot_id", sa.UUID(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["bounty_id"], ["bounties.bounty_id"]),
        # Every reference stays within the event's own bounty.
        sa.ForeignKeyConstraint(
            ["bounty_id", "revision"],
            ["bounty_revisions.bounty_id", "bounty_revisions.revision"],
        ),
        sa.ForeignKeyConstraint(
            ["bounty_id", "round"], ["bounty_rounds.bounty_id", "bounty_rounds.round"]
        ),
        sa.ForeignKeyConstraint(
            ["slot_id", "bounty_id"], ["bounty_slots.slot_id", "bounty_slots.bounty_id"]
        ),
        sa.CheckConstraint(f"kind IN ({_quoted(EVENT_KINDS)})", name="kind"),
    )
    op.create_index(
        "bounty_events_bounty_idx", "bounty_events", ["bounty_id", "event_id"]
    )
    op.execute("""CREATE FUNCTION bounty_history_immutable()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'bounty history is append-only'; END $$""")
    for table in APPEND_ONLY:
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION bounty_history_immutable()"
        )
    for table in (*APPEND_ONLY, "bounty_slots"):
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION bounty_history_immutable()"
        )
    # A slot keeps its identity forever; only its status moves.
    op.execute("""CREATE FUNCTION bounty_slot_status_only()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'bounty slots cannot be deleted';
          END IF;
          IF to_jsonb(NEW) - 'status' - 'status_changed_at'
             IS DISTINCT FROM to_jsonb(OLD) - 'status' - 'status_changed_at'
          THEN
            RAISE EXCEPTION 'only a bounty slot status can change';
          END IF;
          RETURN NEW;
        END $$""")
    op.execute(
        "CREATE TRIGGER bounty_slots_status_only BEFORE UPDATE OR DELETE "
        "ON bounty_slots FOR EACH ROW EXECUTE FUNCTION bounty_slot_status_only()"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT EXISTS (SELECT 1 FROM bounties)")).scalar():
        raise RuntimeError("refusing to drop published bounty history")
    for table in ("bounty_events", "bounty_slots", "bounty_rounds"):
        op.drop_table(table)
    op.drop_table("bounty_revisions")
    op.drop_table("bounties")
    op.execute("DROP FUNCTION bounty_history_immutable()")
    op.execute("DROP FUNCTION bounty_slot_status_only()")
