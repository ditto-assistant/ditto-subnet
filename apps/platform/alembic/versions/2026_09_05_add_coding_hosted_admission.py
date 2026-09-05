"""add irreversible hosted Coding assignment admission

Revision ID: c8a4d91e2b60
Revises: b7f3c8a12d49
Create Date: 2026-09-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c8a4d91e2b60"
down_revision: str | Sequence[str] | None = "b7f3c8a12d49"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "coding_hosted_assignments",
        sa.Column("evaluation_id", sa.UUID(), nullable=False),
        sa.Column("attempt_id", sa.UUID(), nullable=False),
        sa.Column("release_row_id", sa.UUID(), nullable=False),
        sa.Column("registration_sha256", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("validator_hotkey", sa.Text(), nullable=False),
        sa.Column("artifact_sha256", sa.Text(), nullable=False),
        sa.Column("screened_image_sha256", sa.Text(), nullable=False),
        sa.Column("assignment_sha256", sa.Text(), nullable=False),
        sa.Column("authority", postgresql.JSONB(), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("shadow_only", sa.Boolean(), nullable=False),
        sa.Column("weight_eligible", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.Column("admitted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("admission_request_sha256", sa.Text(), nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("worker_id", sa.UUID(), nullable=True),
        sa.PrimaryKeyConstraint("evaluation_id"),
        sa.UniqueConstraint("attempt_id"),
        sa.UniqueConstraint("assignment_sha256"),
        sa.ForeignKeyConstraint(
            ["release_row_id", "registration_sha256"],
            [
                "coding_private_v2_releases.release_row_id",
                "coding_private_v2_releases.registration_sha256",
            ],
            name="coding_hosted_assignments_release_fkey",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.agent_id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "shadow_only = true AND weight_eligible = false",
            name="coding_hosted_assignments_shadow_check",
        ),
        sa.CheckConstraint(
            "registration_sha256 ~ '^[0-9a-f]{64}$' "
            "AND artifact_sha256 ~ '^[0-9a-f]{64}$' "
            "AND screened_image_sha256 ~ '^[0-9a-f]{64}$' "
            "AND assignment_sha256 ~ '^[0-9a-f]{64}$'",
            name="coding_hosted_assignments_digests_check",
        ),
        sa.CheckConstraint(
            "validator_hotkey ~ '^[1-9A-HJ-NP-Za-km-z]{47,48}$' "
            "AND length(trim(reason)) BETWEEN 8 AND 512 "
            "AND length(trim(actor)) BETWEEN 1 AND 120 "
            "AND jsonb_typeof(authority) = 'object' "
            "AND octet_length(authority::text) <= 16384",
            name="coding_hosted_assignments_authority_check",
        ),
        sa.CheckConstraint(
            "expires_at > created_at "
            "AND (admitted_at IS NULL) = (admission_request_sha256 IS NULL) "
            "AND (admitted_at IS NULL OR (admitted_at >= created_at "
            "AND admission_request_sha256 ~ '^[0-9a-f]{64}$')) "
            "AND (started_at IS NULL) = (worker_id IS NULL) "
            "AND (started_at IS NULL OR (admitted_at IS NOT NULL "
            "AND started_at >= admitted_at))",
            name="coding_hosted_assignments_lifecycle_check",
        ),
    )
    op.execute("""
        CREATE FUNCTION coding_hosted_assignment_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'hosted assignments cannot be deleted'
                    USING ERRCODE = '23514';
            END IF;
            IF (to_jsonb(OLD) - ARRAY['admitted_at','admission_request_sha256',
                                     'started_at','worker_id'])
                IS DISTINCT FROM
               (to_jsonb(NEW) - ARRAY['admitted_at','admission_request_sha256',
                                     'started_at','worker_id'])
                OR (OLD.admitted_at IS NOT NULL AND
                    (NEW.admitted_at IS DISTINCT FROM OLD.admitted_at OR
                     NEW.admission_request_sha256 IS DISTINCT FROM
                     OLD.admission_request_sha256))
                OR (OLD.started_at IS NOT NULL AND
                    (NEW.started_at IS DISTINCT FROM OLD.started_at OR
                     NEW.worker_id IS DISTINCT FROM OLD.worker_id))
            THEN
                RAISE EXCEPTION 'hosted assignment authority or lifecycle is immutable'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER coding_hosted_assignment_guard
        BEFORE UPDATE OR DELETE ON coding_hosted_assignments
        FOR EACH ROW EXECUTE FUNCTION coding_hosted_assignment_guard();
    """)


def downgrade() -> None:
    op.drop_table("coding_hosted_assignments")
    op.execute("DROP FUNCTION coding_hosted_assignment_guard()")
