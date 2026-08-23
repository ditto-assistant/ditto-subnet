"""add exclusive shadow coding ticket claims

Revision ID: a0d6c3e9f521
Revises: f9c5b2e8d374
Create Date: 2026-08-30 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a0d6c3e9f521"
down_revision: str | Sequence[str] | None = "f9c5b2e8d374"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "coding_shadow_tickets",
        sa.Column("claim_generation", sa.Integer(), nullable=False, server_default="0"),
    )
    for name, column in (
        ("claim_instance_id", sa.Text()),
        ("claim_acquired_at", sa.DateTime(timezone=True)),
        ("claim_heartbeat_at", sa.DateTime(timezone=True)),
        ("claim_expires_at", sa.DateTime(timezone=True)),
        ("claim_started_at", sa.DateTime(timezone=True)),
    ):
        op.add_column("coding_shadow_tickets", sa.Column(name, column, nullable=True))
    op.create_check_constraint(
        "coding_shadow_tickets_claim_check",
        "coding_shadow_tickets",
        "(claim_generation = 0 AND claim_instance_id IS NULL "
        "AND claim_acquired_at IS NULL AND claim_heartbeat_at IS NULL "
        "AND claim_expires_at IS NULL AND claim_started_at IS NULL) OR "
        "(claim_generation BETWEEN 1 AND 2147483647 AND "
        "((claim_instance_id IS NULL AND claim_acquired_at IS NULL "
        "AND claim_heartbeat_at IS NULL AND claim_expires_at IS NULL "
        "AND claim_started_at IS NULL) OR "
        "(claim_instance_id IS NOT NULL AND "
        "octet_length(claim_instance_id) BETWEEN 1 AND 128 "
        "AND claim_instance_id !~ '[[:space:][:cntrl:]]' "
        "AND claim_acquired_at IS NOT NULL AND claim_heartbeat_at IS NOT NULL "
        "AND claim_expires_at IS NOT NULL "
        "AND claim_heartbeat_at >= claim_acquired_at "
        "AND claim_expires_at > claim_heartbeat_at "
        "AND claim_expires_at <= deadline "
        "AND (claim_started_at IS NULL OR "
        "(claim_started_at >= claim_acquired_at "
        "AND claim_started_at <= claim_heartbeat_at "
        "AND claim_started_at < deadline)))))",
    )
    op.create_index(
        "coding_shadow_tickets_claim_instance_idx",
        "coding_shadow_tickets",
        ["validator_hotkey", "claim_instance_id", "claim_expires_at"],
    )
    op.create_index(
        "coding_shadow_tickets_claim_instance_key",
        "coding_shadow_tickets",
        ["validator_hotkey", "claim_instance_id"],
        unique=True,
        postgresql_where=sa.text("claim_instance_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "coding_shadow_tickets_claim_instance_key",
        table_name="coding_shadow_tickets",
    )
    op.drop_index(
        "coding_shadow_tickets_claim_instance_idx",
        table_name="coding_shadow_tickets",
    )
    op.drop_constraint(
        "coding_shadow_tickets_claim_check",
        "coding_shadow_tickets",
        type_="check",
    )
    for name in (
        "claim_started_at",
        "claim_expires_at",
        "claim_heartbeat_at",
        "claim_acquired_at",
        "claim_instance_id",
        "claim_generation",
    ):
        op.drop_column("coding_shadow_tickets", name)
