"""remove the ATH review reason upper bound

Revision ID: b9d0e1f2a3c4
Revises: a8c9d0e1f2b3
Create Date: 2026-08-04 01:15:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b9d0e1f2a3c4"
down_revision: str | Sequence[str] | None = "a8c9d0e1f2b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Both constraints predate the metadata naming convention and were created
    # by raw SQL, so preserve their exact production names here.
    op.execute("ALTER TABLE ath_reviews DROP CONSTRAINT ath_reviews_lifecycle_check")
    op.execute(
        "ALTER TABLE ath_reviews ADD CONSTRAINT ath_reviews_lifecycle_check CHECK ("
        "(status = 'pending' AND resolved_at IS NULL AND resolved_by IS NULL "
        "AND resolution IS NULL AND resolution_reason IS NULL) OR "
        "(status = 'resolved' AND resolved_at IS NOT NULL "
        "AND resolved_by IS NOT NULL "
        "AND length(trim(resolved_by)) BETWEEN 1 AND 120 "
        "AND resolution IS NOT NULL AND resolution IN ('clear', 'reject') "
        "AND resolution_reason IS NOT NULL "
        "AND length(trim(resolution_reason)) >= 3))"
    )
    op.execute(
        "ALTER TABLE ath_review_actions DROP CONSTRAINT ath_review_actions_reason_check"
    )
    op.execute(
        "ALTER TABLE ath_review_actions "
        "ADD CONSTRAINT ath_review_actions_reason_check "
        "CHECK (length(trim(reason)) >= 3)"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE ath_review_actions DROP CONSTRAINT ath_review_actions_reason_check"
    )
    op.execute(
        "ALTER TABLE ath_review_actions "
        "ADD CONSTRAINT ath_review_actions_reason_check "
        "CHECK (length(trim(reason)) BETWEEN 3 AND 500)"
    )
    op.execute("ALTER TABLE ath_reviews DROP CONSTRAINT ath_reviews_lifecycle_check")
    op.execute(
        "ALTER TABLE ath_reviews ADD CONSTRAINT ath_reviews_lifecycle_check CHECK ("
        "(status = 'pending' AND resolved_at IS NULL AND resolved_by IS NULL "
        "AND resolution IS NULL AND resolution_reason IS NULL) OR "
        "(status = 'resolved' AND resolved_at IS NOT NULL "
        "AND resolved_by IS NOT NULL "
        "AND length(trim(resolved_by)) BETWEEN 1 AND 120 "
        "AND resolution IS NOT NULL AND resolution IN ('clear', 'reject') "
        "AND resolution_reason IS NOT NULL "
        "AND length(trim(resolution_reason)) BETWEEN 3 AND 500))"
    )
