"""Add infra_retry_grants to validator_tickets.

Revision ID: a1c7e93f2b40
Revises: f8c2d6a04e71
Create Date: 2026-07-20 21:00:00.000000

A lease that fails on validator-side infrastructure (a signed ``fail_job`` with
reason ``infrastructure``) earns an ``infra_retry_grant`` that offsets the
``attempt_count`` increment the reissue adds, so an infrastructure outage never
spends the agent's genuine per-version attempt budget. ``NOT NULL DEFAULT 0`` so
every existing ticket keeps its current budget unchanged.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a1c7e93f2b40"
down_revision: str | Sequence[str] | None = "f8c2d6a04e71"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "validator_tickets",
        sa.Column(
            "infra_retry_grants",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_check_constraint(
        "validator_tickets_infra_retry_grants_nonnegative",
        "validator_tickets",
        "infra_retry_grants >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "validator_tickets_infra_retry_grants_nonnegative",
        "validator_tickets",
        type_="check",
    )
    op.drop_column("validator_tickets", "infra_retry_grants")
