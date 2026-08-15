"""add durable validator name cache

Revision ID: f1a7b3c9d502
Revises: d94f2c7a1e08
Create Date: 2026-08-15
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "f1a7b3c9d502"
down_revision: str | Sequence[str] | None = "d94f2c7a1e08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    table = op.create_table(
        "validator_name_cache",
        sa.Column("validator_hotkey", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("stake_weight", sa.Float(), nullable=True),
        sa.Column("refreshed_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint(
            "display_name IS NULL OR length(display_name) BETWEEN 1 AND 80",
            name="validator_name_cache_display_name_check",
        ),
        sa.CheckConstraint(
            "stake_weight IS NULL OR stake_weight >= 0",
            name="validator_name_cache_stake_weight_check",
        ),
        sa.PrimaryKeyConstraint("validator_hotkey"),
    )
    op.bulk_insert(
        table,
        [
            {
                "validator_hotkey": hotkey,
                "display_name": name,
                "stake_weight": None,
                "refreshed_at": datetime(2026, 7, 31, 10, 56, 23, tzinfo=UTC),
            }
            for hotkey, name in {
                "5CFtzzb4vym9eysfeF9cxxp6D7gksuUVTKYNq1mchnrMs118": "Rizzo (Insured)",
                "5Cg3DiRfrgzB1XzN7VuqQNchTgZ8PzPbphMKmVvHobWSL118": (
                    "WildSage Labs (RT21)"
                ),
                "5CqJAjSjv8fjF9uAQpDLyfN1hZEvBjwpFgcGeLbYpcbSaD1C": (
                    "Yuma, a DCG Company"
                ),
                "5FU3YKmvVry2EVRUqzcdaTRAqW1E6nqPPPqmUGLe25JBmNDd": "TAO.com",
                "5HmP9732JFjnut2RY9yg4Gz2qJ38vF8xFwZb5dQVPF7FsmZz": "Ditto",
            }.items()
        ],
    )


def downgrade() -> None:
    op.drop_table("validator_name_cache")
