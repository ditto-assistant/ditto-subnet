"""add append-only ATH review action history

Revision ID: c91f4e7a2b60
Revises: b7c1e9d4a2f8
Create Date: 2026-07-17 03:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c91f4e7a2b60"
down_revision: str | Sequence[str] | None = "b7c1e9d4a2f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE ath_reviews ADD COLUMN reopened_at TIMESTAMPTZ")
    op.execute(
        """
        CREATE TABLE ath_review_actions (
            action_id UUID PRIMARY KEY,
            review_id UUID NOT NULL REFERENCES ath_reviews(review_id)
                ON DELETE CASCADE,
            action TEXT NOT NULL CHECK (action IN ('reopen','clear','reject')),
            reason TEXT NOT NULL CHECK (length(trim(reason)) BETWEEN 3 AND 500),
            actor TEXT NOT NULL CHECK (length(trim(actor)) BETWEEN 1 AND 120),
            evidence JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ath_review_actions_review_created_idx "
        "ON ath_review_actions(review_id, created_at, action_id)"
    )
    op.execute(
        """
        INSERT INTO ath_review_actions (
            action_id, review_id, action, reason, actor, evidence, created_at
        )
        SELECT review_id, review_id, resolution,
               resolution_reason, resolved_by,
               jsonb_build_object(
                   'previous_status', original_evidence->>'previous_status'
               ),
               resolved_at
        FROM ath_reviews
        WHERE status = 'resolved'
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ath_review_actions")
    op.execute("ALTER TABLE ath_reviews DROP COLUMN reopened_at")
