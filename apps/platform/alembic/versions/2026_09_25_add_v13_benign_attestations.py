"""Append-only authenticated human attestations for V13 benign controls.

Revision ID: c8e4b1a97d20
Revises: a40f7d9c621e
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c8e4b1a97d20"
down_revision: str | Sequence[str] | None = "a40f7d9c621e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "v13_known_benign_attestations",
        sa.Column("attestation_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("approval_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("principal_sub", sa.Text(), nullable=False),
        sa.Column("principal_email", sa.Text(), nullable=False),
        sa.Column("review_evidence_sha256", sa.Text(), nullable=False),
        sa.Column("assertion_sha256", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("attested_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["approval_id"],
            ["v13_known_benign_control_approvals.approval_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "approval_id", "principal_sub", name="v13ba_approval_sub_uq"
        ),
        sa.UniqueConstraint(
            "approval_id", "principal_email", name="v13ba_approval_email_uq"
        ),
        sa.CheckConstraint("length(principal_sub) BETWEEN 1 AND 120", name="v13ba_sub"),
        sa.CheckConstraint(
            "length(principal_email) BETWEEN 3 AND 254", name="v13ba_email"
        ),
        sa.CheckConstraint(
            "length(review_evidence_sha256) = 64", name="v13ba_evidence"
        ),
        sa.CheckConstraint("length(assertion_sha256) = 64", name="v13ba_assertion"),
        sa.CheckConstraint("length(reason) >= 8", name="v13ba_reason"),
    )
    op.execute(
        "CREATE TRIGGER v13_known_benign_attestations_immutable "
        "BEFORE UPDATE OR DELETE ON v13_known_benign_attestations "
        "FOR EACH ROW EXECUTE FUNCTION reject_v13_private_generation_mutation()"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER v13_known_benign_attestations_immutable "
        "ON v13_known_benign_attestations"
    )
    op.drop_table("v13_known_benign_attestations")
