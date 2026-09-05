"""add append-only private Coding v2 release registry

Revision ID: b7f3c8a12d49
Revises: a6e4c2d9f731
Create Date: 2026-09-05

Only digest authorities and operator audit metadata are stored. Private object
bytes, object keys, provider coordinates, credentials, and unwrap material are
excluded, and every row is permanently non-selectable and weight-ineligible.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b7f3c8a12d49"
down_revision: str | Sequence[str] | None = "a6e4c2d9f731"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "coding_private_v2_releases",
        sa.Column("release_row_id", sa.UUID(), nullable=False),
        sa.Column("corpus_release_id", sa.Text(), nullable=False),
        sa.Column("coding_contract_version", sa.Integer(), nullable=False),
        sa.Column("private_release_sha256", sa.Text(), nullable=False),
        sa.Column("catalog_sha256", sa.Text(), nullable=False),
        sa.Column("catalog_merkle_root", sa.Text(), nullable=False),
        sa.Column("payload_sha256", sa.Text(), nullable=False),
        sa.Column("transport_sha256", sa.Text(), nullable=False),
        sa.Column("wrapping_key_sha256", sa.Text(), nullable=False),
        sa.Column("publication_receipt_sha256", sa.Text(), nullable=False),
        sa.Column("provider_probe_receipt_sha256", sa.Text(), nullable=False),
        sa.Column("private_input_authority_sha256", sa.Text(), nullable=False),
        sa.Column("curator_signing_key_sha256", sa.Text(), nullable=False),
        sa.Column("publication_source_sha", sa.Text(), nullable=False),
        sa.Column("publication_object_count", sa.Integer(), nullable=False),
        sa.Column("previous_registration_sha256", sa.Text(), nullable=True),
        sa.Column("registration_sha256", sa.Text(), nullable=False),
        sa.Column(
            "registration_authority",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("shadow_only", sa.Boolean(), nullable=False),
        sa.Column("selectable", sa.Boolean(), nullable=False),
        sa.Column("weight_eligible", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "coding_contract_version = 2 AND shadow_only = true "
            "AND selectable = false AND weight_eligible = false "
            "AND publication_object_count BETWEEN 1 AND 10000 "
            "AND previous_registration_sha256 IS NULL",
            name="coding_private_v2_releases_contract_check",
        ),
        sa.CheckConstraint(
            "private_release_sha256 ~ '^[0-9a-f]{64}$' "
            "AND catalog_sha256 ~ '^[0-9a-f]{64}$' "
            "AND catalog_merkle_root ~ '^[0-9a-f]{64}$' "
            "AND payload_sha256 ~ '^[0-9a-f]{64}$' "
            "AND transport_sha256 ~ '^[0-9a-f]{64}$' "
            "AND wrapping_key_sha256 ~ '^[0-9a-f]{64}$' "
            "AND publication_receipt_sha256 ~ '^[0-9a-f]{64}$' "
            "AND provider_probe_receipt_sha256 ~ '^[0-9a-f]{64}$' "
            "AND private_input_authority_sha256 ~ '^[0-9a-f]{64}$' "
            "AND curator_signing_key_sha256 ~ '^[0-9a-f]{64}$' "
            "AND registration_sha256 ~ '^[0-9a-f]{64}$'",
            name="coding_private_v2_releases_hashes_check",
        ),
        sa.CheckConstraint(
            "publication_source_sha ~ '^[0-9a-f]{40}$'",
            name="coding_private_v2_releases_source_check",
        ),
        sa.CheckConstraint(
            "octet_length(corpus_release_id) BETWEEN 1 AND 256 "
            "AND corpus_release_id !~ '[[:space:][:cntrl:]]'",
            name="coding_private_v2_releases_identifier_check",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) >= 8 AND length(trim(actor)) BETWEEN 1 AND 120",
            name="coding_private_v2_releases_audit_check",
        ),
        sa.PrimaryKeyConstraint(
            "release_row_id", name="coding_private_v2_releases_pkey"
        ),
        sa.UniqueConstraint(
            "corpus_release_id",
            name="coding_private_v2_releases_corpus_release_id_key",
        ),
        sa.UniqueConstraint(
            "transport_sha256",
            name="coding_private_v2_releases_transport_sha256_key",
        ),
        sa.UniqueConstraint(
            "publication_receipt_sha256",
            name="coding_private_v2_releases_publication_receipt_sha256_key",
        ),
        sa.UniqueConstraint(
            "registration_sha256",
            name="coding_private_v2_releases_registration_sha256_key",
        ),
        sa.UniqueConstraint(
            "release_row_id",
            "registration_sha256",
            name="coding_private_v2_releases_row_registration_key",
        ),
    )
    op.create_index(
        "coding_private_v2_releases_created_idx",
        "coding_private_v2_releases",
        ["created_at"],
        unique=False,
    )

    op.create_table(
        "coding_private_v2_release_events",
        sa.Column("event_id", sa.UUID(), nullable=False),
        sa.Column("release_row_id", sa.UUID(), nullable=False),
        sa.Column("expected_registration_sha256", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("event_sha256", sa.Text(), nullable=False),
        sa.Column("shadow_only", sa.Boolean(), nullable=False),
        sa.Column("selectable", sa.Boolean(), nullable=False),
        sa.Column("weight_eligible", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action IN ('quarantined', 'retired') "
            "AND shadow_only = true AND selectable = false "
            "AND weight_eligible = false",
            name="coding_private_v2_release_events_contract_check",
        ),
        sa.CheckConstraint(
            "expected_registration_sha256 ~ '^[0-9a-f]{64}$' "
            "AND event_sha256 ~ '^[0-9a-f]{64}$'",
            name="coding_private_v2_release_events_hashes_check",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) >= 8 AND length(trim(actor)) BETWEEN 1 AND 120",
            name="coding_private_v2_release_events_audit_check",
        ),
        sa.ForeignKeyConstraint(
            ["release_row_id", "expected_registration_sha256"],
            [
                "coding_private_v2_releases.release_row_id",
                "coding_private_v2_releases.registration_sha256",
            ],
            name="coding_private_v2_release_events_release_fkey",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "event_id", name="coding_private_v2_release_events_pkey"
        ),
        sa.UniqueConstraint(
            "event_sha256",
            name="coding_private_v2_release_events_event_sha256_key",
        ),
        sa.UniqueConstraint(
            "release_row_id",
            "action",
            name="coding_private_v2_release_events_release_action_key",
        ),
    )
    op.create_index(
        "coding_private_v2_release_events_release_created_idx",
        "coding_private_v2_release_events",
        ["release_row_id", "created_at"],
        unique=False,
    )

    for table in (
        "coding_private_v2_releases",
        "coding_private_v2_release_events",
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
        "coding_private_v2_release_events",
        "coding_private_v2_releases",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    op.drop_index(
        "coding_private_v2_release_events_release_created_idx",
        table_name="coding_private_v2_release_events",
    )
    op.drop_table("coding_private_v2_release_events")
    op.drop_index(
        "coding_private_v2_releases_created_idx",
        table_name="coding_private_v2_releases",
    )
    op.drop_table("coding_private_v2_releases")
