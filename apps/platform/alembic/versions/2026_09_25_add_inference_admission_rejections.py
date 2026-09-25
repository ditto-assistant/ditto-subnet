"""Persist sanitized inference admission rejections.

Revision ID: c3a91e7b2d40
Revises: d18b0f5a72c9
Create Date: 2026-09-24

Rows older than 14 days are pruned by the writer. The table never stores
request bodies, prompts, headers, or provider credentials.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c3a91e7b2d40"
down_revision: str | Sequence[str] | None = "d18b0f5a72c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Keep grant_not_servable reserved for a future pre-reservation refusal.
_CODES = (
    "'invalid_json', 'invalid_schema', 'request_too_large', "
    "'stale_session', 'model_not_allowed', 'grant_not_servable'"
)


def upgrade() -> None:
    op.create_table(
        "inference_admission_rejections",
        sa.Column("rejection_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("lane", sa.Text(), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=False),
        sa.Column("admission_code", sa.Text(), nullable=False),
        sa.Column("grant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("validator_hotkey", sa.Text(), nullable=True),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_bytes", sa.Integer(), nullable=False),
        sa.Column("byte_limit", sa.Integer(), nullable=True),
        sa.Column("platform_revision", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "lane IN ('inference', 'embedding')",
            name="inference_admission_rejections_lane_check",
        ),
        sa.CheckConstraint(
            "http_status IN (400, 403, 409, 413)",
            name="inference_admission_rejections_status_check",
        ),
        sa.CheckConstraint(
            f"admission_code IN ({_CODES})",
            name="inference_admission_rejections_code_check",
        ),
        sa.CheckConstraint(
            "request_bytes >= 0",
            name="inference_admission_rejections_bytes_check",
        ),
        sa.CheckConstraint(
            "byte_limit IS NULL OR byte_limit >= 0",
            name="inference_admission_rejections_limit_check",
        ),
        sa.CheckConstraint(
            "length(platform_revision) BETWEEN 1 AND 64",
            name="inference_admission_rejections_revision_check",
        ),
        sa.CheckConstraint(
            "validator_hotkey IS NULL OR length(validator_hotkey) BETWEEN 1 AND 120",
            name="inference_admission_rejections_hotkey_check",
        ),
        sa.PrimaryKeyConstraint("rejection_id"),
    )
    op.create_index(
        "inference_admission_rejections_grant_idx",
        "inference_admission_rejections",
        ["grant_id", "created_at"],
    )
    op.create_index(
        "inference_admission_rejections_created_idx",
        "inference_admission_rejections",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "inference_admission_rejections_created_idx",
        table_name="inference_admission_rejections",
    )
    op.drop_index(
        "inference_admission_rejections_grant_idx",
        table_name="inference_admission_rejections",
    )
    op.drop_table("inference_admission_rejections")
