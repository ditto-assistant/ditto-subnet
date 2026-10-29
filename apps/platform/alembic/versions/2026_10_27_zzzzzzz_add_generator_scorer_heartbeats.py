"""add generator scorer heartbeats (advertised capability)

Revision ID: 8d2f4b6a1c37
Revises: 8f6a2c4e9b71
Create Date: 2026-10-27

The generator scorer advertises its version, build revision and the case
bounds its pack contract accepts on every work poll. Platform keeps the latest
per scorer so it can refuse work (and settings) the live scorer cannot run,
and so Backroom can show what is deployed. A stale scorer (MaxCases 64 while
cases_per_seed was 70) voided round 25968 and rejected a generator on
2026-10-27. One row per scorer host; not a hot table.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "8d2f4b6a1c37"
down_revision: str | Sequence[str] | None = "8f6a2c4e9b71"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "generator_scorer_heartbeats",
        sa.Column("scorer_id", sa.Text(), nullable=False),
        sa.Column("scorer_version", sa.Text(), nullable=False),
        sa.Column("min_cases", sa.Integer(), nullable=True),
        sa.Column("max_cases", sa.Integer(), nullable=True),
        sa.Column("sandbox", sa.Text(), nullable=True),
        sa.Column("build_revision", sa.Text(), nullable=True),
        sa.Column("last_poll", sa.Text(), nullable=False),
        sa.Column(
            "first_seen_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(min_cases IS NULL) = (max_cases IS NULL) "
            "AND (min_cases IS NULL OR (min_cases >= 1 AND max_cases >= min_cases))",
            name="generator_scorer_heartbeats_cases_check",
        ),
        sa.CheckConstraint(
            "last_poll IN ('qualification', 'round')",
            name="generator_scorer_heartbeats_poll_check",
        ),
        sa.PrimaryKeyConstraint("scorer_id"),
    )


def downgrade() -> None:
    op.drop_table("generator_scorer_heartbeats")
