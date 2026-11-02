"""add per-submission shadow flag on agents

A shadow submission is graded and screened like any other but never ranks on
the public leaderboard, never earns validator weight, and never counts toward a
bench-version authority quorum. The ranking machinery reads it as
``eligible = False``; the column is the owner-set dial that forces that.

``agents`` is not a hot table (the hot tables are ``inference_requests``,
``inference_grants`` and ``validator_tickets``), so a plain metadata-only
``add_column`` with a constant server default is safe: Postgres records the
default in the catalog without rewriting the table.

Revision ID: 653d95402c0f
Revises: b3f926ce145a
Create Date: 2026-09-07
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "653d95402c0f"
down_revision: str | Sequence[str] | None = "b3f926ce145a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column(
            "shadow",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("agents", "shadow")
