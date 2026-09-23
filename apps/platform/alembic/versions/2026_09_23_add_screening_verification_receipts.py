"""Add artifact-bound screening verification evidence receipts.

Revision ID: 7b62d9c083f1
Revises: 3c9d5e7a1b42
Create Date: 2026-09-23

This is storage and read visibility only. No writer or policy decision consumes
these rows until a trusted mandatory-verification runner exists.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "7b62d9c083f1"
down_revision: str | Sequence[str] | None = "3c9d5e7a1b42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "screening_verification_receipts",
        sa.Column("receipt_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("artifact_sha256", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("check_code", sa.Text(), nullable=False),
        sa.Column("evidence_sha256", sa.Text(), nullable=False),
        sa.Column("image_sha256", sa.Text(), nullable=True),
        sa.Column("profile_sha256", sa.Text(), nullable=True),
        sa.Column("challenge_manifest_sha256", sa.Text(), nullable=True),
        sa.Column("worker_hotkey", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.agent_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["attempt_id"], ["screening_attempts.attempt_id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "length(artifact_sha256) = 64", name="svr_artifact_sha_check"
        ),
        sa.CheckConstraint(
            "length(evidence_sha256) = 64", name="svr_evidence_sha_check"
        ),
        sa.CheckConstraint(
            "image_sha256 IS NULL OR length(image_sha256) = 64",
            name="svr_image_sha_check",
        ),
        sa.CheckConstraint(
            "profile_sha256 IS NULL OR length(profile_sha256) = 64",
            name="svr_profile_sha_check",
        ),
        sa.CheckConstraint(
            "challenge_manifest_sha256 IS NULL "
            "OR length(challenge_manifest_sha256) = 64",
            name="svr_manifest_sha_check",
        ),
        sa.CheckConstraint("policy_version > 0", name="svr_policy_version_check"),
        sa.CheckConstraint(
            "length(check_code) BETWEEN 1 AND 64", name="svr_check_code_check"
        ),
        sa.CheckConstraint(
            "length(worker_hotkey) BETWEEN 1 AND 120", name="svr_worker_check"
        ),
    )
    op.create_index(
        "svr_attempt_created_idx",
        "screening_verification_receipts",
        ["attempt_id", "created_at", "receipt_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "svr_attempt_created_idx", table_name="screening_verification_receipts"
    )
    op.drop_table("screening_verification_receipts")
