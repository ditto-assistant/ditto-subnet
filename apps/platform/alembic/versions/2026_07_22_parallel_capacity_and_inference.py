"""add heartbeat v10 slots and ticket inference grants

Revision ID: c7a91f04d2be
Revises: e6b1a4c92d70
Create Date: 2026-07-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c7a91f04d2be"
down_revision: str | None = "e6b1a4c92d70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.add_column(
        "validator_heartbeats",
        sa.Column("benchmark_capacity", json_type, nullable=True),
    )
    op.add_column(
        "validator_tickets",
        sa.Column("slot_id", sa.Text(), server_default="slot-0", nullable=False),
    )
    op.create_check_constraint(
        "validator_tickets_slot_id",
        "validator_tickets",
        "slot_id IN ('slot-0', 'slot-1', 'slot-2', 'slot-3', "
        "'slot-4', 'slot-5', 'slot-6', 'slot-7')",
    )
    op.drop_index(
        "validator_tickets_one_issued_per_validator_idx",
        table_name="validator_tickets",
    )
    op.create_index(
        "validator_tickets_one_issued_per_validator_slot_idx",
        "validator_tickets",
        ["validator_hotkey", "slot_id"],
        unique=True,
        postgresql_where=sa.text("status = 'issued'"),
    )

    op.create_table(
        "inference_grants",
        sa.Column("grant_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("bench_version", sa.Integer(), nullable=False),
        sa.Column("validator_hotkey", sa.Text(), nullable=False),
        sa.Column("slot_id", sa.Text(), nullable=False),
        sa.Column("ticket_deadline", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("bearer_digest", sa.Text(), nullable=True),
        sa.Column("broker_public_key", sa.Text(), nullable=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("allowed_models", json_type, nullable=False),
        sa.Column("request_budget", sa.Integer(), nullable=False),
        sa.Column("token_budget", sa.BigInteger(), nullable=False),
        sa.Column("request_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("prompt_tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column(
            "completion_tokens", sa.BigInteger(), server_default="0", nullable=False
        ),
        sa.Column("cost_microusd", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("active_requests", sa.Integer(), server_default="0", nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'revoked', 'exhausted')",
            name="inference_grants_status",
        ),
        sa.CheckConstraint(
            "request_budget > 0", name="inference_grants_request_budget"
        ),
        sa.CheckConstraint("token_budget > 0", name="inference_grants_token_budget"),
        sa.CheckConstraint(
            "active_requests >= 0", name="inference_grants_active_requests"
        ),
        sa.ForeignKeyConstraint(
            ["agent_id", "bench_version", "validator_hotkey"],
            [
                "validator_tickets.agent_id",
                "validator_tickets.bench_version",
                "validator_tickets.validator_hotkey",
            ],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("grant_id"),
        sa.UniqueConstraint(
            "agent_id",
            "bench_version",
            "validator_hotkey",
            "ticket_deadline",
            name="inference_grants_ticket_lease",
        ),
    )
    op.create_index("inference_grants_expiry_idx", "inference_grants", ["expires_at"])
    op.create_table(
        "inference_requests",
        sa.Column("grant_id", sa.Uuid(), nullable=False),
        sa.Column("nonce", sa.Uuid(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("reserved_tokens", sa.BigInteger(), nullable=False),
        sa.Column("prompt_tokens", sa.BigInteger(), nullable=False),
        sa.Column("completion_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cost_microusd", sa.BigInteger(), nullable=False),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('started', 'completed', 'failed', 'canceled')",
            name="inference_requests_status",
        ),
        sa.CheckConstraint(
            "reserved_tokens > 0", name="inference_requests_reserved_tokens"
        ),
        sa.CheckConstraint("generation > 0", name="inference_requests_generation"),
        sa.ForeignKeyConstraint(
            ["grant_id"], ["inference_grants.grant_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("grant_id", "nonce"),
    )
    op.create_index(
        "inference_requests_started_idx", "inference_requests", ["started_at"]
    )


def downgrade() -> None:
    duplicate_issued = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT validator_hotkey FROM validator_tickets "
                "WHERE status = 'issued' GROUP BY validator_hotkey "
                "HAVING count(*) > 1 LIMIT 1"
            )
        )
        .first()
    )
    if duplicate_issued is not None:
        raise RuntimeError(
            "cannot downgrade parallel capacity while a validator has multiple "
            "issued tickets; drain validators and expire extra slot leases first"
        )
    op.drop_index("inference_requests_started_idx", table_name="inference_requests")
    op.drop_table("inference_requests")
    op.drop_index("inference_grants_expiry_idx", table_name="inference_grants")
    op.drop_table("inference_grants")
    op.drop_index(
        "validator_tickets_one_issued_per_validator_slot_idx",
        table_name="validator_tickets",
    )
    op.create_index(
        "validator_tickets_one_issued_per_validator_idx",
        "validator_tickets",
        ["validator_hotkey"],
        unique=True,
        postgresql_where=sa.text("status = 'issued'"),
    )
    op.drop_constraint("validator_tickets_slot_id", "validator_tickets", type_="check")
    op.drop_column("validator_tickets", "slot_id")
    op.drop_column("validator_heartbeats", "benchmark_capacity")
