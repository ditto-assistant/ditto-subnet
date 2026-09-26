"""add the terminal-review emission eligibility posture and its shadow ledger

Revision ID: b6f3d0c7a915
Revises: 5d1f7a93c2e8
Create Date: 2026-09-23

Two new tables, no change to any existing one, so this is additive and safe to
apply ahead of the code that reads it:

* ``emission_eligibility_settings_revisions`` -- the append-only operator
  posture (ditto-subnet #2041), shaped exactly like
  ``burn_settings_revisions``. An empty table means ``enforcement = "off"``,
  which is the behaviour that existed before the gate, so applying this
  migration on its own changes no weights.
* ``emission_eligibility_shadow_records`` -- the rehearsal ledger: one row per
  artifact per emission window naming what ``enforce`` *would* have withheld.
  The unique key is what keeps a 30-second validator poll from inserting the
  same finding forever.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b6f3d0c7a915"
down_revision: str | Sequence[str] | None = "5d1f7a93c2e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "emission_eligibility_settings_revisions",
        sa.Column("revision", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("parent_revision", sa.Integer(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("settings", json_type, nullable=False),
        sa.Column("checksum", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("scope = '*'", name="emission_eligibility_scope_check"),
        sa.CheckConstraint(
            "length(checksum) = 64", name="emission_eligibility_checksum_check"
        ),
        sa.CheckConstraint(
            "parent_revision >= 0",
            name="emission_eligibility_parent_revision_check",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) >= 8",
            name="emission_eligibility_reason_check",
        ),
        sa.CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="emission_eligibility_actor_check",
        ),
        sa.PrimaryKeyConstraint("revision"),
        sa.UniqueConstraint(
            "scope",
            "parent_revision",
            name="emission_eligibility_scope_parent_key",
        ),
    )
    op.create_index(
        "emission_eligibility_scope_revision_idx",
        "emission_eligibility_settings_revisions",
        ["scope", "revision"],
        unique=True,
    )
    op.create_table(
        "emission_eligibility_shadow_records",
        sa.Column("record_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("artifact_sha256", sa.Text(), nullable=False),
        sa.Column("bench_version", sa.Integer(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("policy_revision", sa.Integer(), nullable=False),
        sa.Column("policy_checksum", sa.Text(), nullable=False),
        sa.Column("enforcement", sa.Text(), nullable=False),
        sa.Column("window_start", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.agent_id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "artifact_sha256 ~ '^[0-9a-f]{64}$'",
            name="emission_eligibility_shadow_sha_check",
        ),
        sa.CheckConstraint(
            "enforcement IN ('off', 'shadow', 'enforce')",
            name="emission_eligibility_shadow_enforcement_check",
        ),
        sa.CheckConstraint(
            "length(policy_checksum) = 64",
            name="emission_eligibility_shadow_checksum_check",
        ),
        sa.PrimaryKeyConstraint("record_id"),
        sa.UniqueConstraint(
            "agent_id",
            "bench_version",
            "policy_revision",
            "window_start",
            name="emission_eligibility_shadow_window_key",
        ),
    )
    op.create_index(
        "emission_eligibility_shadow_window_idx",
        "emission_eligibility_shadow_records",
        ["window_start", "created_at"],
    )
    op.create_index(
        "emission_eligibility_shadow_agent_idx",
        "emission_eligibility_shadow_records",
        ["agent_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "emission_eligibility_shadow_agent_idx",
        table_name="emission_eligibility_shadow_records",
    )
    op.drop_index(
        "emission_eligibility_shadow_window_idx",
        table_name="emission_eligibility_shadow_records",
    )
    op.drop_table("emission_eligibility_shadow_records")
    op.drop_index(
        "emission_eligibility_scope_revision_idx",
        table_name="emission_eligibility_settings_revisions",
    )
    op.drop_table("emission_eligibility_settings_revisions")
