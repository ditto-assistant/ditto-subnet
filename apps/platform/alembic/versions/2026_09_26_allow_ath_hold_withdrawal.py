"""Allow a withdrawn resolution on precautionary ATH holds.

Revision ID: c4e8a1b27d90
Revises: 5d1f7a93c2e8
Create Date: 2026-09-24

``withdraw`` records that an operator removed an unsupported precautionary
hold. It is not a policy clear and not a reject.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c4e8a1b27d90"
down_revision: str | Sequence[str] | None = "5d1f7a93c2e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RESOLUTIONS = "('clear', 'reject', 'withdraw')"
_PRIOR_RESOLUTIONS = "('clear', 'reject')"
_ACTIONS = "('reopen', 'clear', 'reject', 'withdraw')"
_PRIOR_ACTIONS = "('reopen', 'clear', 'reject')"


def _lifecycle(resolutions: str) -> str:
    return (
        "(status = 'pending' AND resolved_at IS NULL AND resolved_by IS NULL "
        "AND resolution IS NULL AND resolution_reason IS NULL) OR "
        "(status = 'resolved' AND resolved_at IS NOT NULL "
        "AND resolved_by IS NOT NULL "
        "AND length(trim(resolved_by)) BETWEEN 1 AND 120 "
        "AND resolution IS NOT NULL "
        f"AND resolution IN {resolutions} "
        "AND resolution_reason IS NOT NULL "
        "AND length(trim(resolution_reason)) >= 3)"
    )


def upgrade() -> None:
    op.execute("ALTER TABLE ath_reviews DROP CONSTRAINT ath_reviews_resolution_check")
    op.execute(
        "ALTER TABLE ath_reviews ADD CONSTRAINT ath_reviews_resolution_check CHECK ("
        f"resolution IS NULL OR resolution IN {_RESOLUTIONS})"
    )
    op.execute("ALTER TABLE ath_reviews DROP CONSTRAINT ath_reviews_lifecycle_check")
    op.execute(
        "ALTER TABLE ath_reviews ADD CONSTRAINT ath_reviews_lifecycle_check CHECK ("
        f"{_lifecycle(_RESOLUTIONS)})"
    )
    op.execute(
        "ALTER TABLE ath_review_actions DROP CONSTRAINT ath_review_actions_action_check"
    )
    op.execute(
        "ALTER TABLE ath_review_actions "
        "ADD CONSTRAINT ath_review_actions_action_check "
        f"CHECK (action IN {_ACTIONS})"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE ath_review_actions DROP CONSTRAINT ath_review_actions_action_check"
    )
    op.execute(
        "ALTER TABLE ath_review_actions "
        "ADD CONSTRAINT ath_review_actions_action_check "
        f"CHECK (action IN {_PRIOR_ACTIONS})"
    )
    op.execute("ALTER TABLE ath_reviews DROP CONSTRAINT ath_reviews_lifecycle_check")
    op.execute(
        "ALTER TABLE ath_reviews ADD CONSTRAINT ath_reviews_lifecycle_check CHECK ("
        f"{_lifecycle(_PRIOR_RESOLUTIONS)})"
    )
    op.execute("ALTER TABLE ath_reviews DROP CONSTRAINT ath_reviews_resolution_check")
    op.execute(
        "ALTER TABLE ath_reviews ADD CONSTRAINT ath_reviews_resolution_check CHECK ("
        f"resolution IS NULL OR resolution IN {_PRIOR_RESOLUTIONS})"
    )
