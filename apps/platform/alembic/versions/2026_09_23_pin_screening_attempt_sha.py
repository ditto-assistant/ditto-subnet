"""Pin the artifact SHA of every new screening attempt.

Revision ID: 7f5e3a91bc42
Revises: 7b62d9c083f1
Create Date: 2026-09-23

Existing rows remain NULL and cannot prove unchanged-artifact retry evidence.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "7f5e3a91bc42"
down_revision: str | Sequence[str] | None = "7b62d9c083f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "screening_attempts", sa.Column("artifact_sha256", sa.Text(), nullable=True)
    )
    op.create_check_constraint(
        "screening_attempts_artifact_sha_check",
        "screening_attempts",
        "artifact_sha256 IS NULL OR length(artifact_sha256) = 64",
    )
    op.execute(
        """
        CREATE FUNCTION reject_screening_attempt_artifact_change()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.artifact_sha256 IS DISTINCT FROM OLD.artifact_sha256 THEN
                RAISE EXCEPTION 'screening attempt artifact SHA is immutable';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER screening_attempt_artifact_immutable
        BEFORE UPDATE OF artifact_sha256 ON screening_attempts
        FOR EACH ROW EXECUTE FUNCTION reject_screening_attempt_artifact_change()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER screening_attempt_artifact_immutable ON screening_attempts"
    )
    op.execute("DROP FUNCTION reject_screening_attempt_artifact_change()")
    op.drop_constraint(
        "screening_attempts_artifact_sha_check", "screening_attempts", type_="check"
    )
    op.drop_column("screening_attempts", "artifact_sha256")
