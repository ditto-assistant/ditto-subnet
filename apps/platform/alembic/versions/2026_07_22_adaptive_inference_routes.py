"""add adaptive inference provider routes

Revision ID: d8b02a15e3cf
Revises: f1a2c3d4e5b6
Create Date: 2026-07-22
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d8b02a15e3cf"
down_revision: str | None = "a8d4c6e21f90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "inference_provider_routes",
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("profile_revision", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("calibration_status", sa.Text(), nullable=False),
        sa.Column("context_length", sa.Integer(), nullable=True),
        sa.Column("quantization", sa.Text(), nullable=True),
        sa.Column("prompt_price_per_token", sa.Float(), nullable=True),
        sa.Column("completion_price_per_token", sa.Float(), nullable=True),
        sa.Column("ewma_tokens_per_second", sa.Float(), nullable=True),
        sa.Column("ewma_latency_ms", sa.Float(), nullable=True),
        sa.Column("ewma_error_rate", sa.Float(), server_default="0", nullable=False),
        sa.Column("ewma_timeout_rate", sa.Float(), server_default="0", nullable=False),
        sa.Column("ewma_tool_accuracy", sa.Float(), nullable=True),
        sa.Column("ewma_composite", sa.Float(), nullable=True),
        sa.Column("calibration_tool_accuracy", sa.Float(), nullable=True),
        sa.Column("calibration_composite", sa.Float(), nullable=True),
        sa.Column(
            "calibration_sample_count", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column(
            "calibration_revision", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("calibration_manifest_sha256", sa.Text(), nullable=True),
        sa.Column("calibrated_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("ewma_cost_microusd", sa.Float(), nullable=True),
        sa.Column("sample_count", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column(
            "selected_ticket_count", sa.BigInteger(), server_default="0", nullable=False
        ),
        sa.Column(
            "exploration_ticket_count", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("last_selected_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("cooldown_until", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("discovered_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("last_observed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('discovered', 'healthy', 'degraded', 'offline')",
            name="inference_provider_route_status",
        ),
        sa.CheckConstraint(
            "calibration_status IN ('shadow', 'eligible', 'disabled')",
            name="inference_provider_calibration_status",
        ),
        sa.CheckConstraint(
            "ewma_error_rate >= 0 AND ewma_error_rate <= 1",
            name="inference_provider_error_rate",
        ),
        sa.CheckConstraint(
            "ewma_timeout_rate >= 0 AND ewma_timeout_rate <= 1",
            name="inference_provider_timeout_rate",
        ),
        sa.CheckConstraint(
            "calibration_revision >= 0",
            name="inference_provider_calibration_revision",
        ),
        sa.PrimaryKeyConstraint("model", "provider", "profile_revision"),
        sa.UniqueConstraint("profile_revision", name="inference_provider_profile_key"),
    )
    op.create_index(
        "inference_provider_routes_selection_idx",
        "inference_provider_routes",
        ["model", "calibration_status", "status"],
    )
    op.create_table(
        "inference_routing_policies",
        sa.Column("model", sa.Text(), primary_key=True),
        sa.Column("revision", sa.Integer(), server_default="0", nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("speed_weight", sa.Float(), nullable=False),
        sa.Column("cost_weight", sa.Float(), nullable=False),
        sa.Column("exploration_weight", sa.Float(), nullable=False),
        sa.Column("exploration_ticket_budget", sa.Integer(), nullable=False),
        sa.Column("min_tool_accuracy", sa.Float(), nullable=False),
        sa.Column("min_composite", sa.Float(), nullable=False),
        sa.Column("min_calibration_samples", sa.Integer(), nullable=False),
        sa.Column("max_error_rate", sa.Float(), nullable=False),
        sa.Column("max_timeout_rate", sa.Float(), nullable=False),
        sa.Column("cooldown_seconds", sa.Integer(), nullable=False),
        sa.Column("ewma_alpha", sa.Float(), nullable=False),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "speed_weight >= 0 AND cost_weight >= 0 AND exploration_weight >= 0",
            name="inference_routing_policy_weights",
        ),
        sa.CheckConstraint(
            "speed_weight + cost_weight + exploration_weight > 0",
            name="inference_routing_policy_nonzero_weights",
        ),
        sa.CheckConstraint(
            "min_tool_accuracy >= 0 AND min_tool_accuracy <= 1 "
            "AND min_composite >= 0 AND min_composite <= 1",
            name="inference_routing_policy_quality",
        ),
        sa.CheckConstraint(
            "max_error_rate >= 0 AND max_error_rate <= 1 "
            "AND max_timeout_rate >= 0 AND max_timeout_rate <= 1 "
            "AND ewma_alpha > 0 AND ewma_alpha <= 1",
            name="inference_routing_policy_reliability",
        ),
        sa.CheckConstraint(
            "exploration_ticket_budget >= 0 AND min_calibration_samples > 0 "
            "AND cooldown_seconds >= 1 AND revision >= 0",
            name="inference_routing_policy_bounds",
        ),
    )
    op.create_table(
        "inference_routing_audit",
        sa.Column("audit_id", sa.Uuid(), primary_key=True),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("profile_revision", sa.Text(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("recorded_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_index(
        "inference_routing_audit_history_idx",
        "inference_routing_audit",
        ["recorded_at"],
    )
    op.add_column(
        "inference_grants", sa.Column("route_provider", sa.Text(), nullable=True)
    )
    op.add_column(
        "inference_grants", sa.Column("route_profile", sa.Text(), nullable=True)
    )
    op.add_column(
        "inference_grants", sa.Column("route_quantization", sa.Text(), nullable=True)
    )
    op.add_column(
        "inference_grants",
        sa.Column("route_prompt_price_per_token", sa.Float(), nullable=True),
    )
    op.add_column(
        "inference_grants",
        sa.Column("route_completion_price_per_token", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("inference_grants", "route_completion_price_per_token")
    op.drop_column("inference_grants", "route_prompt_price_per_token")
    op.drop_column("inference_grants", "route_quantization")
    op.drop_column("inference_grants", "route_profile")
    op.drop_column("inference_grants", "route_provider")
    op.drop_index(
        "inference_routing_audit_history_idx", table_name="inference_routing_audit"
    )
    op.drop_table("inference_routing_audit")
    op.drop_table("inference_routing_policies")
    op.drop_index(
        "inference_provider_routes_selection_idx",
        table_name="inference_provider_routes",
    )
    op.drop_table("inference_provider_routes")
