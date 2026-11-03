"""add operator per-owner submission cooldown overrides

Revision ID: b7d3e91c4f20
Revises: 3f7c1d9a5b24
Create Date: 2026-11-03

The submission cooldown was one global value per payment coldkey. An operator
now needs to slow down one owner (for example after repeated copy rejects)
without changing every other miner's interval. Each row is one append-only
revision of a single owner identity's override, keyed by ``scope``
(``coldkey`` or ``hotkey``) and ``owner_key``. ``action = 'set'`` installs a
minimum interval between that owner's submissions until ``expires_at``;
``action = 'revoke'`` ends it early. Nothing is updated or deleted, so the
table is its own audit trail. A new, empty table: no hot-table lock concerns.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b7d3e91c4f20"
down_revision: str | Sequence[str] | None = "3f7c1d9a5b24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "owner_submission_cooldown_revisions",
        sa.Column("revision_id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("owner_key", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("cooldown_seconds", sa.Integer(), nullable=True),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("revision_id"),
        sa.UniqueConstraint(
            "scope",
            "owner_key",
            "revision",
            name="owner_submission_cooldown_revisions_key",
        ),
        sa.CheckConstraint(
            "scope IN ('coldkey', 'hotkey')",
            name="owner_submission_cooldown_scope_check",
        ),
        sa.CheckConstraint(
            "length(owner_key) BETWEEN 1 AND 128",
            name="owner_submission_cooldown_owner_key_check",
        ),
        sa.CheckConstraint(
            "revision >= 1", name="owner_submission_cooldown_revision_check"
        ),
        sa.CheckConstraint(
            "(action = 'set' AND cooldown_seconds BETWEEN 60 AND 2592000 "
            "AND expires_at IS NOT NULL AND expires_at > created_at) "
            "OR (action = 'revoke' AND cooldown_seconds IS NULL "
            "AND expires_at IS NULL)",
            name="owner_submission_cooldown_action_check",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) >= 8",
            name="owner_submission_cooldown_reason_check",
        ),
        sa.CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="owner_submission_cooldown_actor_check",
        ),
    )


def downgrade() -> None:
    op.drop_table("owner_submission_cooldown_revisions")
