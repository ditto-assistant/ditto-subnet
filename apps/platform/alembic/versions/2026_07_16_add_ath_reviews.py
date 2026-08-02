"""add durable ATH review audit records

Revision ID: d84b3a91f620
Revises: c53fa6d2b194
Create Date: 2026-07-16 13:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d84b3a91f620"
down_revision: str | Sequence[str] | None = "c53fa6d2b194"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE ath_reviews (
            review_id UUID PRIMARY KEY,
            agent_id UUID NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CONSTRAINT ath_reviews_status_check
                CHECK (status IN ('pending','resolved')),
            opened_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            resolved_at TIMESTAMPTZ,
            resolved_by TEXT,
            resolution TEXT CONSTRAINT ath_reviews_resolution_check CHECK (
                resolution IS NULL OR resolution IN ('clear','reject')
            ),
            resolution_reason TEXT,
            original_duplicate_of UUID,
            original_reason TEXT,
            original_policy_version INTEGER NOT NULL,
            original_evidence JSONB NOT NULL,
            algorithm_provenance JSONB NOT NULL,
            CONSTRAINT ath_reviews_agent_id_key UNIQUE (agent_id),
            CONSTRAINT ath_reviews_agent_id_fkey FOREIGN KEY (agent_id)
                REFERENCES agents(agent_id) ON DELETE RESTRICT,
            CONSTRAINT ath_reviews_original_duplicate_of_fkey
                FOREIGN KEY (original_duplicate_of)
                REFERENCES agents(agent_id) ON DELETE RESTRICT,
            CONSTRAINT ath_reviews_lifecycle_check CHECK (
                (status = 'pending' AND resolved_at IS NULL
                    AND resolved_by IS NULL AND resolution IS NULL
                    AND resolution_reason IS NULL)
                OR
                (status = 'resolved' AND resolved_at IS NOT NULL
                    AND resolved_by IS NOT NULL
                    AND length(trim(resolved_by)) BETWEEN 1 AND 120
                    AND resolution IS NOT NULL
                    AND resolution IN ('clear','reject')
                    AND resolution_reason IS NOT NULL
                    AND length(trim(resolution_reason)) BETWEEN 3 AND 500)
            )
        )
        """
    )
    op.execute(
        "CREATE INDEX ath_reviews_status_opened_idx "
        "ON ath_reviews(status, opened_at, review_id)"
    )
    # Evidence-only backfill. Agent status/verdict columns are never updated.
    op.execute(
        """
        INSERT INTO ath_reviews (
            review_id, agent_id, status, opened_at, original_duplicate_of,
            original_reason, original_policy_version, original_evidence,
            algorithm_provenance
        )
        SELECT
            agent_id, agent_id, 'pending', COALESCE(
                (
                    SELECT max(a.recorded_at)
                    FROM score_audit_log a
                    WHERE a.agent_id = agents.agent_id
                      AND a.event = 'agent_finalized'
                ),
                (
                    SELECT max(s.generated_at)
                    FROM scores s
                    WHERE s.agent_id = agents.agent_id
                ),
                created_at
            ), duplicate_of,
            review_reason, screening_policy_version,
            jsonb_build_object(
                'sha256', sha256,
                'size_bytes', size_bytes,
                'content_fingerprint_version', NULL,
                'structural_fingerprint_version', NULL,
                'prompt_fingerprint_version', NULL
            ),
            jsonb_build_object(
                'snapshot', 'legacy-agent-hold',
                'reference_provenance', 'pre-durable-review-unknown',
                'backfilled', true,
                'snapshot_order', 'before-fingerprint-metadata-backfill',
                'opened_at_source', CASE
                    WHEN EXISTS (
                        SELECT 1 FROM score_audit_log a
                        WHERE a.agent_id = agents.agent_id
                          AND a.event = 'agent_finalized'
                    ) THEN 'agent_finalized_audit'
                    WHEN EXISTS (
                        SELECT 1 FROM scores s WHERE s.agent_id = agents.agent_id
                    ) THEN 'latest_score'
                    ELSE 'agent_created_at_fallback'
                END
            )
        FROM agents
        WHERE status = 'ath_pending_review'
        ON CONFLICT (agent_id) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ath_reviews")
