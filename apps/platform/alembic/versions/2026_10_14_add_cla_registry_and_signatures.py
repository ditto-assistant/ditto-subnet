"""Add CLA version registry, append-only CLA signatures, and gate settings history.

Revision ID: c841f90e31b2
Revises: a4963e2b18c7
Create Date: 2026-10-14
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "c841f90e31b2"
down_revision: str | None = "a4963e2b18c7"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "cla_versions",
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("sha256", sa.String(length=64), nullable=False, unique=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "cla_signatures",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("hotkey", sa.String(length=64), nullable=False, index=True),
        sa.Column("coldkey", sa.String(length=64), nullable=False, index=True),
        sa.Column(
            "cla_version",
            sa.Integer(),
            sa.ForeignKey("cla_versions.version", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("cla_sha256", sa.String(length=64), nullable=False),
        sa.Column("signature", sa.String(length=128), nullable=False),
        sa.Column("nonce", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("signed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=True),
    )

    op.create_table(
        "cla_gate_history",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("changed_by", sa.String(length=64), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("mode IN ('off', 'shadow', 'enforce')", name="cla_gate_mode_check"),
    )

    # Enforce strict append-only semantics on cla_signatures at the DB level
    op.execute(
        """
        CREATE OR REPLACE FUNCTION forbid_cla_signatures_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'cla_signatures is append-only; UPDATE and DELETE are forbidden';
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_forbid_cla_signatures_mutation
        BEFORE UPDATE OR DELETE ON cla_signatures
        FOR EACH ROW EXECUTE FUNCTION forbid_cla_signatures_mutation();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_forbid_cla_signatures_mutation ON cla_signatures;")
    op.execute("DROP FUNCTION IF EXISTS forbid_cla_signatures_mutation();")
    op.drop_table("cla_gate_history")
    op.drop_table("cla_signatures")
    op.drop_table("cla_versions")
