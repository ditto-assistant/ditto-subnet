"""Separate official verification controls from economic miner records.

Revision ID: 73bd92c14f60
Revises: 8d91f2b4c6a0
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "73bd92c14f60"
down_revision: str | Sequence[str] | None = "8d91f2b4c6a0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "v13_official_control_images",
        sa.Column("control_id", sa.UUID(), primary_key=True),
        sa.Column("request_id", sa.UUID(), nullable=False),
        sa.Column("fixture_id", sa.UUID(), nullable=False),
        sa.Column("artifact_sha256", sa.Text(), nullable=False),
        sa.Column("image_sha256", sa.Text(), nullable=False),
        sa.Column("image_id", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("producer", sa.Text(), nullable=False),
        sa.Column("staging_id", sa.UUID(), nullable=False),
        sa.Column("verified_storage_key", sa.Text()),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["fixture_id"],
            ["screener_l2_report_canaries.canary_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("request_id"),
        sa.CheckConstraint(
            "artifact_sha256 ~ '^[0-9a-f]{64}$'", name="v13oci_artifact"
        ),
        sa.CheckConstraint("image_sha256 ~ '^[0-9a-f]{64}$'", name="v13oci_image"),
        sa.CheckConstraint(
            "image_id ~ '^sha256:[0-9a-f]{64}$'", name="v13oci_image_id"
        ),
        sa.CheckConstraint(
            "size_bytes > 0 AND size_bytes <= 8589934592", name="v13oci_size"
        ),
        sa.CheckConstraint(
            "length(producer) BETWEEN 1 AND 320", name="v13oci_producer"
        ),
        sa.CheckConstraint(
            "(verified_at IS NULL) = (verified_storage_key IS NULL)",
            name="v13oci_verified_pair",
        ),
    )
    op.create_table(
        "v13_official_control_approvals",
        sa.Column("approval_id", sa.UUID(), primary_key=True),
        sa.Column("control_id", sa.UUID(), nullable=False),
        sa.Column("profile_sha256", sa.Text(), nullable=False),
        sa.Column("review_evidence_sha256", sa.Text(), nullable=False),
        sa.Column("approval_receipt_sha256", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["control_id"],
            ["v13_official_control_images.control_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("control_id", "profile_sha256"),
        *(
            sa.CheckConstraint(f"{name} ~ '^[0-9a-f]{{64}}$'", name=f"v13oca_{name}")
            for name in (
                "profile_sha256",
                "review_evidence_sha256",
                "approval_receipt_sha256",
            )
        ),
        sa.CheckConstraint("length(actor) BETWEEN 1 AND 320", name="v13oca_actor"),
        sa.CheckConstraint("length(reason) >= 8", name="v13oca_reason"),
    )
    op.execute("""
        CREATE FUNCTION protect_v13_official_control_image() RETURNS trigger AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'official control images are retained audit evidence';
          END IF;
          IF OLD.verified_at IS NOT NULL OR
             (to_jsonb(NEW) - 'verified_at' - 'verified_storage_key') IS DISTINCT FROM
             (to_jsonb(OLD) - 'verified_at' - 'verified_storage_key') THEN
            RAISE EXCEPTION 'official control image identity is immutable';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER v13_official_control_image_immutable
        BEFORE UPDATE OR DELETE ON v13_official_control_images
        FOR EACH ROW EXECUTE FUNCTION protect_v13_official_control_image();
    """)
    op.execute("""
        CREATE FUNCTION protect_v13_official_control_approval() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'official control approvals are immutable audit evidence';
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER v13_official_control_approval_immutable
        BEFORE UPDATE OR DELETE ON v13_official_control_approvals
        FOR EACH ROW EXECUTE FUNCTION protect_v13_official_control_approval();
    """)


def downgrade() -> None:
    # Never discard a real approval or uploaded control to make rollback succeed.
    for table in ("v13_official_control_approvals", "v13_official_control_images"):
        if op.get_bind().execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError(
                "preserve official control audit evidence before rollback"
            )
    op.drop_table("v13_official_control_approvals")
    op.drop_table("v13_official_control_images")
    op.execute("DROP FUNCTION protect_v13_official_control_approval()")
    op.execute("DROP FUNCTION protect_v13_official_control_image()")
