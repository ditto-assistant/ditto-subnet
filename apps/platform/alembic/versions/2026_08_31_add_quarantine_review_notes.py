"""Persist signed source-review note ledgers on quarantine evidence.

Revision ID: c8e2a5d7f491
Revises: f7d2c9a4e681
Create Date: 2026-08-31

The screener records bounded, public-safe notes as a source review progresses.
They are submitted with the terminal verdict and hash to a digest included in
the verdict signature. Retaining both makes a timeout or final adjudication
auditable without retaining source, prompts, or challenge values.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c8e2a5d7f491"
down_revision: str | Sequence[str] | None = "f7d2c9a4e681"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add nullable note evidence for new verdicts without rewriting history."""
    op.add_column(
        "screening_quarantines",
        sa.Column("review_notes_digest", sa.Text(), nullable=True),
    )
    op.add_column(
        "screening_quarantines",
        sa.Column(
            "review_notes",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "screening_quarantines_review_notes_digest_check",
        "screening_quarantines",
        "review_notes_digest IS NULL OR review_notes_digest ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        "screening_quarantines_review_notes_pair_check",
        "screening_quarantines",
        "(review_notes IS NULL) = (review_notes_digest IS NULL)",
    )


def downgrade() -> None:
    """Remove note evidence fields in reverse dependency order."""
    op.drop_constraint(
        "screening_quarantines_review_notes_pair_check",
        "screening_quarantines",
        type_="check",
    )
    op.drop_constraint(
        "screening_quarantines_review_notes_digest_check",
        "screening_quarantines",
        type_="check",
    )
    op.drop_column("screening_quarantines", "review_notes")
    op.drop_column("screening_quarantines", "review_notes_digest")
