"""add audited validator fleet update controls

Revision ID: e8d4c7a91f20
Revises: b7c4e1a90d52
Create Date: 2026-08-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e8d4c7a91f20"
down_revision: str | Sequence[str] | None = "b7c4e1a90d52"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "validator_fleet_update_operations",
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("expected_snapshot", sa.Text(), nullable=False),
        sa.Column(
            "target_validator_hotkeys",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "target_stack_revisions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("revoked_lease_count", sa.Integer(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(expected_snapshot) = 64",
            name="validator_fleet_updates_snapshot_check",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(target_validator_hotkeys) = 'array'",
            name="validator_fleet_updates_targets_check",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(target_stack_revisions) = 'object'",
            name="validator_fleet_updates_revisions_check",
        ),
        sa.CheckConstraint(
            "revoked_lease_count >= 0",
            name="validator_fleet_updates_revoked_count_check",
        ),
        sa.CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="validator_fleet_updates_actor_check",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) >= 8",
            name="validator_fleet_updates_reason_check",
        ),
        sa.PrimaryKeyConstraint("operation_id"),
    )
    op.create_index(
        "validator_fleet_updates_created_idx",
        "validator_fleet_update_operations",
        ["created_at"],
    )
    op.add_column(
        "validator_heartbeats",
        sa.Column(
            "last_fleet_update_operation_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "validator_heartbeats_last_fleet_update_fkey",
        "validator_heartbeats",
        "validator_fleet_update_operations",
        ["last_fleet_update_operation_id"],
        ["operation_id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "validator_heartbeats_last_fleet_update_fkey",
        "validator_heartbeats",
        type_="foreignkey",
    )
    op.drop_column("validator_heartbeats", "last_fleet_update_operation_id")
    op.drop_index(
        "validator_fleet_updates_created_idx",
        table_name="validator_fleet_update_operations",
    )
    op.drop_table("validator_fleet_update_operations")
