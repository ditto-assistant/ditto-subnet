"""add operator-controlled validator queue policy settings

Three changes, one head:

* ``queue_policy_settings_revisions`` -- the append-only operator policy for the
  validator queue (cohort sizing, the fresh-vs-cohort lane split, the
  provisional contender lane, the previous-generation carryover gate),
  mirroring the other hot-swappable settings tables.
* ``benchmark_rollouts.rescore_cohort_target`` -- how many inherited agents this
  rollout set out to rescore, frozen at rollout START.
* ``benchmark_rollouts.priority_cohort_target`` -- how many top inherited
  positions gate this rollout's activation, also frozen at START.

Freezing both onto the row is what makes a later policy revision unable to
resize or re-gate an in-flight rollout, and what keeps a historical rollout
explainable by the shape it was actually built to.

The backfill keeps behavior byte-identical:

* ``priority_cohort_target`` backfills to 5 for every existing row, which is the
  hard-coded ``PRIORITY_COHORT_SIZE`` every rollout has ever been gated by --
  including any rollout that is open right now.
* ``rescore_cohort_target`` backfills to 10, the hard-coded
  ``DEFAULT_RESCORE_COHORT_SIZE``. The handful of legacy snapshots frozen at
  eleven-to-twenty-five members record their own size instead, which is the
  target they were actually built to. Either way ``_rollout_rescore_cohort``
  short-circuits on ``len(existing) >= target`` exactly as it did before.

Revision ID: c3f1a7d92e58
Revises: b7e6d5c4a3f2
Create Date: 2026-07-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c3f1a7d92e58"
down_revision: str | Sequence[str] | None = "b7e6d5c4a3f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "queue_policy_settings_revisions",
        sa.Column("revision", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("parent_revision", sa.Integer(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("settings", json_type, nullable=False),
        sa.Column("checksum", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("scope = '*'", name="queue_policy_settings_scope_check"),
        sa.CheckConstraint(
            "length(checksum) = 64", name="queue_policy_settings_checksum_check"
        ),
        sa.CheckConstraint(
            "parent_revision >= 0",
            name="queue_policy_settings_parent_revision_check",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) BETWEEN 8 AND 500",
            name="queue_policy_settings_reason_check",
        ),
        sa.CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="queue_policy_settings_actor_check",
        ),
        sa.PrimaryKeyConstraint("revision"),
        sa.UniqueConstraint(
            "scope",
            "parent_revision",
            name="queue_policy_settings_scope_parent_key",
        ),
    )
    op.create_index(
        "queue_policy_settings_scope_revision_idx",
        "queue_policy_settings_revisions",
        ["scope", "revision"],
        unique=True,
    )
    op.add_column(
        "benchmark_rollouts",
        sa.Column(
            "rescore_cohort_target",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("10"),
        ),
    )
    op.add_column(
        "benchmark_rollouts",
        sa.Column(
            "priority_cohort_target",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("5"),
        ),
    )
    # Legacy snapshots frozen above the default record their own size; plain
    # SQL so the statement is identical on postgres and the sqlite test engine.
    op.execute(
        "UPDATE benchmark_rollouts SET rescore_cohort_target = cohort_size "
        "WHERE cohort_size > 10"
    )
    op.create_check_constraint(
        "benchmark_rollout_bounded_rescore_target",
        "benchmark_rollouts",
        "rescore_cohort_target BETWEEN 5 AND 25",
    )
    op.create_check_constraint(
        "benchmark_rollout_bounded_priority_target",
        "benchmark_rollouts",
        "priority_cohort_target BETWEEN 5 AND 25",
    )


def downgrade() -> None:
    op.drop_constraint(
        "benchmark_rollout_bounded_priority_target",
        "benchmark_rollouts",
        type_="check",
    )
    op.drop_constraint(
        "benchmark_rollout_bounded_rescore_target",
        "benchmark_rollouts",
        type_="check",
    )
    op.drop_column("benchmark_rollouts", "priority_cohort_target")
    op.drop_column("benchmark_rollouts", "rescore_cohort_target")
    op.drop_index(
        "queue_policy_settings_scope_revision_idx",
        table_name="queue_policy_settings_revisions",
    )
    op.drop_table("queue_policy_settings_revisions")
