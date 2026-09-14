"""append-only operator cancellation of an unstarted hosted Coding assignment

Revision ID: 4b7f09043317
Revises: e6f4a9c2d781
Create Date: 2026-09-14

A cancellation is a new immutable row; no assignment, task or evidence row is
deleted or rewritten. Only an assignment whose attempt never started, and whose
bound private task (if any) is already closed, can be cancelled. Once the row
exists PostgreSQL refuses every later change to the assignment (so it can never
be admitted or started) and refuses binding a private task to it. Running
attempts are stopped by the worker's own abort path, never by this ledger.

Every branch serialises on the assignment row lock. Each later statement in the
trigger takes a fresh READ COMMITTED snapshot, so it sees whatever the previous
lock holder committed: a cancellation sees a task bound just before it, and a
task insert waiting behind a cancellation sees that cancellation.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "4b7f09043317"
down_revision: str | Sequence[str] | None = "e6f4a9c2d781"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "coding_hosted_assignment_cancellations",
        sa.Column("evaluation_id", sa.UUID(), nullable=False),
        sa.Column("assignment_sha256", sa.Text(), nullable=False),
        sa.Column("prior_state", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column(
            "cancelled_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.PrimaryKeyConstraint("evaluation_id"),
        sa.ForeignKeyConstraint(
            ["evaluation_id"],
            ["coding_hosted_assignments.evaluation_id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "assignment_sha256 ~ '^[0-9a-f]{64}$' "
            "AND prior_state IN ('pending_admission','admitted') "
            "AND length(trim(reason)) BETWEEN 8 AND 512 "
            "AND length(trim(actor)) BETWEEN 1 AND 120",
            name="coding_hosted_assignment_cancellations_audit_check",
        ),
    )
    op.execute("""
        CREATE FUNCTION guard_hosted_assignment_cancellation() RETURNS trigger AS $$
        DECLARE a coding_hosted_assignments%ROWTYPE;
        BEGIN
            IF TG_TABLE_NAME = 'coding_hosted_assignment_cancellations' THEN
                -- Row lock serialises with admission, start, binding and close.
                SELECT * INTO a FROM coding_hosted_assignments
                    WHERE evaluation_id = NEW.evaluation_id FOR UPDATE;
                IF NOT FOUND
                   OR a.assignment_sha256 IS DISTINCT FROM NEW.assignment_sha256
                   OR a.started_at IS NOT NULL
                   OR a.worker_id IS NOT NULL
                   OR NEW.prior_state IS DISTINCT FROM (CASE
                        WHEN a.admitted_at IS NULL THEN 'pending_admission'
                        ELSE 'admitted' END)
                   OR NEW.cancelled_at < a.created_at
                   OR NEW.cancelled_at > clock_timestamp()
                THEN
                    RAISE EXCEPTION
                        'hosted cancellation requires an unstarted assignment'
                        USING ERRCODE = '23514';
                END IF;
                -- Object access is removed before the ledger row is appended.
                IF EXISTS (
                    SELECT 1 FROM coding_hosted_private_tasks t
                    WHERE t.evaluation_id = NEW.evaluation_id
                      AND t.closed_at IS NULL
                ) THEN
                    RAISE EXCEPTION
                        'hosted cancellation requires a closed private task'
                        USING ERRCODE = '23514';
                END IF;
            ELSIF TG_TABLE_NAME = 'coding_hosted_private_tasks' THEN
                -- FOR SHARE waits for a cancellation holding FOR UPDATE and
                -- makes a later cancellation wait for this insert to commit.
                PERFORM 1 FROM coding_hosted_assignments
                    WHERE evaluation_id = NEW.evaluation_id FOR SHARE;
                IF EXISTS (
                    SELECT 1 FROM coding_hosted_assignment_cancellations c
                    WHERE c.evaluation_id = NEW.evaluation_id
                ) THEN
                    RAISE EXCEPTION 'hosted assignment is cancelled'
                        USING ERRCODE = '23514';
                END IF;
            -- A BEFORE UPDATE row trigger fires with the row lock already held.
            ELSIF NEW IS DISTINCT FROM OLD AND EXISTS (
                SELECT 1 FROM coding_hosted_assignment_cancellations c
                WHERE c.evaluation_id = NEW.evaluation_id
            ) THEN
                RAISE EXCEPTION 'hosted assignment is cancelled'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER coding_hosted_assignment_cancellations_insert
        BEFORE INSERT ON coding_hosted_assignment_cancellations
        FOR EACH ROW EXECUTE FUNCTION guard_hosted_assignment_cancellation();
    """)
    op.execute("""
        CREATE TRIGGER coding_hosted_assignment_cancellations_immutable
        BEFORE UPDATE OR DELETE ON coding_hosted_assignment_cancellations
        FOR EACH ROW EXECUTE FUNCTION guard_coding_catalog_append_only();
    """)
    op.execute("""
        CREATE TRIGGER coding_hosted_assignments_cancellation_guard
        BEFORE UPDATE ON coding_hosted_assignments
        FOR EACH ROW EXECUTE FUNCTION guard_hosted_assignment_cancellation();
    """)
    op.execute("""
        CREATE TRIGGER coding_hosted_private_tasks_cancellation_guard
        BEFORE INSERT ON coding_hosted_private_tasks
        FOR EACH ROW EXECUTE FUNCTION guard_hosted_assignment_cancellation();
    """)


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER coding_hosted_private_tasks_cancellation_guard "
        "ON coding_hosted_private_tasks"
    )
    op.execute(
        "DROP TRIGGER coding_hosted_assignments_cancellation_guard "
        "ON coding_hosted_assignments"
    )
    op.drop_table("coding_hosted_assignment_cancellations")
    op.execute("DROP FUNCTION guard_hosted_assignment_cancellation()")
