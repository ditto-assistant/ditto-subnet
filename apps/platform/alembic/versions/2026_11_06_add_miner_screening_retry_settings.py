"""Add the operator control for miner self-serve screening retries (SN-456).

Revision ID: 9d4b2f7c1e63
Revises: 4c1e7a9d2b58
Create Date: 2026-11-06

The genesis row enables the route with one self-serve retry per agent. That is
the same bound Platform already accepts for ``l2-late-result`` (an artifact can
cost at most one extra screen), and every agent behind it paid an upload fee.
Operators pause it with one audited revision.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "9d4b2f7c1e63"
down_revision: str | Sequence[str] | None = "4c1e7a9d2b58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "miner_screening_retry_settings_revisions",
        sa.Column("revision", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("parent_revision", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("max_retries_per_agent", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "parent_revision >= 0",
            name="miner_screening_retry_settings_parent_revision_check",
        ),
        sa.CheckConstraint(
            "max_retries_per_agent BETWEEN 0 AND 3",
            name="miner_screening_retry_settings_max_retries_check",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) >= 8",
            name="miner_screening_retry_settings_reason_check",
        ),
        sa.CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="miner_screening_retry_settings_actor_check",
        ),
        sa.PrimaryKeyConstraint("revision"),
        sa.UniqueConstraint(
            "parent_revision",
            name="miner_screening_retry_settings_parent_revision_key",
        ),
    )
    settings = sa.table(
        "miner_screening_retry_settings_revisions",
        sa.column("parent_revision", sa.Integer()),
        sa.column("enabled", sa.Boolean()),
        sa.column("max_retries_per_agent", sa.Integer()),
        sa.column("reason", sa.Text()),
        sa.column("actor", sa.Text()),
    )
    op.bulk_insert(
        settings,
        [
            {
                "parent_revision": 0,
                "enabled": True,
                "max_retries_per_agent": 1,
                "reason": "Let owners retry one interrupted screening per agent",
                "actor": "migration",
            }
        ],
    )


def downgrade() -> None:
    op.drop_table("miner_screening_retry_settings_revisions")
