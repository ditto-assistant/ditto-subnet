"""Append-only V13 scorer packet rotations.

Revision ID: a40f7d9c621e
Revises: c47d8b9e204a
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a40f7d9c621e"
down_revision: str | Sequence[str] | None = "c47d8b9e204a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "v13_scorer_cohort_rotations",
        sa.Column("rotation_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("bench_version", sa.Integer(), nullable=False),
        sa.Column("hotkeys", postgresql.JSONB(), nullable=False),
        sa.Column("packet", postgresql.JSONB(), nullable=False),
        sa.Column("previous_packet", postgresql.JSONB(), nullable=False),
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
        sa.CheckConstraint(
            "bench_version = 13", name="v13_scorer_rotation_version_check"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(hotkeys) = 'array' AND jsonb_array_length(hotkeys) = 3",
            name="v13_scorer_rotation_three_hotkeys_check",
        ),
        sa.CheckConstraint(
            "length(slot_settings_checksum) = 64",
            name="v13_scorer_rotation_settings_checksum_check",
        ),
    )
    op.execute(
        """CREATE TRIGGER v13_scorer_rotation_immutable
        BEFORE UPDATE OR DELETE ON v13_scorer_cohort_rotations
        FOR EACH ROW EXECUTE FUNCTION reject_v13_scorer_pin_mutation()"""
    )
    op.execute(
        """CREATE OR REPLACE FUNCTION enforce_v13_ticket_cohort() RETURNS trigger
        LANGUAGE plpgsql AS $$ DECLARE pinned jsonb; BEGIN
          IF NEW.bench_version = 13 AND NEW.status = 'issued' THEN
            PERFORM pg_advisory_xact_lock(
              hashtextextended('ditto:validator-rollout-dispatch:v1', 0)
            );
            SELECT hotkeys INTO pinned FROM v13_scorer_cohort_rotations
              WHERE bench_version = 13 ORDER BY rotation_id DESC LIMIT 1;
            IF pinned IS NULL THEN
              SELECT hotkeys INTO pinned FROM v13_scorer_cohort_pins
                WHERE bench_version = 13;
            END IF;
            IF pinned IS NOT NULL AND NOT (pinned ? NEW.validator_hotkey) THEN
              RAISE EXCEPTION 'V13 ticket validator is outside pinned scorer cohort';
            END IF;
          END IF;
          RETURN NEW;
        END $$"""
    )


def downgrade() -> None:
    op.execute(
        """DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM v13_scorer_cohort_rotations LIMIT 1) THEN
            RAISE EXCEPTION 'cannot downgrade V13 scorer cohort with rotation history';
          END IF;
        END $$"""
    )
    op.execute(
        "DROP TRIGGER v13_scorer_rotation_immutable ON v13_scorer_cohort_rotations"
    )
    op.drop_table("v13_scorer_cohort_rotations")
    op.execute(
        """CREATE OR REPLACE FUNCTION enforce_v13_ticket_cohort() RETURNS trigger
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
