"""Bound report-only verification replay leases across slow isolated builds.

Revision ID: 5c7e219a0d4b
Revises: 6c81f4d239ba
Create Date: 2026-09-23
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "5c7e219a0d4b"
down_revision: str | Sequence[str] | None = "6c81f4d239ba"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "screening_verification_replays",
        sa.Column("lease_started_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "screening_verification_replays",
        sa.Column("lease_renewals", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "svrp_lease_renewals_check",
        "screening_verification_replays",
        "lease_renewals BETWEEN 0 AND 8",
    )


def downgrade() -> None:
    op.drop_constraint(
        "svrp_lease_renewals_check", "screening_verification_replays", type_="check"
    )
    op.drop_column("screening_verification_replays", "lease_renewals")
    op.drop_column("screening_verification_replays", "lease_started_at")
