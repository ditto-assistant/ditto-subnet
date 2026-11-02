"""Record append-only V13 clean approvals and pre-randomness generation starts.

Revision ID: 4ac1e3d7098b
Revises: 9e2c4f7a6b10

This migration inserts no approval, generation, seed, package, or verdict.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "4ac1e3d7098b"
down_revision: str | Sequence[str] | None = "9e2c4f7a6b10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "v13_known_benign_control_approvals",
        sa.Column("approval_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("artifact_sha256", sa.Text(), nullable=False),
        sa.Column("image_sha256", sa.Text(), nullable=False),
        sa.Column("profile_sha256", sa.Text(), nullable=False),
        sa.Column("review_evidence_sha256", sa.Text(), nullable=False),
        sa.Column("approval_receipt_sha256", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("approved_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.agent_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["attempt_id"], ["screening_attempts.attempt_id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("attempt_id", "profile_sha256"),
        *(
            sa.CheckConstraint(f"length({name}) = 64", name=f"v13kb_{name}_check")
            for name in (
                "artifact_sha256",
                "image_sha256",
                "profile_sha256",
                "review_evidence_sha256",
                "approval_receipt_sha256",
            )
        ),
        sa.CheckConstraint("length(actor) BETWEEN 1 AND 120", name="v13kb_actor"),
        sa.CheckConstraint("length(reason) >= 8", name="v13kb_reason"),
    )
    op.create_table(
        "v13_private_generation_groups",
        sa.Column("group_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("target_agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_attempt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_artifact_sha256", sa.Text(), nullable=False),
        sa.Column("target_image_sha256", sa.Text(), nullable=False),
        sa.Column("control_agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("control_attempt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("control_artifact_sha256", sa.Text(), nullable=False),
        sa.Column("control_image_sha256", sa.Text(), nullable=False),
        sa.Column("approval_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("approval_receipt_sha256", sa.Text(), nullable=False),
        sa.Column("profile_sha256", sa.Text(), nullable=False),
        sa.Column("target_receipt_sha256", sa.Text(), nullable=False),
        sa.Column("control_receipt_sha256", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["target_agent_id"], ["agents.agent_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["target_attempt_id"],
            ["screening_attempts.attempt_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["control_agent_id"], ["agents.agent_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["control_attempt_id"],
            ["screening_attempts.attempt_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["approval_id"],
            ["v13_known_benign_control_approvals.approval_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("target_attempt_id"),
        sa.CheckConstraint(
            "target_agent_id <> control_agent_id", name="v13pg_distinct"
        ),
        *(
            sa.CheckConstraint(f"length({name}) = 64", name=f"v13pg_{name}_check")
            for name in (
                "target_artifact_sha256",
                "target_image_sha256",
                "control_artifact_sha256",
                "control_image_sha256",
                "approval_receipt_sha256",
                "profile_sha256",
                "target_receipt_sha256",
                "control_receipt_sha256",
            )
        ),
        sa.CheckConstraint("length(actor) BETWEEN 1 AND 120", name="v13pg_actor"),
    )
    op.execute(
        """
        CREATE FUNCTION reject_v13_private_generation_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'V13 private generation records are append-only';
        END;
        $$
        """
    )
    for table in (
        "v13_known_benign_control_approvals",
        "v13_private_generation_groups",
    ):
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_v13_private_generation_mutation()"
        )


def downgrade() -> None:
    for table in (
        "v13_private_generation_groups",
        "v13_known_benign_control_approvals",
    ):
        op.execute(f"DROP TRIGGER {table}_immutable ON {table}")
        op.drop_table(table)
    op.execute("DROP FUNCTION reject_v13_private_generation_mutation()")
