"""Add independent report-only verification replay leases and receipts.

Revision ID: 6c81f4d239ba
Revises: 8a4d27c0f639
Create Date: 2026-09-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "6c81f4d239ba"
down_revision: str | Sequence[str] | None = "c7e49af025ab"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "screening_verification_replays",
        sa.Column("replay_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("quarantine_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_attempt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("artifact_sha256", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("image_upload_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("image_sha256", sa.Text(), nullable=True),
        sa.Column("image_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("image_id", sa.Text(), nullable=True),
        sa.Column("image_staging_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("image_verified_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("image_verified_storage_key", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("worker_hotkey", sa.Text(), nullable=True),
        sa.Column("lease_deadline", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("failure_code", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.agent_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("request_id", name="svrp_request_id_key"),
        sa.ForeignKeyConstraint(
            ["quarantine_id"],
            ["screening_quarantines.quarantine_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_attempt_id"], ["screening_attempts.attempt_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["image_upload_id"],
            ["screened_image_uploads.image_upload_id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "artifact_sha256 ~ '^[0-9a-f]{64}$'", name="svrp_artifact_sha_check"
        ),
        sa.CheckConstraint(
            "image_sha256 IS NULL OR image_sha256 ~ '^[0-9a-f]{64}$'",
            name="svrp_image_sha_check",
        ),
        sa.CheckConstraint(
            "(image_sha256 IS NULL AND image_size_bytes IS NULL AND image_id IS NULL "
            "AND image_verified_at IS NULL) OR "
            "(image_sha256 IS NOT NULL AND image_size_bytes > 0 "
            "AND image_id IS NOT NULL)",
            name="svrp_image_metadata_check",
        ),
        sa.CheckConstraint(
            "image_upload_id IS NOT NULL OR image_verified_at IS NULL OR "
            "image_verified_storage_key IS NOT NULL",
            name="svrp_verified_storage_key_check",
        ),
        sa.CheckConstraint("policy_version = 13", name="svrp_policy_check"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed')",
            name="svrp_status_check",
        ),
    )
    op.create_index(
        "svrp_one_active_source_idx",
        "screening_verification_replays",
        ["agent_id", "source_attempt_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_index(
        "svrp_claim_idx", "screening_verification_replays", ["status", "created_at"]
    )
    op.execute(
        """CREATE FUNCTION reject_verification_replay_binding_change()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF (NEW.request_id, NEW.agent_id, NEW.quarantine_id,
                NEW.source_attempt_id, NEW.artifact_sha256,
                NEW.policy_version, NEW.image_upload_id)
               IS DISTINCT FROM
               (OLD.request_id, OLD.agent_id, OLD.quarantine_id,
                OLD.source_attempt_id, OLD.artifact_sha256,
                OLD.policy_version, OLD.image_upload_id) THEN
                RAISE EXCEPTION 'verification replay source binding is immutable';
            END IF;
            IF OLD.image_verified_storage_key IS NOT NULL AND
               NEW.image_verified_storage_key IS DISTINCT FROM
               OLD.image_verified_storage_key THEN
                RAISE EXCEPTION 'verification replay verified image key is immutable';
            END IF;
            IF OLD.image_verified_at IS NOT NULL AND
               NEW.image_verified_at IS DISTINCT FROM OLD.image_verified_at THEN
                RAISE EXCEPTION 'verification replay image verification is immutable';
            END IF;
            RETURN NEW;
        END;
        $$"""
    )
    op.execute(
        """CREATE TRIGGER verification_replay_binding_immutable
        BEFORE UPDATE ON screening_verification_replays
        FOR EACH ROW EXECUTE FUNCTION reject_verification_replay_binding_change()"""
    )
    op.create_table(
        "screening_verification_replay_receipts",
        sa.Column("receipt_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("replay_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("check_code", sa.Text(), nullable=False),
        sa.Column("evidence_sha256", sa.Text(), nullable=False),
        sa.Column("worker_hotkey", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["replay_id"],
            ["screening_verification_replays.replay_id"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "length(evidence_sha256) = 64", name="svrr_evidence_sha_check"
        ),
        sa.CheckConstraint(
            "length(check_code) BETWEEN 1 AND 64", name="svrr_check_code_check"
        ),
    )
    op.create_index(
        "svrr_replay_code_idx",
        "screening_verification_replay_receipts",
        ["replay_id", "check_code"],
        unique=True,
    )
    op.execute(
        """CREATE FUNCTION reject_verification_replay_receipt_change()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'verification replay receipts are append-only';
        END;
        $$"""
    )
    op.execute(
        """CREATE TRIGGER verification_replay_receipt_immutable
        BEFORE UPDATE OR DELETE ON screening_verification_replay_receipts
        FOR EACH ROW EXECUTE FUNCTION reject_verification_replay_receipt_change()"""
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER verification_replay_receipt_immutable "
        "ON screening_verification_replay_receipts"
    )
    op.execute("DROP FUNCTION reject_verification_replay_receipt_change()")
    op.drop_index(
        "svrr_replay_code_idx", table_name="screening_verification_replay_receipts"
    )
    op.drop_table("screening_verification_replay_receipts")
    op.execute(
        "DROP TRIGGER verification_replay_binding_immutable "
        "ON screening_verification_replays"
    )
    op.execute("DROP FUNCTION reject_verification_replay_binding_change()")
    op.drop_index("svrp_claim_idx", table_name="screening_verification_replays")
    op.drop_index(
        "svrp_one_active_source_idx", table_name="screening_verification_replays"
    )
    op.drop_table("screening_verification_replays")
