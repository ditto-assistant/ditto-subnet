"""store screener-built validator images

Revision ID: e4a2b9c71d60
Revises: b7c1e9d4a2f8
Create Date: 2026-07-17 00:20:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e4a2b9c71d60"
down_revision: str | Sequence[str] | None = "b7c1e9d4a2f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agents", sa.Column("screened_image_sha256", sa.Text(), nullable=True)
    )
    op.add_column(
        "agents", sa.Column("screened_image_size_bytes", sa.BigInteger(), nullable=True)
    )
    op.add_column("agents", sa.Column("screened_image_id", sa.Text(), nullable=True))
    op.add_column("agents", sa.Column("screened_image_ref", sa.Text(), nullable=True))
    op.add_column(
        "agents",
        sa.Column("screened_image_upload_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "agents",
        sa.Column(
            "screened_image_verified_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.create_check_constraint(
        "agents_screened_image_fields_check",
        "agents",
        "(screened_image_sha256 IS NULL AND screened_image_size_bytes IS NULL "
        "AND screened_image_id IS NULL AND screened_image_ref IS NULL "
        "AND screened_image_upload_id IS NULL "
        "AND screened_image_verified_at IS NULL) OR "
        "(length(screened_image_sha256) = 64 AND screened_image_size_bytes > 0 "
        "AND length(screened_image_id) = 71 AND length(screened_image_ref) > 0 "
        "AND screened_image_upload_id IS NOT NULL "
        "AND screened_image_verified_at IS NOT NULL)",
    )
    op.create_table(
        "screened_image_uploads",
        sa.Column("image_upload_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("screener_hotkey", sa.Text(), nullable=False),
        sa.Column("storage_upload_id", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("image_id", sa.Text(), nullable=False),
        sa.Column("image_ref", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="initiated", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('initiated', 'verified', 'aborted')",
            name="screened_image_uploads_status_check",
        ),
        sa.CheckConstraint("size_bytes > 0", name="screened_image_uploads_size_check"),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.agent_id"],
            name="screened_image_uploads_agent_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["screening_attempts.attempt_id"],
            name="screened_image_uploads_attempt_id_fkey",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("image_upload_id"),
    )
    op.create_index(
        "screened_image_uploads_attempt_idx",
        "screened_image_uploads",
        ["attempt_id"],
    )
    op.create_index(
        "screened_image_uploads_status_expires_idx",
        "screened_image_uploads",
        ["status", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "screened_image_uploads_status_expires_idx",
        table_name="screened_image_uploads",
    )
    op.drop_index(
        "screened_image_uploads_attempt_idx", table_name="screened_image_uploads"
    )
    op.drop_table("screened_image_uploads")
    op.drop_constraint("agents_screened_image_fields_check", "agents", type_="check")
    op.drop_column("agents", "screened_image_verified_at")
    op.drop_column("agents", "screened_image_upload_id")
    op.drop_column("agents", "screened_image_ref")
    op.drop_column("agents", "screened_image_id")
    op.drop_column("agents", "screened_image_size_bytes")
    op.drop_column("agents", "screened_image_sha256")
