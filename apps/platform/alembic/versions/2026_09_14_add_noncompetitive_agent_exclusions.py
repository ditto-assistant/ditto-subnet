"""add audited noncompetitive team canary exclusions

Revision ID: 9e4b27c1d5a3
Revises: e6f4a9c2d781
Create Date: 2026-09-14 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "9e4b27c1d5a3"
down_revision: str | Sequence[str] | None = "e6f4a9c2d781"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Empty by default: no agent is excluded until an operator reserves one.
    op.execute(
        """
        CREATE TABLE noncompetitive_agent_exclusions (
            exclusion_id UUID PRIMARY KEY,
            kind TEXT NOT NULL,
            miner_hotkey TEXT NOT NULL,
            artifact_sha256 TEXT NOT NULL,
            reason TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            agent_id UUID,
            screened_image_sha256 TEXT,
            bound_by TEXT,
            bound_reason TEXT,
            bound_at TIMESTAMPTZ,
            CONSTRAINT noncompetitive_agent_exclusions_agent_id_fkey
                FOREIGN KEY (agent_id) REFERENCES agents (agent_id)
                ON DELETE RESTRICT,
            CONSTRAINT noncompetitive_agent_exclusions_identity_key
                UNIQUE (miner_hotkey, artifact_sha256),
            CONSTRAINT noncompetitive_agent_exclusions_agent_key UNIQUE (agent_id),
            CONSTRAINT noncompetitive_agent_exclusions_kind
                CHECK (kind = 'team_canary'),
            CONSTRAINT noncompetitive_agent_exclusions_artifact
                CHECK (artifact_sha256 ~ '^[0-9a-f]{64}$'),
            CONSTRAINT noncompetitive_agent_exclusions_reason
                CHECK (length(trim(reason)) >= 8),
            CONSTRAINT noncompetitive_agent_exclusions_created_by
                CHECK (length(trim(created_by)) BETWEEN 1 AND 120),
            CONSTRAINT noncompetitive_agent_exclusions_binding CHECK (
                (agent_id IS NULL AND screened_image_sha256 IS NULL
                    AND bound_by IS NULL AND bound_reason IS NULL
                    AND bound_at IS NULL)
                OR (agent_id IS NOT NULL
                    AND screened_image_sha256 ~ '^[0-9a-f]{64}$'
                    AND length(trim(bound_by)) BETWEEN 1 AND 120
                    AND length(trim(bound_reason)) >= 8
                    AND bound_at IS NOT NULL)
            )
        )
        """
    )
    op.execute(
        "CREATE INDEX noncompetitive_agent_exclusions_hotkey_idx "
        "ON noncompetitive_agent_exclusions (miner_hotkey)"
    )
    # Append-only: a reservation is bound at most once and never deleted, so an
    # exclusion can never be quietly lifted to let a team canary compete.
    op.execute(
        """
        CREATE FUNCTION noncompetitive_agent_exclusions_append_only()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'noncompetitive agent exclusions are append-only';
            END IF;
            IF OLD.agent_id IS NOT NULL
                OR NEW.exclusion_id IS DISTINCT FROM OLD.exclusion_id
                OR NEW.kind IS DISTINCT FROM OLD.kind
                OR NEW.miner_hotkey IS DISTINCT FROM OLD.miner_hotkey
                OR NEW.artifact_sha256 IS DISTINCT FROM OLD.artifact_sha256
                OR NEW.reason IS DISTINCT FROM OLD.reason
                OR NEW.created_by IS DISTINCT FROM OLD.created_by
                OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'noncompetitive agent exclusions bind once';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER noncompetitive_agent_exclusions_append_only "
        "BEFORE UPDATE OR DELETE ON noncompetitive_agent_exclusions "
        "FOR EACH ROW EXECUTE FUNCTION noncompetitive_agent_exclusions_append_only()"
    )


def downgrade() -> None:
    op.execute("DROP TABLE noncompetitive_agent_exclusions")
    op.execute("DROP FUNCTION noncompetitive_agent_exclusions_append_only()")
