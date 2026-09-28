"""Drop the reason-code allowlist on audited screening quarantines.

A signed policy-13 INCONCLUSIVE verdict may carry its review audit under any
deciding evidence code (``l2-runtime-evidence-unavailable``,
``source-review-*-budget-exhausted``, ``behavioral-oracle-passed``, ...). The
allowlist admitted only six of them, so every other audited verdict 500ed on
INSERT and the attempt was later swept as ``worker-lease-orphaned``.
``screening_quarantines_reason_code_check`` still bounds every code's shape,
and deferred-review softening stays gated on its own exact code sets.

Revision ID: ece2a682e74c
Revises: a2f4d9c51e60
"""

from collections.abc import Sequence

from alembic import op

revision: str = "ece2a682e74c"
down_revision: str | Sequence[str] | None = "a2f4d9c51e60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "screening_quarantines_review_audit_reason_check",
        "screening_quarantines",
        type_="check",
    )


def downgrade() -> None:
    # The restored allowlist intentionally makes downgrade fail rather than
    # rewrite audited rows retained under any other reason code.
    op.create_check_constraint(
        "screening_quarantines_review_audit_reason_check",
        "screening_quarantines",
        "review_audit IS NULL OR reason_code IN "
        "('source-review-inconclusive', 'agentic-source-review-tripwire', "
        "'l2-model-inconclusive', 'l2-model-total-budget', "
        "'l2-model-tool-budget', 'l2-model-step-budget')",
    )
