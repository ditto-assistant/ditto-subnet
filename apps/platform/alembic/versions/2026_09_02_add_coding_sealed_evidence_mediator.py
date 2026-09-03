"""add append-only Coding sealed-evidence mediator authority

Revision ID: a6e4c2d9f731
Revises: 7f2d9ab4c6e1
Create Date: 2026-09-02

The reservation fixes exact plaintext, ciphertext, envelope, opaque-key digest,
ticket, claim, and writer identity before provider contact. Finalization is a
separate append-only full-byte-verification transition.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a6e4c2d9f731"
down_revision: str | Sequence[str] | None = "7f2d9ab4c6e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "coding_sealed_evidence_reservations",
        sa.Column("reservation_id", sa.UUID(), nullable=False),
        sa.Column("ticket_id", sa.UUID(), nullable=False),
        sa.Column("claim_generation", sa.Integer(), nullable=False),
        sa.Column("validator_hotkey", sa.Text(), nullable=False),
        sa.Column("instance_id", sa.Text(), nullable=False),
        sa.Column("ticket_deadline", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("evidence_kind", sa.Text(), nullable=False),
        sa.Column("plaintext_sha256", sa.Text(), nullable=False),
        sa.Column("plaintext_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("ciphertext_sha256", sa.Text(), nullable=False),
        sa.Column("ciphertext_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("object_key_sha256", sa.Text(), nullable=False),
        sa.Column("envelope_sha256", sa.Text(), nullable=False),
        sa.Column("wrapping_key_sha256", sa.Text(), nullable=False),
        sa.Column("aad_sha256", sa.Text(), nullable=False),
        sa.Column("identity_sha256", sa.Text(), nullable=False),
        sa.Column("weight_eligible", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "claim_generation BETWEEN 1 AND 2147483647 "
            "AND validator_hotkey ~ '^[1-9A-HJ-NP-Za-km-z]{47,48}$' "
            "AND octet_length(instance_id) BETWEEN 1 AND 128 "
            "AND instance_id !~ '[[:space:][:cntrl:]]' "
            "AND plaintext_sha256 ~ '^[0-9a-f]{64}$' "
            "AND ciphertext_sha256 ~ '^[0-9a-f]{64}$' "
            "AND object_key_sha256 ~ '^[0-9a-f]{64}$' "
            "AND envelope_sha256 ~ '^[0-9a-f]{64}$' "
            "AND wrapping_key_sha256 ~ '^[0-9a-f]{64}$' "
            "AND aad_sha256 ~ '^[0-9a-f]{64}$' "
            "AND identity_sha256 ~ '^[0-9a-f]{64}$' "
            "AND ciphertext_size_bytes = plaintext_size_bytes + 16 "
            "AND weight_eligible = false",
            name="coding_sealed_evidence_reservations_authority_check",
        ),
        sa.CheckConstraint(
            "(evidence_kind = 'authoring-transcript' "
            "AND plaintext_size_bytes BETWEEN 1 AND 536870912) OR "
            "(evidence_kind = 'frozen-submission' "
            "AND plaintext_size_bytes BETWEEN 1 AND 134217728) OR "
            "(evidence_kind IN ('authoring-publication-request', "
            "'terminal-publication-request') "
            "AND plaintext_size_bytes BETWEEN 1 AND 4194304) OR "
            "(evidence_kind IN ('authoring-publication-acknowledgement', "
            "'terminal-publication-acknowledgement') "
            "AND plaintext_size_bytes BETWEEN 1 AND 1048576)",
            name="coding_sealed_evidence_reservations_kind_size_check",
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"],
            ["coding_shadow_tickets.ticket_id"],
            name="coding_sealed_evidence_reservations_ticket_fkey",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "reservation_id",
            name="coding_sealed_evidence_reservations_pkey",
        ),
        sa.UniqueConstraint(
            "identity_sha256",
            name="coding_sealed_evidence_reservations_identity_sha_key",
        ),
        sa.UniqueConstraint(
            "ticket_id",
            "claim_generation",
            "evidence_kind",
            name="coding_sealed_evidence_reservations_ticket_generation_kind_key",
        ),
        sa.UniqueConstraint(
            "reservation_id",
            "identity_sha256",
            name="coding_sealed_evidence_reservations_identity_key",
        ),
    )
    op.create_index(
        "coding_sealed_evidence_reservations_ticket_created_idx",
        "coding_sealed_evidence_reservations",
        ["ticket_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "coding_sealed_evidence_finalizations",
        sa.Column("reservation_id", sa.UUID(), nullable=False),
        sa.Column("identity_sha256", sa.Text(), nullable=False),
        sa.Column("storage_status", sa.Text(), nullable=False),
        sa.Column("weight_eligible", sa.Boolean(), nullable=False),
        sa.Column(
            "finalized_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "identity_sha256 ~ '^[0-9a-f]{64}$' "
            "AND storage_status IN ('uploaded', 'reused') "
            "AND weight_eligible = false",
            name="coding_sealed_evidence_finalizations_authority_check",
        ),
        sa.ForeignKeyConstraint(
            ["reservation_id", "identity_sha256"],
            [
                "coding_sealed_evidence_reservations.reservation_id",
                "coding_sealed_evidence_reservations.identity_sha256",
            ],
            name="coding_sealed_evidence_finalizations_reservation_fkey",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "reservation_id",
            name="coding_sealed_evidence_finalizations_pkey",
        ),
    )
    op.create_index(
        "coding_sealed_evidence_finalizations_finalized_idx",
        "coding_sealed_evidence_finalizations",
        ["finalized_at"],
        unique=False,
    )

    for table in (
        "coding_sealed_evidence_reservations",
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
        "coding_sealed_evidence_reservations",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    op.drop_index(
        "coding_sealed_evidence_finalizations_finalized_idx",
        table_name="coding_sealed_evidence_finalizations",
    )
    op.drop_table("coding_sealed_evidence_finalizations")
    op.drop_index(
        "coding_sealed_evidence_reservations_ticket_created_idx",
        table_name="coding_sealed_evidence_reservations",
    )
    op.drop_table("coding_sealed_evidence_reservations")
