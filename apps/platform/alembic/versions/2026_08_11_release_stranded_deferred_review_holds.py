"""Release ATH holds stranded by a reopened copy review.

Revision ID: b7c4e1a90d52
Revises: d19a7e4b2c11
Create Date: 2026-08-11

When a resolved *copy* review was reopened as a deferred source review, the
reopen cleared ``agents.duplicate_of`` but left ``ath_reviews`` still pointing
at the matched agent. ``resolve_copy_review`` refuses to resolve while those two
disagree, so every clear returned 409 "agent hold evidence no longer matches
review" and the agent could never leave ``ath_pending_review`` -- the emission
ledger excludes it the whole time and no operator action could release it.

The reopen path now clears the pointer alongside the agent's, but rows already
written carry the desync. This repair fixes exactly those: pending reviews whose
current lifecycle is a deferred source review, still holding a matched pointer,
whose agent has none. The discarded pointer is preserved under
``original_evidence.prior_review.original_duplicate_of`` so the copy history
stays auditable, matching what the corrected reopen path now records.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b7c4e1a90d52"
down_revision: str | None = "d19a7e4b2c11"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STRANDED = """
    SELECT r.review_id
    FROM ath_reviews r
    JOIN agents a ON a.agent_id = r.agent_id
    WHERE r.status = 'pending'
      AND r.original_duplicate_of IS NOT NULL
      AND a.duplicate_of IS NULL
      AND r.algorithm_provenance ->> 'review_kind' = 'deferred_source_review'
"""

# Preserve the pointer in the audit snapshot before dropping it, creating the
# prior_review object when the reopen predates it being recorded.
_PRESERVE = """
    UPDATE ath_reviews r
    SET original_evidence = jsonb_set(
            jsonb_set(
                COALESCE(r.original_evidence, '{}'::jsonb),
                '{prior_review}',
                COALESCE(r.original_evidence -> 'prior_review', '{}'::jsonb),
                true
            ),
            '{prior_review,original_duplicate_of}',
            to_jsonb(r.original_duplicate_of::text),
            true
        )
    WHERE r.review_id IN (SELECT review_id FROM stranded)
"""

_CLEAR = """
    UPDATE ath_reviews
    SET original_duplicate_of = NULL
    WHERE review_id IN (SELECT review_id FROM stranded)
"""

# Reinstates the desync this repair fixed, from the preserved snapshot.
_RESTORE = """
    UPDATE ath_reviews r
    SET original_duplicate_of = (
            r.original_evidence -> 'prior_review'
                ->> 'original_duplicate_of'
        )::uuid
    FROM agents a
    WHERE a.agent_id = r.agent_id
      AND r.status = 'pending'
      AND r.original_duplicate_of IS NULL
      AND a.duplicate_of IS NULL
      AND r.algorithm_provenance ->> 'review_kind' = 'deferred_source_review'
      AND r.original_evidence -> 'prior_review'
            ->> 'original_duplicate_of' IS NOT NULL
"""


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # jsonb_set is Postgres-only. No non-Postgres deployment carries these
        # rows, and the corrected reopen path prevents new ones.
        return
    op.execute(f"WITH stranded AS ({_STRANDED}) {_PRESERVE}")
    op.execute(f"WITH stranded AS ({_STRANDED}) {_CLEAR}")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(_RESTORE)
