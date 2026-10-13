"""add Season 2 reranker submissions (shadow intake, SN-207)

One new table, ``reranker_submissions``, for the Season 2 reranker track.

It is deliberately separate from ``agents``. Every ``agents`` row in
``uploaded`` is claimed by the Docker screener, allocated to validators, and
joined into the leaderboard, quorum, anti-copy fingerprinting and
``evaluation_payments``. A reranker artifact is an ONNX model, not a harness,
so none of those paths may ever see it.

Shadow only: no payment, no weights. ``seed`` and ``seed_block_hash`` stay
null at intake; the trusted scorer worker derives them after the commit from a
finalized block. ``artifact_sha256`` is unique so an exact duplicate of an
earlier artifact is rejected and the first submitter keeps it.

Revision ID: 3e8b5c1d9a47
Revises: 7d3ca1e6a290
Create Date: 2026-10-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "3e8b5c1d9a47"
down_revision: str | Sequence[str] | None = "7d3ca1e6a290"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DIGEST = "~ '^[0-9a-f]{64}$'"
# Check names are short: the metadata naming convention renders them as
# ``ck_reranker_submissions_<name>``.


def upgrade() -> None:
    op.create_table(
        "reranker_submissions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("miner_hotkey", sa.Text(), nullable=False),
        sa.Column("miner_coldkey", sa.Text(), nullable=True),
        sa.Column("upload_sha256", sa.Text(), nullable=False),
        sa.Column("artifact_sha256", sa.Text(), nullable=False),
        sa.Column("contract_sha256", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("base_model", sa.Text(), nullable=False),
        sa.Column("base_model_license", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'accepted'"),
            nullable=False,
        ),
        sa.Column("invalid_reason", sa.Text(), nullable=True),
        sa.Column("seed_block_hash", sa.Text(), nullable=True),
        sa.Column("seed", sa.BigInteger(), nullable=True),
        sa.Column("score", postgresql.JSONB(), nullable=True),
        sa.Column("scored_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('accepted', 'invalid', 'scored', 'failed')",
            name="status",
        ),
        sa.CheckConstraint(
            f"upload_sha256 {_DIGEST} AND artifact_sha256 {_DIGEST} "
            f"AND contract_sha256 {_DIGEST}",
            name="digests",
        ),
        sa.CheckConstraint("size_bytes > 0", name="size"),
        sa.CheckConstraint(
            "length(base_model) BETWEEN 1 AND 256 "
            "AND length(base_model_license) BETWEEN 1 AND 64",
            name="base_model",
        ),
        sa.CheckConstraint("seed IS NULL OR seed >= 0", name="seed"),
        sa.PrimaryKeyConstraint("id", name="reranker_submissions_pkey"),
        sa.UniqueConstraint(
            "artifact_sha256", name="reranker_submissions_artifact_sha256_key"
        ),
    )
    op.create_index(
        "reranker_submissions_status_created_idx",
        "reranker_submissions",
        ["status", "created_at"],
    )
    op.create_index(
        "reranker_submissions_hotkey_created_idx",
        "reranker_submissions",
        ["miner_hotkey", "created_at"],
    )
    op.create_index(
        "reranker_submissions_coldkey_created_idx",
        "reranker_submissions",
        ["miner_coldkey", "created_at"],
    )
    op.create_index(
        "reranker_submissions_created_idx",
        "reranker_submissions",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("reranker_submissions_created_idx", table_name="reranker_submissions")
    op.drop_index(
        "reranker_submissions_coldkey_created_idx", table_name="reranker_submissions"
    )
    op.drop_index(
        "reranker_submissions_hotkey_created_idx", table_name="reranker_submissions"
    )
    op.drop_index(
        "reranker_submissions_status_created_idx", table_name="reranker_submissions"
    )
    op.drop_table("reranker_submissions")
