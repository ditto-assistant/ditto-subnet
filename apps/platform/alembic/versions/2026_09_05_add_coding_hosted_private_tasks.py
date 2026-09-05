"""add hosted private task selection and irreversible grant phases

Revision ID: d9b5ea2f3c71
Revises: c8a4d91e2b60
Create Date: 2026-09-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d9b5ea2f3c71"
down_revision: str | Sequence[str] | None = "c8a4d91e2b60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "coding_hosted_private_tasks",
        sa.Column("evaluation_id", sa.UUID(), nullable=False),
        sa.Column("selection_sha256", sa.Text(), nullable=False),
        sa.Column("selection_authority", postgresql.JSONB(), nullable=False),
        sa.Column("catalog_index", sa.Integer(), nullable=False),
        sa.Column("max_patch_bytes", sa.Integer(), nullable=False),
        sa.Column("authoring_grant_id", sa.UUID(), nullable=False),
        sa.Column("grading_grant_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.Column("frozen_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("frozen_patch_sha256", sa.Text(), nullable=True),
        sa.Column("frozen_patch_size", sa.Integer(), nullable=True),
        sa.Column("closed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("close_reason", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("evaluation_id"),
        sa.UniqueConstraint("authoring_grant_id"),
        sa.UniqueConstraint("grading_grant_id"),
        sa.ForeignKeyConstraint(
            ["evaluation_id"],
            ["coding_hosted_assignments.evaluation_id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "selection_sha256 ~ '^[0-9a-f]{64}$' AND catalog_index BETWEEN 0 AND 249 "
            "AND max_patch_bytes BETWEEN 1 AND 134217728 "
            "AND authoring_grant_id <> grading_grant_id "
            "AND authoring_grant_id <> '00000000-0000-0000-0000-000000000000'::uuid "
            "AND grading_grant_id <> '00000000-0000-0000-0000-000000000000'::uuid "
            "AND jsonb_typeof(selection_authority) = 'object' "
            "AND octet_length(selection_authority::text) <= 4096",
            name="coding_hosted_private_tasks_authority_check",
        ),
        sa.CheckConstraint(
            "(frozen_at IS NULL) = (frozen_patch_sha256 IS NULL) "
            "AND (frozen_at IS NULL) = (frozen_patch_size IS NULL) "
            "AND (frozen_at IS NULL OR (frozen_at >= created_at "
            "AND frozen_patch_sha256 ~ '^[0-9a-f]{64}$' "
            "AND frozen_patch_size BETWEEN 0 AND max_patch_bytes)) "
            "AND (closed_at IS NULL) = (close_reason IS NULL) "
            "AND (closed_at IS NULL OR (closed_at >= created_at "
            "AND (frozen_at IS NULL OR closed_at >= frozen_at) "
            "AND close_reason IN ('completed','failed','aborted')))",
            name="coding_hosted_private_tasks_phase_check",
        ),
    )
    op.execute("""
        CREATE FUNCTION coding_hosted_private_task_guard() RETURNS trigger AS $$
        DECLARE
            assignment coding_hosted_assignments%ROWTYPE;
            phase_columns text[] := ARRAY['frozen_at','frozen_patch_sha256',
                'frozen_patch_size','closed_at','close_reason'];
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'hosted private tasks cannot be deleted'
                    USING ERRCODE = '23514';
            END IF;
            SELECT * INTO assignment FROM coding_hosted_assignments
                WHERE evaluation_id = NEW.evaluation_id;
            IF NOT FOUND OR assignment.authority->>'selection_sha256'
                IS DISTINCT FROM NEW.selection_sha256 THEN
                RAISE EXCEPTION 'hosted private task assignment mismatch'
                    USING ERRCODE = '23514';
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF assignment.started_at IS NOT NULL
                   OR assignment.expires_at <= clock_timestamp()
                   OR NEW.created_at < assignment.created_at
                   OR NEW.created_at > clock_timestamp()
                   OR NEW.frozen_at IS NOT NULL OR NEW.frozen_patch_sha256 IS NOT NULL
                   OR NEW.frozen_patch_size IS NOT NULL OR NEW.closed_at IS NOT NULL
                   OR NEW.close_reason IS NOT NULL THEN
                    RAISE EXCEPTION 'hosted private task must bind before start'
                        USING ERRCODE = '23514';
                END IF;
            ELSE
                IF (to_jsonb(OLD) - phase_columns)
                    IS DISTINCT FROM
                   (to_jsonb(NEW) - phase_columns)
                   OR (OLD.frozen_at IS NOT NULL AND
                       ROW(NEW.frozen_at, NEW.frozen_patch_sha256,
                           NEW.frozen_patch_size) IS DISTINCT FROM
                       ROW(OLD.frozen_at, OLD.frozen_patch_sha256,
                           OLD.frozen_patch_size))
                   OR (OLD.closed_at IS NOT NULL AND NEW IS DISTINCT FROM OLD)
                THEN
                    RAISE EXCEPTION 'hosted private task authority is immutable'
                        USING ERRCODE = '23514';
                END IF;
                IF OLD.frozen_at IS NULL AND NEW.frozen_at IS NOT NULL AND
                    (assignment.started_at IS NULL
                     OR NEW.frozen_at < assignment.started_at
                     OR NEW.frozen_at >= assignment.expires_at
                     OR NEW.frozen_at > clock_timestamp()) THEN
                    RAISE EXCEPTION 'hosted freeze requires an active attempt'
                        USING ERRCODE = '23514';
                END IF;
                IF NEW.closed_at IS NOT NULL AND NEW.closed_at > clock_timestamp() THEN
                    RAISE EXCEPTION 'hosted close time is invalid'
                        USING ERRCODE = '23514';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER coding_hosted_private_task_guard
        BEFORE INSERT OR UPDATE OR DELETE ON coding_hosted_private_tasks
        FOR EACH ROW EXECUTE FUNCTION coding_hosted_private_task_guard();
    """)


def downgrade() -> None:
    op.drop_table("coding_hosted_private_tasks")
    op.execute("DROP FUNCTION coding_hosted_private_task_guard()")
