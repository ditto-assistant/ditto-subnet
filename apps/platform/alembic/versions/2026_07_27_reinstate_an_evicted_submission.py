"""make an operator eviction reversible

Revision ID: c7a4f1e2b903
Revises: a7c14f8bd260
Create Date: 2026-07-27

Two changes, both on cold, operator-write-only tables.

``validator_queue_withdrawals`` gains ``reinstated_at``: a handful of rows and no
hot writers (the same reasoning ``2026_07_27_record_evicted_leases_on_queue_removal``
recorded), so a plain ``add_column`` is correct here and the ``safe_add_column``
machinery -- which exists for ``validator_tickets``, ``inference_grants`` and
``inference_requests`` -- is not needed. Nullable forever: ``NULL`` means "this
removal is still in force", which is the true state of every row already in the
table, so there is nothing to backfill.

Its ``(agent_id, bench_version)`` unique constraint becomes a *partial* unique
index over the in-force rows only. Existing rows all have ``reinstated_at IS
NULL``, so the index covers exactly what the constraint covered; what it adds is
the ability to evict a submission again after a reinstatement, without which the
first reversal would permanently disarm the eviction lever for that era.

``validator_queue_reinstatements`` is new: one append-only row per reversal,
carrying its own actor, reason, snapshot and idempotency key. The eviction row it
resolves is never deleted -- ``GET /admin/lease-revocations?action=operator_evicted``
reads the lease audit rows that eviction wrote, and an eviction that was later
reversed is a fact worth keeping.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c7a4f1e2b903"
down_revision: str | None = "a7c14f8bd260"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column(
        "validator_queue_withdrawals",
        sa.Column("reinstated_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.drop_constraint(
        "validator_queue_withdrawals_agent_bench_key",
        "validator_queue_withdrawals",
        type_="unique",
    )
    op.create_index(
        "validator_queue_withdrawals_agent_bench_key",
        "validator_queue_withdrawals",
        ["agent_id", "bench_version"],
        unique=True,
        postgresql_where=sa.text("reinstated_at IS NULL"),
    )
    op.create_table(
        "validator_queue_reinstatements",
        sa.Column("reinstatement_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("withdrawal_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("bench_version", sa.Integer(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("expected_snapshot", sa.Text(), nullable=False),
        sa.Column("score_count", sa.Integer(), nullable=False),
        sa.Column("ticket_snapshot", _JSON, nullable=False),
        sa.Column("retry_budget_snapshot", _JSON, nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("reinstatement_id"),
        sa.ForeignKeyConstraint(
            ["withdrawal_id"],
            ["validator_queue_withdrawals.withdrawal_id"],
            ondelete="RESTRICT",
            name="validator_queue_reinstatements_withdrawal_id_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.agent_id"],
            ondelete="RESTRICT",
            name="validator_queue_reinstatements_agent_id_fkey",
        ),
        sa.CheckConstraint(
            "bench_version > 0",
            name="validator_queue_reinstatements_bench_version_positive",
        ),
        sa.CheckConstraint(
            "score_count >= 0",
            name="validator_queue_reinstatements_score_count_nonnegative",
        ),
        sa.CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="validator_queue_reinstatements_actor_length",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) BETWEEN 8 AND 500",
            name="validator_queue_reinstatements_reason_length",
        ),
        sa.UniqueConstraint(
            "withdrawal_id",
            name="validator_queue_reinstatements_withdrawal_key",
        ),
    )
    op.create_index(
        "validator_queue_reinstatements_agent_idx",
        "validator_queue_reinstatements",
        ["agent_id", "bench_version", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "validator_queue_reinstatements_agent_idx",
        table_name="validator_queue_reinstatements",
    )
    op.drop_table("validator_queue_reinstatements")
    op.drop_index(
        "validator_queue_withdrawals_agent_bench_key",
        table_name="validator_queue_withdrawals",
    )
    # Reversing the partial index needs the table to hold at most one removal per
    # (agent, era) again. Reinstated rows are exactly the ones a total constraint
    # cannot admit, and the reinstatement rows that explain them are gone by this
    # point, so they are dropped rather than left to fail the constraint.
    op.execute(
        "DELETE FROM validator_queue_withdrawals WHERE reinstated_at IS NOT NULL"
    )
    op.create_unique_constraint(
        "validator_queue_withdrawals_agent_bench_key",
        "validator_queue_withdrawals",
        ["agent_id", "bench_version"],
    )
    op.drop_column("validator_queue_withdrawals", "reinstated_at")
