"""Add the readmission payment lane (SN-485).

Three changes, all inert until a readmission is charged:

* ``readmission_payments``: the finalized transfer that paid one readmission of
  an existing agent into one benchmark era. The primary key
  ``(block_hash, extrinsic_index)`` stops a replay within the lane, and a
  partial unique index allows at most one *admitted* readmission per agent and
  era. ``outcome = 'credited'`` records a paid readmission that was refused
  after payment; its proof is then banked as an upload credit. A readmission
  paid by spending a credit keeps that credit's history in ``credit_audit``.
* ``readmission_reservations``: the pre-payment fee and address snapshot for
  one ``(agent_id, bench_version)``, separate from the per-coldkey upload slot
  so a readmission never blocks a new upload.
* ``evaluation_payments.credit_origin``: why an unspent credit exists. Null on
  every existing row (a byte-identical upload); ``readmission`` marks a credit
  banked by a refused readmission, which stays spendable for 30 days instead
  of 24 hours.

None of these is a hot table. The new column is nullable with no default,
which is a metadata-only add.

Revision ID: 4f1c8a7d2e95
Revises: 9d4b7e2a6c18
Create Date: 2026-11-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "4f1c8a7d2e95"
down_revision: str | Sequence[str] | None = "9d4b7e2a6c18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TS = sa.TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.create_table(
        "readmission_payments",
        sa.Column("block_hash", sa.Text(), nullable=False),
        sa.Column("extrinsic_index", sa.Integer(), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("bench_version", sa.Integer(), nullable=False),
        sa.Column("rollout_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("miner_hotkey", sa.Text(), nullable=False),
        sa.Column("miner_coldkey", sa.Text(), nullable=False),
        sa.Column("amount_rao", sa.BigInteger(), nullable=False),
        sa.Column("tao_usd_rate", sa.Numeric(20, 8), nullable=True),
        sa.Column("dest_address", sa.Text(), nullable=False),
        sa.Column("timestamp", _TS, nullable=False),
        sa.Column("funding", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("refusal_reason", sa.Text(), nullable=True),
        sa.Column("credit_audit", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", _TS, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", _TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint(
            "block_hash", "extrinsic_index", name="readmission_payments_pkey"
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.agent_id"],
            ondelete="RESTRICT",
            name="readmission_payments_agent_id_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["rollout_id"],
            ["benchmark_rollouts.rollout_id"],
            ondelete="RESTRICT",
            name="readmission_payments_rollout_id_fkey",
        ),
        sa.CheckConstraint(
            "amount_rao > 0", name="readmission_payments_amount_rao_check"
        ),
        sa.CheckConstraint(
            "tao_usd_rate IS NULL OR tao_usd_rate > 0",
            name="readmission_payments_tao_usd_rate_positive",
        ),
        sa.CheckConstraint(
            "extrinsic_index >= 0",
            name="readmission_payments_extrinsic_index_check",
        ),
        sa.CheckConstraint(
            "bench_version >= 1", name="readmission_payments_bench_version_check"
        ),
        sa.CheckConstraint(
            "funding IN ('fresh', 'credit')",
            name="readmission_payments_funding_check",
        ),
        sa.CheckConstraint(
            "(funding = 'credit') = (credit_audit IS NOT NULL)",
            name="readmission_payments_credit_audit_check",
        ),
        sa.CheckConstraint(
            "(outcome = 'admitted' AND refusal_reason IS NULL) OR "
            "(outcome = 'credited' AND funding = 'fresh' "
            "AND length(trim(refusal_reason)) > 0)",
            name="readmission_payments_outcome_check",
        ),
    )
    op.create_index(
        "readmission_payments_admitted_once",
        "readmission_payments",
        ["agent_id", "bench_version"],
        unique=True,
        postgresql_where=sa.text("outcome = 'admitted'"),
    )
    op.create_index(
        "readmission_payments_miner_coldkey_idx",
        "readmission_payments",
        ["miner_coldkey", "created_at"],
    )
    op.create_index(
        "readmission_payments_bench_version_idx",
        "readmission_payments",
        ["bench_version", "created_at"],
    )

    op.create_table(
        "readmission_reservations",
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("bench_version", sa.Integer(), nullable=False),
        sa.Column("token", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rollout_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("miner_hotkey", sa.Text(), nullable=False),
        sa.Column("miner_coldkey", sa.Text(), nullable=False),
        sa.Column("settings_revision", sa.Integer(), nullable=False),
        sa.Column("cooldown_seconds", sa.Integer(), nullable=False),
        sa.Column("fee_amount_rao", sa.BigInteger(), nullable=False),
        sa.Column("payment_send_address", sa.Text(), nullable=False),
        sa.Column("created_at", _TS, server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", _TS, nullable=False),
        sa.PrimaryKeyConstraint(
            "agent_id", "bench_version", name="readmission_reservations_pkey"
        ),
        sa.UniqueConstraint("token", name="readmission_reservations_token_key"),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.agent_id"],
            ondelete="CASCADE",
            name="readmission_reservations_agent_id_fkey",
        ),
        sa.CheckConstraint(
            "cooldown_seconds BETWEEN 60 AND 86400",
            name="readmission_reservations_cooldown_seconds_check",
        ),
        sa.CheckConstraint(
            "fee_amount_rao BETWEEN 1 AND 1000000000000",
            name="readmission_reservations_fee_amount_rao_check",
        ),
    )
    op.create_index(
        "readmission_reservations_expires_at_idx",
        "readmission_reservations",
        ["expires_at"],
    )
    op.create_index(
        "readmission_reservations_miner_coldkey_idx",
        "readmission_reservations",
        ["miner_coldkey"],
    )

    op.add_column(
        "evaluation_payments",
        sa.Column("credit_origin", sa.Text(), nullable=True),
    )
    # NOT VALID skips the existing-row scan under the ACCESS EXCLUSIVE lock
    # (every existing row is NULL anyway); VALIDATE then runs in its own
    # transaction under SHARE UPDATE EXCLUSIVE, which does not block readers
    # or writers of the payment ledger.
    op.execute(
        "ALTER TABLE evaluation_payments "
        "ADD CONSTRAINT evaluation_payments_credit_origin_check "
        "CHECK (credit_origin IS NULL OR credit_origin = 'readmission') NOT VALID"
    )
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TABLE evaluation_payments "
            "VALIDATE CONSTRAINT evaluation_payments_credit_origin_check"
        )


def downgrade() -> None:
    op.drop_constraint(
        "evaluation_payments_credit_origin_check",
        "evaluation_payments",
        type_="check",
    )
    op.drop_column("evaluation_payments", "credit_origin")
    op.drop_index(
        "readmission_reservations_miner_coldkey_idx",
        table_name="readmission_reservations",
    )
    op.drop_index(
        "readmission_reservations_expires_at_idx",
        table_name="readmission_reservations",
    )
    op.drop_table("readmission_reservations")
    op.drop_index(
        "readmission_payments_bench_version_idx", table_name="readmission_payments"
    )
    op.drop_index(
        "readmission_payments_miner_coldkey_idx", table_name="readmission_payments"
    )
    op.drop_index(
        "readmission_payments_admitted_once", table_name="readmission_payments"
    )
    op.drop_table("readmission_payments")
