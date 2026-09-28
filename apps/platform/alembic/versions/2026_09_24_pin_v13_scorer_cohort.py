"""Pin the V13 scorer cohort and signed runtime packet before L2 authority.

Revision ID: c47d8b9e204a
Revises: e9c24a7b135d
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c47d8b9e204a"
down_revision: str | Sequence[str] | None = "e9c24a7b135d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "v13_scorer_cohort_pins",
        sa.Column("bench_version", sa.Integer(), primary_key=True),
        sa.Column("hotkeys", postgresql.JSONB(), nullable=False),
        sa.Column("packet", postgresql.JSONB(), nullable=False),
        sa.Column("slot_settings_revision", sa.Integer(), nullable=False),
        sa.Column("slot_settings_checksum", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("bench_version = 13", name="v13_scorer_pin_version_check"),
        sa.CheckConstraint(
            "jsonb_typeof(hotkeys) = 'array' AND jsonb_array_length(hotkeys) = 3",
            name="v13_scorer_pin_three_hotkeys_check",
        ),
        sa.CheckConstraint(
            "length(slot_settings_checksum) = 64",
            name="v13_scorer_pin_settings_checksum_check",
        ),
    )
    op.execute(
        """CREATE FUNCTION reject_v13_scorer_pin_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
          RAISE EXCEPTION 'v13 scorer cohort pins are immutable';
        END $$"""
    )
    op.execute(
        """CREATE TRIGGER v13_scorer_pin_immutable
        BEFORE UPDATE OR DELETE ON v13_scorer_cohort_pins
        FOR EACH ROW EXECUTE FUNCTION reject_v13_scorer_pin_mutation()"""
    )
    op.execute(
        """CREATE FUNCTION enforce_v13_ticket_cohort() RETURNS trigger
        LANGUAGE plpgsql AS $$ DECLARE pinned jsonb; BEGIN
          IF NEW.bench_version = 13 AND NEW.status = 'issued' THEN
            PERFORM pg_advisory_xact_lock(
              hashtextextended('ditto:validator-rollout-dispatch:v1', 0)
            );
            SELECT hotkeys INTO pinned FROM v13_scorer_cohort_pins
              WHERE bench_version = 13;
            IF pinned IS NOT NULL AND NOT (pinned ? NEW.validator_hotkey) THEN
              RAISE EXCEPTION 'V13 ticket validator is outside pinned scorer cohort';
            END IF;
          END IF;
          RETURN NEW;
        END $$"""
    )
    op.execute(
        """CREATE TRIGGER v13_ticket_cohort_gate
        BEFORE INSERT OR UPDATE ON validator_tickets
        FOR EACH ROW EXECUTE FUNCTION enforce_v13_ticket_cohort()"""
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER v13_ticket_cohort_gate ON validator_tickets")
    op.execute("DROP FUNCTION enforce_v13_ticket_cohort()")
    op.execute("DROP TRIGGER v13_scorer_pin_immutable ON v13_scorer_cohort_pins")
    op.execute("DROP FUNCTION reject_v13_scorer_pin_mutation()")
    op.drop_table("v13_scorer_cohort_pins")
