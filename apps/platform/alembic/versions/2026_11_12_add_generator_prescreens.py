"""Generator pre-screens: small, cheap panel runs for never-scored generators.

A pre-screen batch runs up to a few never-scored generators' packs
(``cases_per_column`` cases each, on a fresh secret seed) on a small panel
while no round needs it; its column scores only order the challenger queue
and are never paid. Batches, their validator attempts and their panel results
live in their own tables, apart from chain-epoch rounds. Indexes start at
2**40 so the scorer and validator wire protocols, which carry a round index,
route a pre-screen apart from a round. Each generator keeps its latest
pre-screen index and score.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "c3e8a2f61d4b"
down_revision = "e5a91c3d7b60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "generator_prescreens",
        sa.Column("prescreen_index", sa.BigInteger(), primary_key=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("seed", sa.Text(), nullable=False),
        sa.Column("settings_revision", sa.Integer(), nullable=False),
        sa.Column("cases_per_column", sa.Integer(), nullable=False),
        sa.Column("bench_version", sa.Integer(), nullable=False),
        sa.Column("columns", postgresql.JSONB(), nullable=False),
        sa.Column("panel", postgresql.JSONB(), nullable=False),
        sa.Column("opened_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("lease_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("lease_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("generation", postgresql.JSONB(), nullable=True),
        sa.Column("dataset_sha256", sa.Text(), nullable=True),
        sa.Column("dataset_bytes", sa.LargeBinary(), nullable=True),
        sa.Column("column_cases", postgresql.JSONB(), nullable=True),
        sa.Column("score", postgresql.JSONB(), nullable=True),
        sa.Column("scorer_version", sa.Text(), nullable=True),
        sa.Column("closed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("void_reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "prescreen_index >= 1099511627776",
            name="generator_prescreens_index_check",
        ),
        sa.CheckConstraint(
            "status IN ('generating', 'running', 'scoring', 'scored', 'void')",
            name="generator_prescreens_status_check",
        ),
        sa.CheckConstraint(
            "cases_per_column BETWEEN 8 AND 80",
            name="generator_prescreens_cases_check",
        ),
        sa.CheckConstraint(
            "(status = 'void') = (void_reason IS NOT NULL)",
            name="generator_prescreens_void_reason_check",
        ),
    )
    op.create_index(
        "generator_prescreens_status_idx", "generator_prescreens", ["status"]
    )
    op.create_table(
        "generator_prescreen_attempts",
        sa.Column("prescreen_index", sa.BigInteger(), primary_key=True),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("validator_hotkey", sa.Text(), primary_key=True),
        sa.Column("ticket_deadline", sa.TIMESTAMP(timezone=True), primary_key=True),
        sa.Column("slot_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("issued_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["prescreen_index"],
            ["generator_prescreens.prescreen_index"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "status IN ('leased', 'completed', 'failed')",
            name="generator_prescreen_attempts_status_check",
        ),
    )
    op.create_table(
        "generator_prescreen_results",
        sa.Column("prescreen_index", sa.BigInteger(), primary_key=True),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("validator_hotkey", sa.Text(), nullable=False),
        sa.Column("ticket_deadline", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("results", postgresql.JSONB(), nullable=False),
        sa.Column("answered", sa.Integer(), nullable=False),
        sa.Column("faulted", sa.Integer(), nullable=False),
        sa.Column("signature", sa.Text(), nullable=False),
        sa.Column(
            "recorded_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["prescreen_index"],
            ["generator_prescreens.prescreen_index"],
            ondelete="CASCADE",
        ),
    )
    op.add_column(
        "generator_submissions",
        sa.Column("prescreen_score", sa.Float(), nullable=True),
    )
    op.add_column(
        "generator_submissions",
        sa.Column("prescreen_index", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("generator_submissions", "prescreen_index")
    op.drop_column("generator_submissions", "prescreen_score")
    op.drop_table("generator_prescreen_results")
    op.drop_table("generator_prescreen_attempts")
    op.drop_index("generator_prescreens_status_idx", table_name="generator_prescreens")
    op.drop_table("generator_prescreens")
