"""add audited artifact release settings

Revision ID: a7c4e2f913bd
Revises: e2b7c91d4a60
Create Date: 2026-07-24
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a7c4e2f913bd"
down_revision: str | None = "e2b7c91d4a60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "artifact_release_settings_revisions",
        sa.Column("revision", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("parent_revision", sa.Integer(), nullable=False),
        sa.Column("embargo_hours", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "embargo_hours BETWEEN 6 AND 24",
            name="artifact_release_settings_embargo_hours_check",
        ),
        sa.CheckConstraint(
            "parent_revision >= 0",
            name="artifact_release_settings_parent_revision_check",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) BETWEEN 8 AND 500",
            name="artifact_release_settings_reason_check",
        ),
        sa.CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="artifact_release_settings_actor_check",
        ),
        sa.PrimaryKeyConstraint("revision"),
        sa.UniqueConstraint(
            "parent_revision",
            name="artifact_release_settings_parent_revision_key",
        ),
    )
    artifact_release_settings = sa.table(
        "artifact_release_settings_revisions",
        sa.column("parent_revision", sa.Integer()),
        sa.column("embargo_hours", sa.Integer()),
        sa.column("reason", sa.Text()),
        sa.column("actor", sa.Text()),
    )
    op.bulk_insert(
        artifact_release_settings,
        [
            {
                "parent_revision": 0,
                "embargo_hours": 24,
                "reason": "Initialize privacy-first 24-hour source embargo",
                "actor": "migration",
            }
        ],
    )


def downgrade() -> None:
    op.drop_table("artifact_release_settings_revisions")
