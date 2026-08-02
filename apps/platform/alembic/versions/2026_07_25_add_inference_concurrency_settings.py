"""add operator-controlled hosted-embedding concurrency settings

One new append-only settings table,
``inference_concurrency_settings_revisions``, mirroring the other
hot-swappable boards (``queue_policy_settings_revisions``,
``efficiency_bonus_settings_revisions``,
``admin_screener_review_settings``).

There is deliberately **no backfill row**. The shipped defaults in
``ditto.api_models.inference_concurrency_settings`` are the governing policy
until an operator writes a revision, exactly as the other boards behave, so an
empty table is a valid and fully-specified state.

Note that the shipped defaults here are NOT the values this migration's
predecessor ran with: the hosted v7 embedding lane moves from 1/8/32 to
12/48/144 in the same change. That raise lives in application defaults rather
than in a seeded row so that reverting it is a code revert, and so the operator
history in this table contains only operator decisions.

Revision ID: e7b4c02a5d18
Revises: d4a2b8e63f19
Create Date: 2026-07-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e7b4c02a5d18"
down_revision: str | Sequence[str] | None = "d4a2b8e63f19"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "inference_concurrency_settings_revisions",
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
        sa.CheckConstraint(
            "scope = '*'", name="inference_concurrency_settings_scope_check"
        ),
        sa.CheckConstraint(
            "length(checksum) = 64",
            name="inference_concurrency_settings_checksum_check",
        ),
        sa.CheckConstraint(
            "parent_revision >= 0",
            name="inference_concurrency_settings_parent_revision_check",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) BETWEEN 8 AND 500",
            name="inference_concurrency_settings_reason_check",
        ),
        sa.CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="inference_concurrency_settings_actor_check",
        ),
        sa.PrimaryKeyConstraint("revision"),
        sa.UniqueConstraint(
            "scope",
            "parent_revision",
            name="inference_concurrency_settings_scope_parent_key",
        ),
    )
    op.create_index(
        "inference_concurrency_settings_scope_revision_idx",
        "inference_concurrency_settings_revisions",
        ["scope", "revision"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "inference_concurrency_settings_scope_revision_idx",
        table_name="inference_concurrency_settings_revisions",
    )
    op.drop_table("inference_concurrency_settings_revisions")
