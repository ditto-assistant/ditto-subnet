"""Persist signed public-safe screener review budget audits."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d71e3f901a2b"
down_revision: str | Sequence[str] | None = "d4f6a8b1c2e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "screening_quarantines",
        sa.Column("review_audit_digest", sa.Text(), nullable=True),
    )
    op.add_column(
        "screening_quarantines",
        sa.Column(
            "review_audit",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "screening_quarantines_review_audit_digest_check",
        "screening_quarantines",
        "review_audit_digest IS NULL OR review_audit_digest ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        "screening_quarantines_review_audit_pair_check",
        "screening_quarantines",
        "(review_audit IS NULL) = (review_audit_digest IS NULL)",
    )
    op.create_check_constraint(
        "screening_quarantines_review_audit_reason_check",
        "screening_quarantines",
        "review_audit IS NULL OR reason_code = 'source-review-inconclusive'",
    )


def downgrade() -> None:
    op.drop_constraint(
        "screening_quarantines_review_audit_reason_check",
        "screening_quarantines",
        type_="check",
    )
    op.drop_constraint(
        "screening_quarantines_review_audit_pair_check",
        "screening_quarantines",
        type_="check",
    )
    op.drop_constraint(
        "screening_quarantines_review_audit_digest_check",
        "screening_quarantines",
        type_="check",
    )
    op.drop_column("screening_quarantines", "review_audit")
    op.drop_column("screening_quarantines", "review_audit_digest")
