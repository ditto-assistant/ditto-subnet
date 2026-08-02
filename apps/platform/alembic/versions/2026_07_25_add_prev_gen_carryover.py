"""add previous-generation carryover adoption table

One new table, ``benchmark_rollout_carryover``: the durable record of which
stranded previous-generation submissions a benchmark era adopted.

Why a separate table instead of a ``kind`` column on
``benchmark_rollout_members``: every existing query that counts or lists rollout
members reads "row in ``benchmark_rollout_members``" as "member of the inherited
rescore cohort", and several of those queries gate ACTIVATION. Adding a
discriminator would require a ``kind`` filter on all of them, and missing one
would silently re-gate an open rollout on agents that were never in its cohort.
A separate table cannot be accidentally joined by a query that does not name it.

The row is also the admission credential for a carried-over submission, and the
application only ever inserts it in the same transaction that pins the agent's
desired-version ``benchmark_datasets`` row. Admission therefore cannot outrun
dataset generation.

Creating the table changes no behaviour: it ships empty, and the operator
setting that populates it (``prev_gen_carryover.enabled``) defaults to false.

Revision ID: d4a2b8e63f19
Revises: c3f1a7d92e58
Create Date: 2026-07-25
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d4a2b8e63f19"
down_revision: str | Sequence[str] | None = "c3f1a7d92e58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "benchmark_rollout_carryover",
        sa.Column("rollout_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("frozen_score_count", sa.Integer(), nullable=False),
        sa.Column("frozen_owner_key", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("rollout_id", "agent_id"),
        sa.UniqueConstraint("rollout_id", "position"),
        sa.ForeignKeyConstraint(
            ["rollout_id"],
            ["benchmark_rollouts.rollout_id"],
            ondelete="CASCADE",
        ),
        # RESTRICT, matching benchmark_rollout_members: an adopted submission's
        # agent row must not be deletable out from under its admission record.
        sa.ForeignKeyConstraint(["agent_id"], ["agents.agent_id"], ondelete="RESTRICT"),
        sa.CheckConstraint("position > 0", name="benchmark_carryover_position"),
        # A submission with three prior-era scores is finalized and by
        # definition not stranded, so an adopted row can only ever have 0-2.
        sa.CheckConstraint(
            "frozen_score_count BETWEEN 0 AND 2",
            name="benchmark_carryover_frozen_score_count",
        ),
    )


def downgrade() -> None:
    op.drop_table("benchmark_rollout_carryover")
