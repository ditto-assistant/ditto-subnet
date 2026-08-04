"""add federated screener nodes and capacity control state

Revision ID: f68d2c1a9b04
Revises: c0e1f2a3b4d5
Create Date: 2026-08-04 11:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f68d2c1a9b04"
down_revision: str | Sequence[str] | None = "c0e1f2a3b4d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "screener_node_bootstrap_grants",
        sa.Column("grant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment", sa.Text(), nullable=False),
        sa.Column("node_id", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("provider_resource_id", sa.Text(), nullable=False),
        sa.Column("controller_epoch", sa.Text(), nullable=False),
        sa.Column("image_reference", sa.Text(), nullable=True),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("registration_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "environment ~ '^[a-z][a-z0-9-]{0,31}$'",
            name="screener_node_bootstrap_grants_environment_check",
        ),
        sa.CheckConstraint(
            "length(node_id) BETWEEN 1 AND 63",
            name="screener_node_bootstrap_grants_node_id_length_check",
        ),
        sa.CheckConstraint(
            "provider IN ('gcp', 'targon', 'hetzner', 'home', 'test')",
            name="screener_node_bootstrap_grants_provider_check",
        ),
        sa.CheckConstraint(
            "length(token_hash) = 64",
            name="screener_node_bootstrap_grants_token_hash_check",
        ),
        sa.CheckConstraint(
            "image_reference IS NULL OR image_reference ~ "
            "'^[a-z0-9.-]+(:[0-9]+)?/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$'",
            name="screener_node_bootstrap_grants_image_reference_check",
        ),
        sa.PrimaryKeyConstraint("grant_id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(
        "screener_node_bootstrap_grants_node_idx",
        "screener_node_bootstrap_grants",
        ["node_id", "created_at"],
    )
    op.create_table(
        "screener_nodes",
        sa.Column("environment", sa.Text(), nullable=False),
        sa.Column("node_id", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("provider_resource_id", sa.Text(), nullable=False),
        sa.Column("screener_hotkey", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("previous_token_hash", sa.Text(), nullable=True),
        sa.Column(
            "previous_token_expires_at", sa.TIMESTAMP(timezone=True), nullable=True
        ),
        sa.Column("last_refresh_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("token_expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), server_default="active", nullable=False),
        sa.Column("capacity", sa.Integer(), server_default="1", nullable=False),
        sa.Column("image_reference", sa.Text(), nullable=True),
        sa.Column(
            "registered_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "rotated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("status_reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "environment ~ '^[a-z][a-z0-9-]{0,31}$'",
            name="screener_nodes_environment_check",
        ),
        sa.CheckConstraint(
            "length(node_id) BETWEEN 1 AND 63",
            name="screener_nodes_node_id_length_check",
        ),
        sa.CheckConstraint(
            "provider IN ('gcp', 'targon', 'hetzner', 'home', 'test')",
            name="screener_nodes_provider_check",
        ),
        sa.CheckConstraint(
            "length(token_hash) = 64", name="screener_nodes_token_hash_check"
        ),
        sa.CheckConstraint(
            "previous_token_hash IS NULL OR length(previous_token_hash) = 64",
            name="screener_nodes_previous_token_hash_check",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'draining', 'quarantined', 'revoked')",
            name="screener_nodes_status_check",
        ),
        sa.CheckConstraint(
            "capacity BETWEEN 1 AND 16", name="screener_nodes_capacity_check"
        ),
        sa.CheckConstraint(
            "image_reference IS NULL OR image_reference ~ "
            "'^[a-z0-9.-]+(:[0-9]+)?/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$'",
            name="screener_nodes_image_reference_check",
        ),
        sa.PrimaryKeyConstraint("node_id"),
        sa.UniqueConstraint(
            "environment",
            "provider",
            "provider_resource_id",
            name="screener_nodes_provider_resource_key",
        ),
        sa.UniqueConstraint("screener_hotkey"),
    )
    op.create_index(
        "screener_nodes_provider_status_idx", "screener_nodes", ["provider", "status"]
    )
    op.create_table(
        "screener_capacity_snapshots",
        sa.Column("environment", sa.Text(), nullable=False),
        sa.Column("controller_epoch", sa.Text(), nullable=False),
        sa.Column("controller_source_sha", sa.Text(), nullable=False),
        sa.Column("provider_ready", sa.Boolean(), nullable=False),
        sa.Column(
            "controller_heartbeat_at", sa.TIMESTAMP(timezone=True), nullable=False
        ),
        sa.Column(
            "controller_lease_expires_at", sa.TIMESTAMP(timezone=True), nullable=False
        ),
        sa.Column("runnable_backlog", sa.Integer(), nullable=False),
        sa.Column("active_leases", sa.Integer(), nullable=False),
        sa.Column("desired_slots", sa.Integer(), nullable=False),
        sa.Column("global_cap", sa.Integer(), nullable=False),
        sa.Column("targon_capability", sa.Text(), nullable=False),
        sa.Column("targon_available", sa.Integer(), nullable=False),
        sa.Column("targon_healthy", sa.Integer(), nullable=False),
        sa.Column("targon_pending", sa.Integer(), nullable=False),
        sa.Column("targon_draining", sa.Integer(), nullable=False),
        sa.Column("gce_target", sa.Integer(), nullable=False),
        sa.Column("gce_healthy", sa.Integer(), nullable=False),
        sa.Column("gce_pending", sa.Integer(), nullable=False),
        sa.Column("gce_draining", sa.Integer(), nullable=False),
        sa.Column("fallback_reason", sa.Text(), nullable=True),
        sa.Column(
            "last_provider_success_at", sa.TIMESTAMP(timezone=True), nullable=True
        ),
        sa.Column("last_provider_error_code", sa.Text(), nullable=True),
        sa.Column("last_provider_error_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "targon_capability IN ('go', 'nogo', 'unknown')",
            name="screener_capacity_snapshots_targon_capability_check",
        ),
        sa.CheckConstraint(
            "controller_source_sha ~ '^[0-9a-f]{40}$'",
            name="screener_capacity_snapshots_source_sha_check",
        ),
        sa.CheckConstraint(
            "runnable_backlog >= 0 AND active_leases >= 0 AND "
            "desired_slots >= 0 AND global_cap >= 0",
            name="screener_capacity_snapshots_nonnegative_demand_check",
        ),
        sa.CheckConstraint(
            "targon_available >= 0 AND targon_healthy >= 0 AND "
            "targon_pending >= 0 AND targon_draining >= 0",
            name="screener_capacity_snapshots_nonnegative_targon_check",
        ),
        sa.CheckConstraint(
            "gce_target >= 0 AND gce_healthy >= 0 AND "
            "gce_pending >= 0 AND gce_draining >= 0",
            name="screener_capacity_snapshots_nonnegative_gce_check",
        ),
        sa.PrimaryKeyConstraint("environment"),
    )
    op.create_table(
        "screener_capacity_events",
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=True),
        sa.Column("node_id", sa.Text(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("controller_epoch", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "provider IS NULL OR "
            "provider IN ('gcp', 'targon', 'hetzner', 'home', 'test')",
            name="screener_capacity_events_provider_check",
        ),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(
        "screener_capacity_events_environment_created_idx",
        "screener_capacity_events",
        ["environment", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "screener_capacity_events_environment_created_idx",
        table_name="screener_capacity_events",
    )
    op.drop_table("screener_capacity_events")
    op.drop_table("screener_capacity_snapshots")
    op.drop_index("screener_nodes_provider_status_idx", table_name="screener_nodes")
    op.drop_table("screener_nodes")
    op.drop_index(
        "screener_node_bootstrap_grants_node_idx",
        table_name="screener_node_bootstrap_grants",
    )
    op.drop_table("screener_node_bootstrap_grants")
