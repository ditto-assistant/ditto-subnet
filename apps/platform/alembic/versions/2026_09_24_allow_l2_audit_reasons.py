"""Retain signed L2 inconclusive audits under their exact reason codes."""

from collections.abc import Sequence

from alembic import op

revision: str = "a6e1c7d9420b"
down_revision: str | Sequence[str] | None = "9d8c2e4a7f10"
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
        "('source-review-inconclusive', 'agentic-source-review-tripwire', "
        "'l2-model-inconclusive', 'l2-model-total-budget', "
        "'l2-model-tool-budget', 'l2-model-step-budget')",
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
        "review_audit IS NULL OR reason_code IN "
        "('source-review-inconclusive', 'agentic-source-review-tripwire')",
    )
