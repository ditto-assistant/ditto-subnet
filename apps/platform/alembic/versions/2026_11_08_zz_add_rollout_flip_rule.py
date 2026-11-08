"""Freeze an operator flip rule onto each benchmark rollout.

Revision ID: b7e1f4a9c203
Revises: c4d8e2f1a937
Create Date: 2026-11-08

Bench bumps rescore a frozen cohort and the ledger flips to the new version only
when the priority cohort holds quorum AND five ranked owner families do. If
fewer agents get there, the old board is paid indefinitely. This adds the four
columns an optional flip rule needs, frozen at rollout START exactly like
``rescore_cohort_target``:

* ``flip_rule_enabled``            -- defaults ``false``; every existing row and
  every rollout started before an operator opts in keeps today's rule.
* ``flip_min_agents``              -- immediate threshold (3..5), default 5.
* ``flip_deadline_hours``          -- 1..168, default 36.
* ``flip_min_agents_at_deadline``  -- 3..5 and <= ``flip_min_agents``, default 3.

``benchmark_rollouts`` is a handful of rows and is not one of the hot tables, so
constant-default ``ADD COLUMN`` (metadata only on PostgreSQL 11+) is correct and
needs no ``safe_add_column`` batching. Nothing reads the columns while
``flip_rule_enabled`` is false, so the migration changes no behaviour.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b7e1f4a9c203"
down_revision = "c4d8e2f1a937"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "benchmark_rollouts",
        sa.Column(
            "flip_rule_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "benchmark_rollouts",
        sa.Column(
            "flip_min_agents",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("5"),
        ),
    )
    op.add_column(
        "benchmark_rollouts",
        sa.Column(
            "flip_deadline_hours",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("36"),
        ),
    )
    op.add_column(
        "benchmark_rollouts",
        sa.Column(
            "flip_min_agents_at_deadline",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("3"),
        ),
    )
    op.create_check_constraint(
        "benchmark_rollout_bounded_flip_min_agents",
        "benchmark_rollouts",
        "flip_min_agents BETWEEN 3 AND 5",
    )
    op.create_check_constraint(
        "benchmark_rollout_bounded_flip_deadline_agents",
        "benchmark_rollouts",
        "flip_min_agents_at_deadline BETWEEN 3 AND 5 "
        "AND flip_min_agents_at_deadline <= flip_min_agents",
    )
    op.create_check_constraint(
        "benchmark_rollout_bounded_flip_deadline_hours",
        "benchmark_rollouts",
        "flip_deadline_hours BETWEEN 1 AND 168",
    )


def downgrade() -> None:
    for name in (
        "benchmark_rollout_bounded_flip_deadline_hours",
        "benchmark_rollout_bounded_flip_deadline_agents",
        "benchmark_rollout_bounded_flip_min_agents",
    ):
        op.drop_constraint(name, "benchmark_rollouts", type_="check")
    for column in (
        "flip_min_agents_at_deadline",
        "flip_deadline_hours",
        "flip_min_agents",
        "flip_rule_enabled",
    ):
        op.drop_column("benchmark_rollouts", column)
