"""add authoritative validator ticket purpose

Revision ID: c3a71f9d4e82
Revises: b9e5d7f31c42
Create Date: 2026-07-23
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c3a71f9d4e82"
down_revision: str | None = "b9e5d7f31c42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "validator_tickets",
        sa.Column(
            "purpose",
            sa.Text(),
            nullable=False,
            server_default="legacy_unclassified",
        ),
    )
    op.add_column(
        "validator_tickets",
        sa.Column(
            "legacy_completion_allowed",
            sa.Boolean(),
            nullable=False,
            # Old replicas omit this column. Their short-lived leases may
            # complete during rolling overlap; purpose-aware writers explicitly
            # send FALSE and old-writer reissues are cleared by the guard.
            server_default=sa.true(),
        ),
    )
    op.execute(
        """
        UPDATE validator_tickets
        SET legacy_completion_allowed = TRUE
        WHERE status = 'issued'
        """
    )
    op.add_column(
        "validator_tickets",
        sa.Column(
            "purpose_revision",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_check_constraint(
        "validator_tickets_purpose_valid",
        "validator_tickets",
        "purpose IN ('legacy_unclassified', 'canonical_quorum', 'continual_retest')",
    )
    op.create_check_constraint(
        "validator_tickets_purpose_revision_nonnegative",
        "validator_tickets",
        "purpose_revision >= 0",
    )
    # Terminal leases can be classified from append-only evidence. Keep every
    # in-flight lease unclassified: old application replicas can continue to
    # issue rows during a rolling deploy, and guessing would let one scoring
    # endpoint consume the other lane's capability. New code waits for these
    # short-lived leases to expire and then reissues them with an explicit
    # purpose.
    op.execute(
        """
        UPDATE validator_tickets AS ticket
        SET purpose = 'continual_retest'
        WHERE ticket.status <> 'issued'
          AND EXISTS (
            SELECT 1
            FROM confirmation_scores AS confirmation
            WHERE confirmation.agent_id = ticket.agent_id
              AND confirmation.bench_version = ticket.bench_version
              AND confirmation.validator_hotkey = ticket.validator_hotkey
              AND confirmation.created_at >= ticket.issued_at
        )
        """
    )
    op.execute(
        """
        UPDATE validator_tickets AS ticket
        SET purpose = 'canonical_quorum'
        WHERE ticket.status <> 'issued'
          AND purpose = 'legacy_unclassified'
          AND EXISTS (
              SELECT 1
              FROM scores AS score
              WHERE score.agent_id = ticket.agent_id
                AND score.bench_version = ticket.bench_version
                AND score.validator_hotkey = ticket.validator_hotkey
                AND score.updated_at >= ticket.issued_at
          )
        """
    )
    # Old application replicas do not know either new column. Inserts receive
    # revision 0 from the server defaults. This guard also catches old-writer
    # reissues of an existing mutable row: a fresh issued_at without a revision
    # increment is forced back to the unclassified, non-consumable transition
    # state. Purpose-aware writers increment the revision on every new lease.
    op.execute(
        """
        CREATE FUNCTION guard_validator_ticket_purpose() RETURNS trigger AS $$
        BEGIN
            IF NEW.issued_at IS DISTINCT FROM OLD.issued_at
               AND NEW.purpose_revision = OLD.purpose_revision THEN
                NEW.purpose = 'legacy_unclassified';
                NEW.purpose_revision = 0;
                NEW.legacy_completion_allowed = TRUE;
            END IF;
            IF NEW.status = 'scored'
               AND OLD.status IS DISTINCT FROM 'scored'
               AND NEW.purpose_revision = 0
               AND NOT NEW.legacy_completion_allowed THEN
                RAISE EXCEPTION
                    'cannot score an unclassified validator ticket'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER validator_tickets_purpose_guard
        BEFORE UPDATE ON validator_tickets
        FOR EACH ROW
        EXECUTE FUNCTION guard_validator_ticket_purpose()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS validator_tickets_purpose_guard ON validator_tickets"
    )
    op.execute("DROP FUNCTION IF EXISTS guard_validator_ticket_purpose()")
    op.drop_constraint(
        "validator_tickets_purpose_revision_nonnegative",
        "validator_tickets",
        type_="check",
    )
    op.drop_constraint(
        "validator_tickets_purpose_valid",
        "validator_tickets",
        type_="check",
    )
    op.drop_column("validator_tickets", "purpose")
    op.drop_column("validator_tickets", "purpose_revision")
    op.drop_column("validator_tickets", "legacy_completion_allowed")
