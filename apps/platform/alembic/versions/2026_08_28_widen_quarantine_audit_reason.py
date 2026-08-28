"""Allow reviewer budget audits on tripwire quarantines.

The Platform-attested Targon/Cloud Run court quarantines with reason
``agentic-source-review-tripwire`` and ships the reviewer's budget audit as
operator evidence. The 2026-08-02 check only admitted audits on
``source-review-inconclusive`` rows, so every verdict-bearing platform-attested
review 500ed on INSERT and the attempt burned forever.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "f2a91c48d7e5"
down_revision: str | Sequence[str] | None = "6570642aba4a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "screening_quarantines_review_audit_reason_check",
        "screening_quarantines",
        type_="check",
    )
    op.create_check_constraint(
        "screening_quarantines_review_audit_reason_check",
        "screening_quarantines",
        "review_audit IS NULL OR reason_code IN "
        "('source-review-inconclusive', 'agentic-source-review-tripwire')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "screening_quarantines_review_audit_reason_check",
        "screening_quarantines",
        type_="check",
    )
    op.create_check_constraint(
        "screening_quarantines_review_audit_reason_check",
        "screening_quarantines",
        "review_audit IS NULL OR reason_code = 'source-review-inconclusive'",
    )
