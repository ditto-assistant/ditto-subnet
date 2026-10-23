"""Add the shadow DittoBench generator competition.

Generator miners upload challenge packs (inert JSON: a memory haystack plus
cases with deterministic gold answers). A trusted scorer leases each pack,
validates it with the contract in research/dittobench-datagen/challenge, runs
it against a panel of top agents, and posts per-agent results; Platform scores
the pack and folds hotkeys into a weight-zero generator ledger.

Revision ID: 5c1e9a7d2b40
Revises: 73bd92c14f60
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "5c1e9a7d2b40"
down_revision: str | Sequence[str] | None = "73bd92c14f60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "generator_competition_settings_revisions",
        sa.Column("revision", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("parent_revision", sa.Integer(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("settings", postgresql.JSONB(), nullable=False),
        sa.Column("checksum", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("scope = '*'", name="generator_settings_scope_check"),
        sa.CheckConstraint(
            "length(checksum) = 64", name="generator_settings_checksum_check"
        ),
        sa.CheckConstraint(
            "parent_revision >= 0", name="generator_settings_parent_revision_check"
        ),
        sa.CheckConstraint(
            "length(trim(reason)) >= 8", name="generator_settings_reason_check"
        ),
        sa.CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="generator_settings_actor_check",
        ),
        sa.UniqueConstraint(
            "scope", "parent_revision", name="generator_settings_scope_parent_key"
        ),
    )

    op.create_table(
        "generator_packs",
        sa.Column("pack_id", sa.UUID(), primary_key=True),
        sa.Column("miner_hotkey", sa.Text(), nullable=False),
        sa.Column("owner_key", sa.Text(), nullable=False),
        sa.Column("pack_sha256", sa.Text(), nullable=False),
        sa.Column("pack_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("case_count", sa.Integer(), nullable=False),
        sa.Column("generator_name", sa.Text(), nullable=False),
        sa.Column("generator_version", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("not_before", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("lease_id", sa.UUID()),
        sa.Column("lease_expires_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("panel", postgresql.JSONB()),
        sa.Column("validation", postgresql.JSONB()),
        sa.Column("panel_results", postgresql.JSONB()),
        sa.Column("score", sa.Float()),
        sa.Column("score_summary", postgresql.JSONB()),
        sa.Column("settings_revision", sa.Integer()),
        sa.Column("scorer_version", sa.Text()),
        sa.Column("last_outcome", sa.Text()),
        sa.Column(
            "submitted_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("scored_at", sa.TIMESTAMP(timezone=True)),
        sa.UniqueConstraint("pack_sha256", name="generator_packs_sha256_key"),
        sa.CheckConstraint(
            "pack_sha256 ~ '^[0-9a-f]{64}$'", name="generator_packs_sha256_check"
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'leased', 'scored', 'invalid', 'failed')",
            name="generator_packs_status_check",
        ),
        sa.CheckConstraint(
            "size_bytes > 0 AND size_bytes = octet_length(pack_bytes)",
            name="generator_packs_size_check",
        ),
        sa.CheckConstraint(
            "case_count BETWEEN 1 AND 64", name="generator_packs_case_count_check"
        ),
        sa.CheckConstraint(
            "(status = 'leased') = (lease_id IS NOT NULL)",
            name="generator_packs_lease_check",
        ),
        sa.CheckConstraint(
            "score IS NULL OR (score >= 0 AND score <= 1)",
            name="generator_packs_score_check",
        ),
    )
    op.create_index(
        "generator_packs_queue_idx",
        "generator_packs",
        ["status", "not_before", "submitted_at"],
    )
    op.create_index(
        "generator_packs_hotkey_idx",
        "generator_packs",
        ["miner_hotkey", "submitted_at"],
    )

    op.create_table(
        "generator_case_verdicts",
        sa.Column("pack_id", sa.UUID(), nullable=False),
        sa.Column("case_id", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("panel_size", sa.Integer(), nullable=False),
        sa.Column("solvers", sa.Integer(), nullable=False),
        sa.Column("solver_owners", sa.Integer(), nullable=False),
        sa.Column("mean_score", sa.Float(), nullable=False),
        sa.Column("champion_failed", sa.Boolean(), nullable=False),
        sa.Column("credit", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["pack_id"], ["generator_packs.pack_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("pack_id", "case_id"),
        sa.CheckConstraint(
            "fingerprint ~ '^[0-9a-f]{64}$'",
            name="generator_case_verdicts_fingerprint_check",
        ),
        sa.CheckConstraint(
            "status IN ('discriminating', 'trivial', 'unsolved', 'duplicate')",
            name="generator_case_verdicts_status_check",
        ),
    )
    op.create_index(
        "generator_case_verdicts_fingerprint_idx",
        "generator_case_verdicts",
        ["fingerprint"],
    )


def downgrade() -> None:
    op.drop_index(
        "generator_case_verdicts_fingerprint_idx",
        table_name="generator_case_verdicts",
    )
    op.drop_table("generator_case_verdicts")
    op.drop_index("generator_packs_hotkey_idx", table_name="generator_packs")
    op.drop_index("generator_packs_queue_idx", table_name="generator_packs")
    op.drop_table("generator_packs")
    op.drop_table("generator_competition_settings_revisions")
