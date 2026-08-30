"""add qualified coding certification leases

Revision ID: b3e7c1a0d492
Revises: a0d6c3e9f521
Create Date: 2026-08-30

Shadow-only control-plane leases for the public coding canary. They never
participate in score aggregation, weights, or emissions.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b3e7c1a0d492"
down_revision: str | Sequence[str] | None = "a0d6c3e9f521"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "coding_certification_leases",
        sa.Column("lease_id", sa.UUID(), nullable=False),
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("artifact_sha256", sa.Text(), nullable=False),
        sa.Column("screened_image_sha256", sa.Text(), nullable=False),
        sa.Column("screened_image_id", sa.Text(), nullable=False),
        sa.Column("screened_image_ref", sa.Text(), nullable=False),
        sa.Column("screened_image_upload_id", sa.UUID(), nullable=False),
        sa.Column("validator_hotkey", sa.Text(), nullable=False),
        sa.Column("bench_version", sa.Integer(), nullable=False),
        sa.Column("coding_contract_version", sa.Integer(), nullable=False),
        sa.Column("core_qualification_observation_id", sa.UUID(), nullable=False),
        sa.Column("core_qualification_policy_checksum", sa.Text(), nullable=False),
        sa.Column("canary_manifest_sha256", sa.Text(), nullable=False),
        sa.Column("runner_plan_sha256", sa.Text(), nullable=False),
        sa.Column("grader_plan_sha256", sa.Text(), nullable=False),
        sa.Column("resource_profile_sha256", sa.Text(), nullable=False),
        sa.Column("inference_policy_sha256", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("weight_eligible", sa.Boolean(), nullable=False),
        sa.Column("issued_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("deadline", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("aborted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("authority", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "artifact_sha256 ~ '^[0-9a-f]{64}$' "
            "AND screened_image_sha256 ~ '^[0-9a-f]{64}$' "
            "AND core_qualification_policy_checksum ~ '^[0-9a-f]{64}$' "
            "AND canary_manifest_sha256 ~ '^[0-9a-f]{64}$' "
            "AND runner_plan_sha256 ~ '^[0-9a-f]{64}$' "
            "AND grader_plan_sha256 ~ '^[0-9a-f]{64}$' "
            "AND resource_profile_sha256 ~ '^[0-9a-f]{64}$' "
            "AND inference_policy_sha256 ~ '^[0-9a-f]{64}$'",
            name="coding_certification_leases_sha_check",
        ),
        sa.CheckConstraint(
            "validator_hotkey ~ '^[1-9A-HJ-NP-Za-km-z]{47,48}$'",
            name="coding_certification_leases_hotkey_check",
        ),
        sa.CheckConstraint(
            "coding_contract_version = 1 AND bench_version >= 7",
            name="coding_certification_leases_version_check",
        ),
        sa.CheckConstraint(
            "status IN ('issued', 'claimed', 'aborted', 'expired')",
            name="coding_certification_leases_status_check",
        ),
        sa.CheckConstraint(
            "weight_eligible = false",
            name="coding_certification_leases_weight_ineligible",
        ),
        sa.CheckConstraint(
            "issued_at < deadline AND deadline <= issued_at + interval '30 minutes'",
            name="coding_certification_leases_deadline_check",
        ),
        sa.CheckConstraint(
            "octet_length(screened_image_id) BETWEEN 1 AND 256 "
            "AND octet_length(screened_image_ref) BETWEEN 1 AND 512 "
            "AND screened_image_id !~ '[[:space:][:cntrl:]]' "
            "AND screened_image_ref !~ '[[:cntrl:]]'",
            name="coding_certification_leases_image_identity_check",
        ),
        sa.CheckConstraint(
            "(status = 'issued' AND claimed_at IS NULL AND aborted_at IS NULL) "
            "OR (status = 'claimed' AND claimed_at IS NOT NULL "
            "AND claimed_at >= issued_at AND claimed_at < deadline "
            "AND aborted_at IS NULL) "
            "OR (status = 'aborted' AND aborted_at IS NOT NULL "
            "AND aborted_at >= issued_at AND claimed_at IS NULL) "
            "OR (status = 'expired' AND claimed_at IS NULL AND aborted_at IS NULL)",
            name="coding_certification_leases_lifecycle_check",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.agent_id"],
            ondelete="CASCADE",
            name="coding_certification_leases_agent_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["core_qualification_observation_id"],
            ["core_qualification_observations.observation_id"],
            ondelete="RESTRICT",
            name="coding_certification_leases_observation_fkey",
        ),
        sa.PrimaryKeyConstraint("lease_id"),
    )
    op.create_index(
        "coding_certification_leases_inflight_idx",
        "coding_certification_leases",
        [
            "agent_id",
            "artifact_sha256",
            "screened_image_sha256",
            "bench_version",
            "coding_contract_version",
        ],
        unique=True,
        postgresql_where=sa.text("status IN ('issued', 'claimed')"),
    )
    op.create_index(
        "coding_certification_leases_validator_deadline_idx",
        "coding_certification_leases",
        ["validator_hotkey", "deadline"],
    )


def downgrade() -> None:
    op.drop_index(
        "coding_certification_leases_validator_deadline_idx",
        table_name="coding_certification_leases",
    )
    op.drop_index(
        "coding_certification_leases_inflight_idx",
        table_name="coding_certification_leases",
    )
    op.drop_table("coding_certification_leases")
