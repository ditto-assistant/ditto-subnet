"""Copy court family triage: triage revision and enforce-mode application.

A recommendation is unique per (review, settings revision, triage revision) so
the near-duplicate family triage re-triages holds the rules engine already
escalated under the same settings. ``application``/``applied_at`` record what
an enforce-mode court did with the recommendation.

Revision ID: 7c0f3e1a9b42
Revises: 3c5e7a9b2d41
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "7c0f3e1a9b42"
down_revision = "3c5e7a9b2d41"
branch_labels = None
depends_on = None

_TABLE = "ath_copy_court_recommendations"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            "triage_revision",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'rules-v1'"),
        ),
    )
    op.add_column(_TABLE, sa.Column("application", postgresql.JSONB(), nullable=True))
    op.add_column(
        _TABLE,
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ath_copy_court_recommendations_triage_revision_check",
        _TABLE,
        "length(trim(triage_revision)) BETWEEN 1 AND 64",
    )
    op.drop_constraint(
        "ath_copy_court_recommendations_review_revision_key", _TABLE, type_="unique"
    )
    op.create_unique_constraint(
        "ath_copy_court_recommendations_review_triage_key",
        _TABLE,
        ["review_id", "settings_revision", "triage_revision"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "ath_copy_court_recommendations_review_triage_key", _TABLE, type_="unique"
    )
    op.execute(
        f"DELETE FROM {_TABLE} WHERE triage_revision <> 'rules-v1'"  # noqa: S608
    )
    op.create_unique_constraint(
        "ath_copy_court_recommendations_review_revision_key",
        _TABLE,
        ["review_id", "settings_revision"],
    )
    op.drop_constraint(
        "ath_copy_court_recommendations_triage_revision_check", _TABLE, type_="check"
    )
    op.drop_column(_TABLE, "applied_at")
    op.drop_column(_TABLE, "application")
    op.drop_column(_TABLE, "triage_revision")
