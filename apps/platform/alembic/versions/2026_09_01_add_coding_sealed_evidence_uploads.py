"""add append-only sealed coding evidence upload authority

Revision ID: d3a9e6b4c812
Revises: 7f2d9ab4c6e1
Create Date: 2026-09-01

The Platform database is the authority for one exact evidence reservation and
its accepted finalization.  No bucket, object key, presigned URL, credentials,
or worker activation is introduced here.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d3a9e6b4c812"
down_revision: str | Sequence[str] | None = "7f2d9ab4c6e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "coding_sealed_evidence_uploads",
        sa.Column("upload_id", sa.UUID(), nullable=False),
        sa.Column("ticket_id", sa.UUID(), nullable=False),
        sa.Column("claim_generation", sa.Integer(), nullable=False),
        sa.Column("evidence_kind", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("weight_eligible", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "claim_generation BETWEEN 1 AND 2147483647 "
            "AND sha256 ~ '^[0-9a-f]{64}$' "
            "AND content_type = 'application/octet-stream' "
            "AND weight_eligible = false",
            name="coding_sealed_evidence_uploads_authority_check",
        ),
        sa.CheckConstraint(
            "(evidence_kind = 'authoring-transcript' "
            "AND size_bytes BETWEEN 1 AND 536870912) OR "
            "(evidence_kind = 'frozen-submission' "
            "AND size_bytes BETWEEN 1 AND 134217728) OR "
            "(evidence_kind IN ('authoring-publication-request', "
            "'terminal-publication-request') "
            "AND size_bytes BETWEEN 1 AND 4194304) OR "
            "(evidence_kind IN ('authoring-publication-acknowledgement', "
            "'terminal-publication-acknowledgement') "
            "AND size_bytes BETWEEN 1 AND 1048576)",
            name="coding_sealed_evidence_uploads_kind_size_check",
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"],
            ["coding_shadow_tickets.ticket_id"],
            name="coding_sealed_evidence_uploads_ticket_fkey",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("upload_id", name="coding_sealed_evidence_uploads_pkey"),
        sa.UniqueConstraint(
            "ticket_id",
            "claim_generation",
            "evidence_kind",
            name="coding_sealed_evidence_uploads_ticket_generation_kind_key",
        ),
        sa.UniqueConstraint(
            "upload_id",
            "ticket_id",
            "claim_generation",
            "evidence_kind",
            "sha256",
            "size_bytes",
            name="coding_sealed_evidence_uploads_upload_authority_key",
        ),
    )
    op.create_index(
        "coding_sealed_evidence_uploads_ticket_created_idx",
        "coding_sealed_evidence_uploads",
        ["ticket_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "coding_sealed_evidence_finalizations",
        sa.Column("upload_id", sa.UUID(), nullable=False),
        sa.Column("ticket_id", sa.UUID(), nullable=False),
        sa.Column("claim_generation", sa.Integer(), nullable=False),
        sa.Column("evidence_kind", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("weight_eligible", sa.Boolean(), nullable=False),
        sa.Column(
            "finalized_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "claim_generation BETWEEN 1 AND 2147483647 "
            "AND sha256 ~ '^[0-9a-f]{64}$' "
            "AND weight_eligible = false",
            name="coding_sealed_evidence_finalizations_authority_check",
        ),
        sa.ForeignKeyConstraint(
            [
                "upload_id",
                "ticket_id",
                "claim_generation",
                "evidence_kind",
                "sha256",
                "size_bytes",
            ],
            [
                "coding_sealed_evidence_uploads.upload_id",
                "coding_sealed_evidence_uploads.ticket_id",
                "coding_sealed_evidence_uploads.claim_generation",
                "coding_sealed_evidence_uploads.evidence_kind",
                "coding_sealed_evidence_uploads.sha256",
                "coding_sealed_evidence_uploads.size_bytes",
            ],
            name="coding_sealed_evidence_finalizations_upload_fkey",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "upload_id", name="coding_sealed_evidence_finalizations_pkey"
        ),
    )
    op.create_index(
        "coding_sealed_evidence_finalizations_ticket_finalized_idx",
        "coding_sealed_evidence_finalizations",
        ["ticket_id", "finalized_at"],
        unique=False,
    )

    for table in (
        "coding_sealed_evidence_uploads",
        "coding_sealed_evidence_finalizations",
    ):
        op.execute(
            f"""
            CREATE TRIGGER {table}_append_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION guard_coding_catalog_append_only()
            """
        )


def downgrade() -> None:
    for table in (
        "coding_sealed_evidence_finalizations",
        "coding_sealed_evidence_uploads",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    op.drop_index(
        "coding_sealed_evidence_finalizations_ticket_finalized_idx",
        table_name="coding_sealed_evidence_finalizations",
    )
    op.drop_table("coding_sealed_evidence_finalizations")
    op.drop_index(
        "coding_sealed_evidence_uploads_ticket_created_idx",
        table_name="coding_sealed_evidence_uploads",
    )
    op.drop_table("coding_sealed_evidence_uploads")
