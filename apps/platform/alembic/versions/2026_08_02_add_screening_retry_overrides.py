"""add audited screening retry overrides

Revision ID: f7b8c9d0e1a2
Revises: d71e3f901a2b
Create Date: 2026-08-02 12:24:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "f7b8c9d0e1a2"
down_revision: str | Sequence[str] | None = "d71e3f901a2b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE screening_retry_overrides (
            override_id UUID PRIMARY KEY,
            agent_id UUID NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
            attempt_id UUID NOT NULL
                REFERENCES screening_attempts(attempt_id) ON DELETE CASCADE,
            artifact_sha256 TEXT NOT NULL,
            expected_score_count INTEGER NOT NULL,
            reason TEXT NOT NULL,
            actor TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT screening_retry_overrides_attempt_key UNIQUE (attempt_id),
            CONSTRAINT screening_retry_overrides_sha_length_check
                CHECK (length(artifact_sha256) = 64),
            CONSTRAINT screening_retry_overrides_score_count_check
                CHECK (expected_score_count >= 0),
            CONSTRAINT screening_retry_overrides_reason_check
                CHECK (length(trim(reason)) BETWEEN 8 AND 500),
            CONSTRAINT screening_retry_overrides_actor_check
                CHECK (length(trim(actor)) BETWEEN 1 AND 120)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX screening_retry_overrides_agent_created_idx
        ON screening_retry_overrides (agent_id, created_at, override_id)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS screening_retry_overrides")
