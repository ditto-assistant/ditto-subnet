"""add purpose-bound confirmation inference grants

Revision ID: c8a6d1e4f903
Revises: e8a4c91d7f20
Create Date: 2026-08-13
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c8a6d1e4f903"
down_revision: str | Sequence[str] | None = "e8a4c91d7f20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "confirmation_inference_grants",
        sa.Column("grant_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("ticket_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("bundle_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("validator_hotkey", sa.Text(), nullable=False),
        sa.Column("lane", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("bearer_digest", sa.Text(), nullable=False),
        sa.Column("broker_public_key", sa.Text(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("route_provider", sa.Text(), nullable=False),
        sa.Column("receipt_provider", sa.Text(), nullable=False),
        sa.Column("profile_revision", sa.Text(), nullable=False),
        sa.Column("request_budget", sa.Integer(), nullable=False),
        sa.Column("token_budget", sa.BigInteger(), nullable=False),
        sa.Column("cost_budget_microusd", sa.BigInteger(), nullable=False),
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
            "lane IN ('reader', 'judge', 'embedding')",
            name="confirmation_inference_grants_lane_check",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'revoked', 'exhausted')",
            name="confirmation_inference_grants_status_check",
        ),
        sa.CheckConstraint(
            "request_budget > 0 AND token_budget > 0 AND cost_budget_microusd > 0",
            name="confirmation_inference_grants_budget_check",
        ),
        sa.CheckConstraint(
            "request_count >= 0 AND prompt_tokens >= 0 "
            "AND completion_tokens >= 0 AND cost_microusd >= 0 "
            "AND active_requests >= 0",
            name="confirmation_inference_grants_accounting_check",
        ),
        sa.CheckConstraint(
            "generation > 0", name="confirmation_inference_grants_generation_check"
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id", "bundle_id"],
            [
                "confirmation_bundle_tickets.ticket_id",
                "confirmation_bundle_tickets.bundle_id",
            ],
            name="confirmation_inference_grants_ticket_fkey",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("grant_id"),
        sa.UniqueConstraint(
            "ticket_id", "lane", name="confirmation_inference_grants_ticket_lane_key"
        ),
    )
    op.create_index(
        "confirmation_inference_grants_expiry_idx",
        "confirmation_inference_grants",
        ["expires_at"],
    )
    op.create_table(
        "confirmation_inference_requests",
        sa.Column("grant_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("nonce", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("reserved_tokens", sa.BigInteger(), nullable=False),
        sa.Column("max_chargeable_tokens", sa.BigInteger(), nullable=False),
        sa.Column("prompt_tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column(
            "completion_tokens", sa.BigInteger(), server_default="0", nullable=False
        ),
        sa.Column("cost_microusd", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("upstream_provider", sa.Text(), nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('started', 'completed', 'failed', 'canceled')",
            name="confirmation_inference_requests_status_check",
        ),
        sa.CheckConstraint(
            "generation > 0 AND reserved_tokens > 0 "
            "AND max_chargeable_tokens >= reserved_tokens",
            name="confirmation_inference_requests_reservation_check",
        ),
        sa.CheckConstraint(
            "prompt_tokens >= 0 AND completion_tokens >= 0 AND cost_microusd >= 0",
            name="confirmation_inference_requests_accounting_check",
        ),
        sa.ForeignKeyConstraint(
            ["grant_id"], ["confirmation_inference_grants.grant_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("grant_id", "nonce"),
    )
    op.create_index(
        "confirmation_inference_requests_started_idx",
        "confirmation_inference_requests",
        ["started_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "confirmation_inference_requests_started_idx",
        table_name="confirmation_inference_requests",
    )
    op.drop_table("confirmation_inference_requests")
    op.drop_index(
        "confirmation_inference_grants_expiry_idx",
        table_name="confirmation_inference_grants",
    )
    op.drop_table("confirmation_inference_grants")
