"""record infrastructure voids for continual-retest samples

Revision ID: 7e2b9c4d1a60
Revises: 5d1c7e9a2b40
Create Date: 2026-11-01

A continual-retest sample is an immutable signed composite, one row per
``(agent, version, validator, seed)``. On 2026-10-31 03:59-04:09Z the inference
gateway refused about half of two Wonder v6 retests' calls for exhausted
credits and both were accepted at 0.65/0.70 against a 0.92 median. Nothing
could set them aside: no operator tool reached the row, and a re-run on the
same key was dropped by ``ON CONFLICT DO NOTHING``.

``confirmation_score_voids`` is the append-only record that a specific sample
measured the platform, not the agent: the exact row (including its run id and
composite), the reason, the evidence and its digest, the actor and the source
(an operator or the post-outage sweep). It is never updated or deleted. Every
fold read excludes a voided row.

``confirmation_scores.void_generation`` lets the re-run land. It joins the
primary key, so the first result for a key is generation 0 and a re-run after
one void is generation 1. The score rows themselves stay immutable: nothing
is ever updated, only appended at the next generation.

The table is small (hundreds to low thousands of rows), so the primary-key
rebuild is one short exclusive lock.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "7e2b9c4d1a60"
down_revision: str | Sequence[str] | None = "5d1c7e9a2b40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCORES = "confirmation_scores"
_VOIDS = "confirmation_score_voids"
_PK_COLUMNS = ["agent_id", "bench_version", "validator_hotkey", "seed"]


def upgrade() -> None:
    op.add_column(
        _SCORES,
        sa.Column(
            "void_generation",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.create_check_constraint(
        "confirmation_scores_void_generation_check", _SCORES, "void_generation >= 0"
    )
    op.drop_constraint("confirmation_scores_pkey", _SCORES, type_="primary")
    op.create_primary_key(
        "confirmation_scores_pkey", _SCORES, [*_PK_COLUMNS, "void_generation"]
    )

    op.create_table(
        _VOIDS,
        sa.Column("void_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("bench_version", sa.Integer(), nullable=False),
        sa.Column("validator_hotkey", sa.Text(), nullable=False),
        sa.Column("seed", sa.BigInteger(), nullable=False),
        sa.Column("void_generation", sa.Integer(), nullable=False),
        sa.Column("sample_kind", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("composite", sa.Float(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.Column("evidence_digest", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("outage_started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("outage_ended_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            [*_PK_COLUMNS, "void_generation"],
            [f"{_SCORES}.{column}" for column in [*_PK_COLUMNS, "void_generation"]],
            ondelete="CASCADE",
            name="confirmation_score_voids_sample_fkey",
        ),
        sa.UniqueConstraint(
            *_PK_COLUMNS,
            "void_generation",
            name="confirmation_score_voids_sample_key",
        ),
        sa.CheckConstraint(
            "sample_kind IN ('continual_retest')",
            name="confirmation_score_voids_sample_kind_check",
        ),
        sa.CheckConstraint(
            "source IN ('operator', 'outage_sweep')",
            name="confirmation_score_voids_source_check",
        ),
        sa.CheckConstraint(
            "evidence_digest ~ '^[0-9a-f]{64}$'",
            name="confirmation_score_voids_evidence_digest_check",
        ),
        sa.CheckConstraint(
            "length(btrim(reason)) >= 8",
            name="confirmation_score_voids_reason_check",
        ),
        sa.CheckConstraint(
            "outage_ended_at IS NULL OR outage_started_at IS NOT NULL",
            name="confirmation_score_voids_outage_window_check",
        ),
    )
    op.create_index(
        "confirmation_score_voids_agent_idx",
        _VOIDS,
        ["agent_id", "bench_version"],
    )
    op.create_index("confirmation_score_voids_created_idx", _VOIDS, ["created_at"])


def downgrade() -> None:
    op.drop_index("confirmation_score_voids_created_idx", table_name=_VOIDS)
    op.drop_index("confirmation_score_voids_agent_idx", table_name=_VOIDS)
    op.drop_table(_VOIDS)
    # A re-run row at generation >= 1 cannot survive the narrower key.
    op.execute(f"DELETE FROM {_SCORES} WHERE void_generation > 0")
    op.drop_constraint("confirmation_scores_pkey", _SCORES, type_="primary")
    op.create_primary_key("confirmation_scores_pkey", _SCORES, _PK_COLUMNS)
    op.drop_constraint(
        "confirmation_scores_void_generation_check", _SCORES, type_="check"
    )
    op.drop_column(_SCORES, "void_generation")
