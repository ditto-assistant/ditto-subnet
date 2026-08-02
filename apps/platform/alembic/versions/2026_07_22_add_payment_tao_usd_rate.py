"""Record the TAO/USD rate used to verify each miner payment.

Revision ID: e3f5a7b91c24
Revises: f1a2c3d4e5b6
Create Date: 2026-07-22 00:30:00.000000

Legacy rows remain null until a separately audited historical-price backfill.
New writes persist the already-fetched verifier rate, so this column adds no
oracle request and no payment-path availability dependency.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e3f5a7b91c24"
down_revision: str | Sequence[str] | None = "f1a2c3d4e5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "evaluation_payments",
        sa.Column("tao_usd_rate", sa.Numeric(precision=20, scale=8), nullable=True),
    )
    op.create_check_constraint(
        "evaluation_payments_tao_usd_rate_positive",
        "evaluation_payments",
        "tao_usd_rate IS NULL OR tao_usd_rate > 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "evaluation_payments_tao_usd_rate_positive",
        "evaluation_payments",
        type_="check",
    )
    op.drop_column("evaluation_payments", "tao_usd_rate")
