"""add generator scorer advertised grading versions

Revision ID: 3c5e7a9b2d41
Revises: 8d2f4b6a1c37
Create Date: 2026-10-27

The generator scorer now advertises the pack grading policies it supports
(``grading_bench_versions``) on every work poll, explicitly, the way a
validator advertises ``generator-round-bench-<n>``. Platform opens and leases a
round at version N, and requests a qualification at N, only for a scorer that
lists N. NULL is a scorer that predates the list: treated as v13-only.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "3c5e7a9b2d41"
down_revision: str | Sequence[str] | None = "8d2f4b6a1c37"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "generator_scorer_heartbeats",
        sa.Column(
            "grading_bench_versions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "generator_scorer_heartbeats_grading_versions_check",
        "generator_scorer_heartbeats",
        "grading_bench_versions IS NULL "
        "OR (jsonb_typeof(grading_bench_versions) = 'array' "
        "AND jsonb_array_length(grading_bench_versions) BETWEEN 1 AND 16)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "generator_scorer_heartbeats_grading_versions_check",
        "generator_scorer_heartbeats",
        type_="check",
    )
    op.drop_column("generator_scorer_heartbeats", "grading_bench_versions")
