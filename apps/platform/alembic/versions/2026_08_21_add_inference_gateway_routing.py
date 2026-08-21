"""add ordered inference gateway routing and attempt telemetry

Revision ID: c7e1a4b9d205
Revises: b7e2c91a04d6
Create Date: 2026-08-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c7e1a4b9d205"
down_revision: str | Sequence[str] | None = "b7e2c91a04d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.add_column(
        "inference_routing_policies",
        sa.Column(
            "gateway_provider_order",
            json_type,
            server_default=sa.text("'[\"openrouter\"]'"),
            nullable=False,
        ),
    )
    op.create_table(
        "inference_gateway_attempts",
        sa.Column("attempt_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("grant_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("nonce", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("phase", sa.Integer(), nullable=False),
        sa.Column("gateway_provider", sa.Text(), nullable=False),
        sa.Column("upstream_provider", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("upstream_attempts", sa.Integer(), nullable=False),
        sa.Column("openrouter_attempts", sa.Integer(), nullable=False),
        sa.Column("prompt_tokens", sa.BigInteger(), nullable=False),
        sa.Column("completion_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cost_microusd", sa.BigInteger(), nullable=False),
        sa.Column("cost_available", sa.Boolean(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("timed_out", sa.Boolean(), nullable=False),
        sa.Column("terminal_error_code", sa.Text(), nullable=True),
        sa.Column("recorded_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('completed', 'failed')",
            name="inference_gateway_attempt_status",
        ),
        sa.CheckConstraint(
            "phase >= 0 AND upstream_attempts >= 0 AND openrouter_attempts >= 0",
            name="inference_gateway_attempt_counts",
        ),
        sa.CheckConstraint(
            "prompt_tokens >= 0 AND completion_tokens >= 0 AND "
            "cost_microusd >= 0 AND latency_ms >= 0",
            name="inference_gateway_attempt_usage",
        ),
        sa.ForeignKeyConstraint(
            ["grant_id", "nonce"],
            ["inference_requests.grant_id", "inference_requests.nonce"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("attempt_id"),
        sa.UniqueConstraint(
            "grant_id", "nonce", "phase", name="inference_gateway_attempt_phase_key"
        ),
    )
    op.create_index(
        "inference_gateway_attempt_provider_recorded_idx",
        "inference_gateway_attempts",
        ["gateway_provider", "recorded_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "inference_gateway_attempt_provider_recorded_idx",
        table_name="inference_gateway_attempts",
    )
    op.drop_table("inference_gateway_attempts")
    op.drop_column("inference_routing_policies", "gateway_provider_order")
