"""add a durable audit row per served artifact

Before this table the only trace of an artifact fetch was a stdout line in the
validator endpoint. Nothing in the database recorded that miner source had been
handed to anyone, so a leak could be observed in its consequences -- a tarball
resubmitted under a third-party hotkey -- and never attributed to a fetcher.

``artifact_fetch_audit`` is that record, written by every path that serves
artifact bytes or a presigned URL to them: the public, validator and screener
artifact routes, the admin screening artifact / source-file routes, and the
admin copy-review source-diff routes.

Shape notes:

* No FK to ``agents``. The record must outlive the submission it describes; an
  agent row may be pruned and the history of who read its source must not
  cascade away. ``score_audit_log`` is unbound for the same reason.
* No hash chain. ``score_audit_log`` serializes every append behind a
  ``FOR UPDATE`` head lock to maintain one linear chain, which is right for a
  public tamper-evident score projection and wrong for a read-path audit, where
  it would put concurrent artifact fetches in a queue. Plain INSERTs here.
* Three indexes, one per question an incident actually asks: who fetched *this
  agent's* artifact, what did *this requester* fetch, and what was fetched
  *during this window*.

Creating a new table takes no lock on any hot table, so this is safe to apply
under load; ``alembic/env.py`` still bounds it with the ambient ``lock_timeout``.

Revision ID: e8b3c05d7a41
Revises: f3b6a80c95d1
Create Date: 2026-07-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e8b3c05d7a41"
down_revision: str | Sequence[str] | None = "f3b6a80c95d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "artifact_fetch_audit",
        sa.Column(
            "seq",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("requester_kind", sa.Text(), nullable=False),
        sa.Column("requester_id", sa.Text(), nullable=True),
        sa.Column("requester_instance_id", sa.Text(), nullable=True),
        sa.Column("lease_id", sa.Uuid(), nullable=True),
        sa.Column("bench_version", sa.Integer(), nullable=True),
        sa.Column("artifact_sha256", sa.Text(), nullable=True),
        sa.Column("source_ip", sa.Text(), nullable=True),
        sa.Column(
            "detail",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
        sa.Column(
            "fetched_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("seq"),
        sa.CheckConstraint(
            "requester_kind IN ('validator', 'screener', 'admin', 'public')",
            name="artifact_fetch_audit_requester_kind_check",
        ),
        # The public artifact route is unauthenticated, so it is the one kind
        # with no identity to record; every other kind must carry one.
        sa.CheckConstraint(
            "(requester_kind = 'public') = (requester_id IS NULL)",
            name="artifact_fetch_audit_requester_id_presence_check",
        ),
        sa.CheckConstraint(
            "bench_version IS NULL OR bench_version > 0",
            name="artifact_fetch_audit_bench_version_check",
        ),
    )
    op.create_index(
        "artifact_fetch_audit_agent_idx",
        "artifact_fetch_audit",
        ["agent_id", "fetched_at"],
    )
    op.create_index(
        "artifact_fetch_audit_requester_idx",
        "artifact_fetch_audit",
        ["requester_kind", "requester_id", "fetched_at"],
    )
    op.create_index(
        "artifact_fetch_audit_fetched_idx",
        "artifact_fetch_audit",
        ["fetched_at", "seq"],
    )


def downgrade() -> None:
    op.drop_index("artifact_fetch_audit_fetched_idx", table_name="artifact_fetch_audit")
    op.drop_index(
        "artifact_fetch_audit_requester_idx", table_name="artifact_fetch_audit"
    )
    op.drop_index("artifact_fetch_audit_agent_idx", table_name="artifact_fetch_audit")
    op.drop_table("artifact_fetch_audit")
