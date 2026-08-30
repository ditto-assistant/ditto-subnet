"""bind an exact retry to an immutable adjudicator review revision

Revision ID: 4f2b7d9a1e63
Revises: d4e9a1c7b258
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "4f2b7d9a1e63"
down_revision: str | Sequence[str] | None = "d4e9a1c7b258"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "screening_retry_overrides",
        sa.Column("review_settings_revision", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "screening_retry_overrides_review_settings_revision_fkey",
        "screening_retry_overrides",
        "screener_review_settings_revisions",
        ["review_settings_revision"],
        ["revision"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "screening_retry_overrides_review_settings_full_review_check",
        "screening_retry_overrides",
        "review_settings_revision IS NULL OR force_full_review",
    )


def downgrade() -> None:
    op.drop_constraint(
        "screening_retry_overrides_review_settings_full_review_check",
        "screening_retry_overrides",
        type_="check",
    )
    op.drop_constraint(
        "screening_retry_overrides_review_settings_revision_fkey",
        "screening_retry_overrides",
        type_="foreignkey",
    )
    op.drop_column("screening_retry_overrides", "review_settings_revision")
