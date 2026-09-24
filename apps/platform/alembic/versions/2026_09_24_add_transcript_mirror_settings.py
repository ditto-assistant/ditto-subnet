"""Gate the public transcript mirror behind an audited operator setting.

Revision ID: b7e2c9a41d08
Revises: 1bc9d8a6207e
Create Date: 2026-09-24

The genesis row leaves the mirror off. A public bucket alone must not publish
transcripts.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b7e2c9a41d08"
down_revision: str | Sequence[str] | None = "1bc9d8a6207e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "transcript_mirror_settings_revisions",
        sa.Column("revision", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("parent_revision", sa.Integer(), nullable=False),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "parent_revision >= 0",
            name="transcript_mirror_settings_parent_revision_check",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) >= 8",
            name="transcript_mirror_settings_reason_check",
        ),
        sa.CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="transcript_mirror_settings_actor_check",
        ),
        sa.PrimaryKeyConstraint("revision"),
        sa.UniqueConstraint(
            "parent_revision",
            name="transcript_mirror_settings_parent_revision_key",
        ),
    )
    settings = sa.table(
        "transcript_mirror_settings_revisions",
        sa.column("parent_revision", sa.Integer()),
        sa.column("enabled", sa.Boolean()),
        sa.column("reason", sa.Text()),
        sa.column("actor", sa.Text()),
    )
    op.bulk_insert(
        settings,
        [
            {
                "parent_revision": 0,
                "enabled": False,
                "reason": "Keep the public transcript mirror off",
                "actor": "migration",
            }
        ],
    )


def downgrade() -> None:
    op.drop_table("transcript_mirror_settings_revisions")
