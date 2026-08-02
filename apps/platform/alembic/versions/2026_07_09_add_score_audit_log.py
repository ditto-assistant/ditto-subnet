"""add score_audit_log (append-only, hash-chained public audit log)

Revision ID: d3a9f5e17c24
Revises: c8f2d6a01b93
Create Date: 2026-07-09 15:00:00.000000

The tamper-evident public projection of the k=3 scoring record (see
:class:`ditto.db.models.ScoreAuditEntry` and
``docs/public-telemetry.md``). Every scoring event appends one immutable row in
the same transaction as the score write:

- ``score`` — one validator's signed score (the full signed tuple + signature).
- ``agent_finalized`` — quorum reached: the median + the scoring validators.

``entry_hash`` is a SHA-256 hash chain over each entry's canonical JSON (which
embeds ``prev_hash``), so removing or editing any historical entry breaks every
later link. Rows are only ever inserted, never updated or deleted.

``seq`` is BIGSERIAL (monotonic append order). ``agent_id`` is deliberately NOT
FK-bound: the audit history must outlive a pruned agent, not cascade away. A
unique index on ``entry_hash`` catches an accidental duplicate append; a
``agent_id`` index serves the per-agent public read.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d3a9f5e17c24"
down_revision: str | Sequence[str] | None = "c8f2d6a01b93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the append-only, hash-chained ``score_audit_log`` table."""
    op.execute(
        """
        CREATE TABLE score_audit_log (
            seq BIGSERIAL PRIMARY KEY,
            agent_id UUID NOT NULL,
            validator_hotkey TEXT,
            event TEXT NOT NULL,
            payload JSONB NOT NULL,
            prev_hash TEXT NOT NULL,
            entry_hash TEXT NOT NULL,
            recorded_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT score_audit_log_entry_hash_key UNIQUE (entry_hash)
        )
        """
    )
    op.execute(
        "CREATE INDEX score_audit_log_agent_id_idx ON score_audit_log (agent_id)"
    )


def downgrade() -> None:
    """Drop the audit log table."""
    op.execute("DROP TABLE IF EXISTS score_audit_log")
