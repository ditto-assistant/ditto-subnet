"""add append-only shadow coding capability certifications

Revision ID: c7d91f4a2e60
Revises: c3f6a9e2b514
Create Date: 2026-08-29

The table records validator-signed, exact-artifact capability receipts. It is
deliberately disconnected from scores, ranking, and weights.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c7d91f4a2e60"
down_revision: str | Sequence[str] | None = "c3f6a9e2b514"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "coding_capability_certifications",
        sa.Column("certification_row_id", sa.UUID(), nullable=False),
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("artifact_sha256", sa.Text(), nullable=False),
        sa.Column("screened_image_sha256", sa.Text(), nullable=False),
        sa.Column("validator_hotkey", sa.Text(), nullable=False),
        sa.Column("bench_version", sa.Integer(), nullable=False),
        sa.Column("ticket_deadline", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("coding_contract_version", sa.Integer(), nullable=False),
        sa.Column("certification_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("failure_stage", sa.Text(), nullable=True),
        sa.Column("failure_code", sa.Text(), nullable=True),
        sa.Column("certification_sha256", sa.Text(), nullable=False),
        sa.Column("canary_manifest_sha256", sa.Text(), nullable=False),
        sa.Column("transcript_object_key", sa.Text(), nullable=True),
        sa.Column("frozen_submission_object_key", sa.Text(), nullable=True),
        sa.Column("issued_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("weight_eligible", sa.Boolean(), nullable=False),
        sa.Column("receipt", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("signature", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "artifact_sha256 ~ '^[0-9a-f]{64}$'",
            name="coding_certifications_artifact_sha_check",
        ),
        sa.CheckConstraint(
            "screened_image_sha256 ~ '^[0-9a-f]{64}$'",
            name="coding_certifications_image_sha_check",
        ),
        sa.CheckConstraint(
            "certification_sha256 ~ '^[0-9a-f]{64}$'",
            name="coding_certifications_receipt_sha_check",
        ),
        sa.CheckConstraint(
            "canary_manifest_sha256 ~ '^[0-9a-f]{64}$'",
            name="coding_certifications_canary_sha_check",
        ),
        sa.CheckConstraint(
            "signature ~ '^[0-9a-f]{128}$'",
            name="coding_certifications_signature_check",
        ),
        sa.CheckConstraint(
            "coding_contract_version > 0 AND bench_version > 0",
            name="coding_certifications_versions_positive",
        ),
        sa.CheckConstraint(
            "status IN ('unsupported', 'failed', 'certified')",
            name="coding_certifications_status_check",
        ),
        sa.CheckConstraint(
            "weight_eligible = false",
            name="coding_certifications_weight_ineligible",
        ),
        sa.CheckConstraint(
            "expires_at > issued_at AND expires_at <= issued_at + interval '24 hours'",
            name="coding_certifications_expiry_check",
        ),
        sa.CheckConstraint(
            "((status = 'certified' AND failure_stage IS NULL "
            "AND failure_code IS NULL) "
            "OR (status <> 'certified' AND failure_stage IS NOT NULL "
            "AND failure_code IS NOT NULL))",
            name="coding_certifications_failure_shape",
        ),
        sa.CheckConstraint(
            "transcript_object_key IS NULL "
            "OR transcript_object_key ~ '^sha256/[0-9a-f]{64}$'",
            name="coding_certifications_transcript_key_check",
        ),
        sa.CheckConstraint(
            "frozen_submission_object_key IS NULL "
            "OR frozen_submission_object_key ~ '^sha256/[0-9a-f]{64}$'",
            name="coding_certifications_frozen_key_check",
        ),
        sa.CheckConstraint(
            "status <> 'certified' OR (transcript_object_key IS NOT NULL "
            "AND frozen_submission_object_key IS NOT NULL)",
            name="coding_certifications_certified_artifacts",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.agent_id"],
            name="coding_certifications_agent_id_fkey",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "certification_row_id", name="coding_capability_certifications_pkey"
        ),
        sa.UniqueConstraint(
            "agent_id",
            "validator_hotkey",
            "coding_contract_version",
            "certification_id",
            name="coding_certifications_identity_key",
        ),
    )
    op.create_index(
        "coding_certifications_agent_created_idx",
        "coding_capability_certifications",
        ["agent_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "coding_certifications_active_idx",
        "coding_capability_certifications",
        ["agent_id", "expires_at", "validator_hotkey"],
        unique=False,
        postgresql_where=sa.text("status = 'certified'"),
    )


def downgrade() -> None:
    op.drop_index(
        "coding_certifications_active_idx",
        table_name="coding_capability_certifications",
    )
    op.drop_index(
        "coding_certifications_agent_created_idx",
        table_name="coding_capability_certifications",
    )
    op.drop_table("coding_capability_certifications")
