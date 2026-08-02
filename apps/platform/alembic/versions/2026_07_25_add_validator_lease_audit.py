"""add the validator lease revocation audit table

The platform can revoke a validator's in-flight lease before its deadline when
the slot is proven idle. That rewrite (``status = expired``, ``deadline = now``)
destroys whatever run was on the slot and spends one of the agent's bounded
same-version retries, and it used to leave no trace whatsoever -- no log line,
no row, no metric. Three destroyed v7 benchmark runs had to be reconstructed
from ticket timestamps.

``validator_lease_audit`` is the append-only record of those revocations,
carrying the liveness evidence the decision was made on (heartbeat freshness,
the capacity snapshot, the lease age). Insert-only and outside the score audit
hash chain: a lease lifecycle event is not a scoring event.

Purely additive -- one new table, no backfill, no change to any existing row.

Revision ID: e5b8c31d47af
Revises: d4a2b8e63f19
Create Date: 2026-07-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e5b8c31d47af"
down_revision: str | Sequence[str] | None = "d4a2b8e63f19"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "validator_lease_audit",
        sa.Column("audit_id", sa.UUID(), nullable=False),
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("validator_hotkey", sa.Text(), nullable=False),
        sa.Column("slot_id", sa.Text(), nullable=False),
        sa.Column("bench_version", sa.Integer(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("context", sa.Text(), nullable=False),
        sa.Column("evidence", json_type, nullable=False),
        sa.Column(
            "recorded_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "bench_version > 0",
            name="validator_lease_audit_bench_version_positive",
        ),
        sa.PrimaryKeyConstraint("audit_id"),
    )
    op.create_index(
        "validator_lease_audit_agent_idx",
        "validator_lease_audit",
        ["agent_id", "recorded_at"],
    )
    op.create_index(
        "validator_lease_audit_validator_idx",
        "validator_lease_audit",
        ["validator_hotkey", "recorded_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "validator_lease_audit_validator_idx", table_name="validator_lease_audit"
    )
    op.drop_index("validator_lease_audit_agent_idx", table_name="validator_lease_audit")
    op.drop_table("validator_lease_audit")
