"""add submission cooldown settings and pre-payment admission reservations

Revision ID: f4b8c2d91a70
Revises: c3f7a1d9b6e2
Create Date: 2026-07-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f4b8c2d91a70"
down_revision: str | Sequence[str] | None = "c3f7a1d9b6e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "submission_settings_revisions",
        sa.Column("revision", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("parent_revision", sa.Integer(), nullable=False),
        sa.Column("cooldown_seconds", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "cooldown_seconds BETWEEN 60 AND 86400",
            name="submission_settings_cooldown_seconds_check",
        ),
        sa.CheckConstraint(
            "parent_revision >= 0", name="submission_settings_parent_revision_check"
        ),
        sa.CheckConstraint(
            "length(trim(reason)) BETWEEN 8 AND 500",
            name="submission_settings_reason_check",
        ),
        sa.CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="submission_settings_actor_check",
        ),
        sa.PrimaryKeyConstraint("revision"),
        sa.UniqueConstraint(
            "parent_revision", name="submission_settings_parent_revision_key"
        ),
    )
    op.bulk_insert(
        sa.table(
            "submission_settings_revisions",
            sa.column("parent_revision", sa.Integer()),
            sa.column("cooldown_seconds", sa.Integer()),
            sa.column("reason", sa.Text()),
            sa.column("actor", sa.Text()),
        ),
        [
            {
                "parent_revision": 0,
                "cooldown_seconds": 3600,
                "reason": "Initialize existing one-hour submission cooldown",
                "actor": "migration",
            }
        ],
    )
    op.create_table(
        "upload_admission_reservations",
        sa.Column("miner_coldkey", sa.Text(), nullable=False),
        sa.Column("token", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("miner_hotkey", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("settings_revision", sa.Integer(), nullable=False),
        sa.Column("cooldown_seconds", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint(
            "cooldown_seconds BETWEEN 60 AND 86400",
            name="upload_admission_cooldown_seconds_check",
        ),
        sa.CheckConstraint(
            "length(sha256) = 64", name="upload_admission_sha256_length_check"
        ),
        sa.PrimaryKeyConstraint("miner_coldkey"),
        sa.UniqueConstraint("token"),
    )
    op.create_index(
        "upload_admission_expires_at_idx",
        "upload_admission_reservations",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "upload_admission_expires_at_idx",
        table_name="upload_admission_reservations",
    )
    op.drop_table("upload_admission_reservations")
    op.drop_table("submission_settings_revisions")
