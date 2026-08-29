"""add relay-owned provider outage circuits and lease park markers

Revision ID: c4f1a92e7b63
Revises: 8d2e1f4c9a70
Create Date: 2026-08-28 18:30:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c4f1a92e7b63"
down_revision: str | Sequence[str] | None = "8d2e1f4c9a70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE provider_outage_circuits (
            provider TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            epoch UUID NOT NULL,
            opened_at TIMESTAMPTZ NOT NULL,
            retry_at TIMESTAMPTZ NOT NULL,
            last_failure_at TIMESTAMPTZ NOT NULL,
            closed_at TIMESTAMPTZ,
            failure_count BIGINT NOT NULL DEFAULT 1,
            last_status INTEGER,
            last_error_code TEXT NOT NULL,
            probe_kind TEXT,
            probe_key TEXT,
            probe_expires_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT provider_outage_circuits_state_check
                CHECK (state IN ('open', 'closed')),
            CONSTRAINT provider_outage_circuits_failure_count_check
                CHECK (failure_count > 0),
            CONSTRAINT provider_outage_circuits_status_check
                CHECK (last_status IS NULL OR last_status BETWEEN 400 AND 599),
            CONSTRAINT provider_outage_circuits_probe_check CHECK (
                (probe_kind IS NULL AND probe_key IS NULL AND probe_expires_at IS NULL)
                OR
                (probe_kind IN ('scoring', 'screening')
                 AND probe_key IS NOT NULL
                 AND probe_expires_at IS NOT NULL)
            )
        )
        """
    )
    op.execute("ALTER TABLE validator_tickets ADD COLUMN provider_outage_epoch UUID")
    op.execute(
        "ALTER TABLE validator_tickets ADD COLUMN provider_outage_attempted_epoch UUID"
    )
    op.execute(
        "ALTER TABLE submission_source_reviews ADD COLUMN provider_outage_epoch UUID"
    )
    op.execute(
        "ALTER TABLE submission_source_reviews "
        "ADD COLUMN provider_outage_attempted_epoch UUID"
    )
    op.execute(
        "CREATE INDEX validator_tickets_provider_outage_idx "
        "ON validator_tickets (provider_outage_epoch) "
        "WHERE provider_outage_epoch IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX submission_source_reviews_provider_outage_idx "
        "ON submission_source_reviews (provider_outage_epoch) "
        "WHERE provider_outage_epoch IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS submission_source_reviews_provider_outage_idx")
    op.execute("DROP INDEX IF EXISTS validator_tickets_provider_outage_idx")
    op.execute(
        "ALTER TABLE submission_source_reviews "
        "DROP COLUMN IF EXISTS provider_outage_attempted_epoch"
    )
    op.execute(
        "ALTER TABLE submission_source_reviews "
        "DROP COLUMN IF EXISTS provider_outage_epoch"
    )
    op.execute(
        "ALTER TABLE validator_tickets "
        "DROP COLUMN IF EXISTS provider_outage_attempted_epoch"
    )
    op.execute(
        "ALTER TABLE validator_tickets DROP COLUMN IF EXISTS provider_outage_epoch"
    )
    op.execute("DROP TABLE IF EXISTS provider_outage_circuits")
