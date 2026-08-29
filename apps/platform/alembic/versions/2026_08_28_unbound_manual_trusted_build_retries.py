"""Remove the automatic-era cap from manual trusted-build retries.

Revision ID: f2d9c34a718e
Revises: e7a1c94b2d60
Create Date: 2026-08-28

Trusted image builds now make one provider attempt and park on failure. Every
later attempt requires an explicit Backroom action, so the historical cap of
ten automatic-era attempts would eventually make a parked build impossible to
retry manually. Keep the nonnegative ledger invariant without imposing a
limit on audited operator actions.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "f2d9c34a718e"
down_revision: str | Sequence[str] | None = "e7a1c94b2d60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "trusted_image_builds_attempt_count_check",
        "trusted_image_builds",
        type_="check",
    )
    op.create_check_constraint(
        "trusted_image_builds_attempt_count_check",
        "trusted_image_builds",
        "attempt_count >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "trusted_image_builds_attempt_count_check",
        "trusted_image_builds",
        type_="check",
    )
    op.create_check_constraint(
        "trusted_image_builds_attempt_count_check",
        "trusted_image_builds",
        "attempt_count BETWEEN 0 AND 10",
    )
