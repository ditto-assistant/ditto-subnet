"""Allow a withdrawn resolution on precautionary ATH holds.

Revision ID: c4e8a1b27d90
Revises: 5e2a8c4f9d17
Create Date: 2026-09-29

``withdraw`` records that an operator removed an unsupported precautionary
hold. It is not a policy clear and not a reject.

Downgrade restores the two-value constraints and therefore fails while any
``ath_reviews.resolution = 'withdraw'`` or ``ath_review_actions.action =
'withdraw'`` row exists. That is deliberate: rewriting a withdrawal into a
clear would certify an artifact nobody certified, rewriting it into a reject
would record a violation nobody found, and deleting the ledger row would erase
an audited operator action. An operator who must downgrade has to settle each
withdrawn review first (reopen, then clear or reject) and decide what happens
to its ``withdraw`` ledger rows; the migration will not choose for them.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c4e8a1b27d90"
down_revision: str | Sequence[str] | None = "5e2a8c4f9d17"
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
    # The restored constraints intentionally make downgrade fail rather than
    # rewrite or delete withdrawn reviews and their audited ledger rows. See
    # the module docstring.
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
