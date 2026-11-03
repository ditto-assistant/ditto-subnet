"""publish dueling season commitments on chain through elected validators

Revision ID: d9e291eb6ec3
Revises: 8e2a4c6d1f37
Create Date: 2026-11-03

A committed dueling season's seed commitment is published on chain as
``dds1:{season_index}:{commitment}`` before its anchor block exists, when the
generator competition's ``onchain_commitment`` is on (``dueling-bench.md``
section 2). Platform elects validators exactly as for generator-round
``dgr1`` commitments and records each claim and report in a new table; the
season row carries the publication state and the first confirmed block and
extrinsic. Both tables are small and off the request hot path, so plain adds
are fine. Existing seasons read as ``disabled`` (never published).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d9e291eb6ec3"
down_revision: str | Sequence[str] | None = "8e2a4c6d1f37"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEASONS = "dueling_seasons"
PUBLICATIONS = "dueling_season_commitment_publications"


def upgrade() -> None:
    ts = sa.TIMESTAMP(timezone=True)
    op.add_column(
        SEASONS,
        sa.Column(
            "commitment_publication",
            sa.Text(),
            nullable=False,
            server_default="disabled",
        ),
    )
    op.add_column(SEASONS, sa.Column("commitment_published_at", ts))
    op.add_column(SEASONS, sa.Column("commitment_block", sa.BigInteger()))
    op.add_column(SEASONS, sa.Column("commitment_extrinsic_hash", sa.Text()))
    op.add_column(SEASONS, sa.Column("commitment_error", sa.Text()))
    op.create_check_constraint(
        "dueling_seasons_commitment_publication_check",
        SEASONS,
        "commitment_publication IN ('pending', 'published', 'failed', 'disabled')",
    )
    op.create_table(
        PUBLICATIONS,
        sa.Column("season_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("validator_hotkey", sa.Text(), primary_key=True),
        sa.Column("commitment", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("claimed_at", ts, nullable=False),
        sa.Column("claim_expires_at", ts, nullable=False),
        sa.Column("reported_at", ts),
        sa.Column("block_number", sa.BigInteger()),
        sa.Column("extrinsic_hash", sa.Text()),
        sa.Column("detail", sa.Text()),
        sa.ForeignKeyConstraint(["season_id"], [f"{SEASONS}.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "status IN ('claimed', 'confirmed', 'failed')",
            name="season_publication_status_check",
        ),
        sa.CheckConstraint(
            "status <> 'confirmed' OR block_number IS NOT NULL",
            name="season_publication_block_check",
        ),
    )


def downgrade() -> None:
    op.drop_table(PUBLICATIONS)
    op.drop_constraint(
        "dueling_seasons_commitment_publication_check", SEASONS, type_="check"
    )
    op.drop_column(SEASONS, "commitment_error")
    op.drop_column(SEASONS, "commitment_extrinsic_hash")
    op.drop_column(SEASONS, "commitment_block")
    op.drop_column(SEASONS, "commitment_published_at")
    op.drop_column(SEASONS, "commitment_publication")
