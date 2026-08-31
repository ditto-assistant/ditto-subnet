"""add claimed-lease shadow coding certification inference requests

Revision ID: b8c1e4a7d359
Revises: d4e9a1c7b258
Create Date: 2026-08-31

Public-canary inference requests bind a claimed-lease grant. They never reuse
the private-ticket request ledger or its ticket foreign keys.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b8c1e4a7d359"
down_revision: str | Sequence[str] | None = "4f2b7d9a1e63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "coding_certification_inference_requests",
        sa.Column("request_row_id", sa.UUID(), nullable=False),
        sa.Column("grant_id", sa.UUID(), nullable=False),
        sa.Column("lease_id", sa.UUID(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("request_sequence", sa.Integer(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.UUID(), nullable=False),
        sa.Column("case_id", sa.Text(), nullable=False),
        sa.Column("profile_capability_id", sa.Text(), nullable=False),
        sa.Column("inference_grant_sha256", sa.Text(), nullable=False),
        sa.Column("locked_request_sha256", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("provider_settlement_sha256", sa.Text(), nullable=True),
        sa.Column("provider_generation_id", sa.Text(), nullable=True),
        sa.Column("provider_settlement_json", sa.Text(), nullable=True),
        sa.Column("unsettled_reason", sa.Text(), nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("settled_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("weight_eligible", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "generation BETWEEN 1 AND 2147483647 "
            "AND sequence BETWEEN 1 AND 1100 "
            "AND request_sequence BETWEEN 1 AND 256 "
            "AND attempt BETWEEN 1 AND 3",
            name="coding_certification_inference_requests_order_check",
        ),
        sa.CheckConstraint(
            "inference_grant_sha256 ~ '^[0-9a-f]{64}$' "
            "AND locked_request_sha256 ~ '^[0-9a-f]{64}$' "
            "AND (provider_settlement_sha256 IS NULL OR "
            "provider_settlement_sha256 ~ '^[0-9a-f]{64}$') "
            "AND (provider_generation_id IS NULL OR "
            "(octet_length(provider_generation_id) BETWEEN 1 AND 256 "
            "AND provider_generation_id !~ '[[:space:][:cntrl:]]'))",
            name="coding_certification_inference_requests_digests_check",
        ),
        sa.CheckConstraint(
            "octet_length(case_id) BETWEEN 1 AND 256 "
            "AND octet_length(profile_capability_id) BETWEEN 1 AND 256 "
            "AND case_id !~ '[[:space:][:cntrl:]]' "
            "AND profile_capability_id !~ '[[:space:][:cntrl:]]' "
            "AND (provider_settlement_json IS NULL OR "
            "octet_length(provider_settlement_json) BETWEEN 1 AND 65536)",
            name="coding_certification_inference_requests_bounds_check",
        ),
        sa.CheckConstraint(
            "status IN ('started', 'receipt_free_retry', 'complete', "
            "'provider_failure', 'unsettled') "
            "AND ((status = 'started' "
            "AND provider_settlement_sha256 IS NULL "
            "AND provider_generation_id IS NULL "
            "AND provider_settlement_json IS NULL "
            "AND unsettled_reason IS NULL AND settled_at IS NULL) "
            "OR (status IN ('receipt_free_retry', 'complete', "
            "'provider_failure') "
            "AND provider_settlement_sha256 IS NOT NULL "
            "AND provider_settlement_json IS NOT NULL "
            "AND unsettled_reason IS NULL AND settled_at IS NOT NULL) "
            "OR (status = 'unsettled' "
            "AND provider_settlement_sha256 IS NULL "
            "AND provider_generation_id IS NULL "
            "AND provider_settlement_json IS NULL "
            "AND unsettled_reason IN ('provider_settlement_unavailable', "
            "'provider_response_lost', 'relay_infrastructure', "
            "'invalid_provider_settlement') "
            "AND settled_at IS NOT NULL)) "
            "AND (settled_at IS NULL OR settled_at >= started_at) "
            "AND weight_eligible = false",
            name="coding_certification_inference_requests_state_check",
        ),
        sa.ForeignKeyConstraint(
            ["grant_id", "lease_id"],
            [
                "coding_certification_inference_grants.grant_id",
                "coding_certification_inference_grants.lease_id",
            ],
            name="coding_certification_inference_requests_grant_fkey",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "request_row_id",
            name="coding_certification_inference_requests_pkey",
        ),
        sa.UniqueConstraint(
            "grant_id",
            "sequence",
            name="coding_certification_inference_requests_grant_sequence_key",
        ),
        sa.UniqueConstraint(
            "grant_id",
            "request_sequence",
            "attempt",
            name="coding_certification_inference_requests_request_attempt_key",
        ),
        sa.UniqueConstraint(
            "grant_id",
            "request_id",
            "attempt",
            name="coding_certification_inference_requests_request_id_attempt_key",
        ),
        sa.UniqueConstraint(
            "provider_settlement_sha256",
            name="coding_certification_inference_requests_settlement_key",
        ),
        sa.UniqueConstraint(
            "provider_generation_id",
            name="coding_certification_inference_requests_provider_generation_key",
        ),
    )
    op.create_index(
        "coding_certification_inference_requests_grant_status_idx",
        "coding_certification_inference_requests",
        ["grant_id", "status", "sequence"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "coding_certification_inference_requests_grant_status_idx",
        table_name="coding_certification_inference_requests",
    )
    op.drop_table("coding_certification_inference_requests")
