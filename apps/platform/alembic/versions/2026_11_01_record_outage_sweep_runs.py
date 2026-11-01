"""record post-outage automatic void sweeps

Revision ID: b4f81d2c6e93
Revises: 7e2b9c4d1a60
Create Date: 2026-11-01

The provider-outage circuit parks scoring while it is open, but it closes on
any successful call, so in a low-balance flap a continual retest can still
finish with many credit-refused calls and be accepted. The platform-role sweep
voids those samples through the same evidence gate operators use
(``confirmation_score_voids.source = 'outage_sweep'``) and flags canonical
scores with the same evidence for an operator to replace.

``outage_sweep_runs`` is the append-only record of every sweep: the scanned
watermark range, how many samples were examined, the voids written, the outage
windows observed and the flagged canonical scores. The next run resumes from
the last row's watermark.

``confirmation_scores_version_created_idx`` serves the sweep's keyset read in
acceptance order. The table holds low thousands of rows, so a plain index
build is one short lock.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b4f81d2c6e93"
down_revision: str | Sequence[str] | None = "7e2b9c4d1a60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RUNS = "outage_sweep_runs"


def upgrade() -> None:
    op.create_index(
        "confirmation_scores_version_created_idx",
        "confirmation_scores",
        ["bench_version", "created_at"],
    )
    op.create_table(
        _RUNS,
        sa.Column("sweep_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("bench_version", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("watermark_from", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("watermark_to", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("watermark_to_key", postgresql.JSONB(), nullable=True),
        sa.Column("examined_count", sa.Integer(), nullable=False),
        sa.Column("voided_count", sa.Integer(), nullable=False),
        sa.Column("error_count", sa.Integer(), nullable=False),
        sa.Column("voids", postgresql.JSONB(), nullable=False),
        sa.Column("outage_windows", postgresql.JSONB(), nullable=False),
        sa.Column("canonical_flagged", postgresql.JSONB(), nullable=False),
        sa.Column("errors", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint(
            "examined_count >= 0 AND voided_count >= 0 AND error_count >= 0 "
            "AND voided_count + error_count <= examined_count",
            name="outage_sweep_runs_counts_check",
        ),
        sa.CheckConstraint(
            "watermark_to >= watermark_from",
            name="outage_sweep_runs_watermark_check",
        ),
        sa.CheckConstraint(
            "finished_at >= started_at",
            name="outage_sweep_runs_finished_check",
        ),
    )
    op.create_index(
        "outage_sweep_runs_version_started_idx",
        _RUNS,
        ["bench_version", "started_at"],
    )
    op.create_index("outage_sweep_runs_started_idx", _RUNS, ["started_at"])


def downgrade() -> None:
    op.drop_index("outage_sweep_runs_started_idx", table_name=_RUNS)
    op.drop_index("outage_sweep_runs_version_started_idx", table_name=_RUNS)
    op.drop_table(_RUNS)
    op.drop_index(
        "confirmation_scores_version_created_idx", table_name="confirmation_scores"
    )
