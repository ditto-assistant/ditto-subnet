"""Add the model_context (CPM) competition.

Miners submit small open-weight models pinned at a Hugging Face revision,
committed on chain as ``dcpm1:<manifest_sha256>``. Platform screens them,
opens weekly seasons with three public seeds, leases runs to a trusted CPM
scorer, and publishes a shadow ledger (docs/research/cpm/competition-v1.md
section 5). Five new tables, no hot table touched; the empty settings table
resolves to ``mode=off``, so this migration changes no live behavior.

Revision ID: 8e3b6f1a2c94
Revises: 6a2d9c4e8b71
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "8e3b6f1a2c94"
down_revision: str | Sequence[str] | None = "6a2d9c4e8b71"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NOW = sa.func.now()


def _ts(name: str, *, nullable: bool = True, now: bool = False) -> sa.Column:
    return sa.Column(
        name,
        sa.TIMESTAMP(timezone=True),
        nullable=nullable,
        server_default=_NOW if now else None,
    )


def upgrade() -> None:
    op.create_table(
        "model_context_settings_revisions",
        sa.Column("revision", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("parent_revision", sa.Integer(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("settings", postgresql.JSONB(), nullable=False),
        sa.Column("checksum", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        _ts("created_at", nullable=False, now=True),
        sa.CheckConstraint("scope = '*'", name="model_context_settings_scope_check"),
        sa.CheckConstraint(
            "length(checksum) = 64", name="model_context_settings_checksum_check"
        ),
        sa.CheckConstraint(
            "parent_revision >= 0",
            name="model_context_settings_parent_revision_check",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) >= 8", name="model_context_settings_reason_check"
        ),
        sa.CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="model_context_settings_actor_check",
        ),
        sa.UniqueConstraint(
            "scope", "parent_revision", name="model_context_settings_scope_parent_key"
        ),
    )

    op.create_table(
        "model_context_submissions",
        sa.Column("submission_id", sa.UUID(), primary_key=True),
        sa.Column("hotkey", sa.Text(), nullable=False),
        sa.Column("hf_repo", sa.Text(), nullable=False),
        sa.Column("revision", sa.Text(), nullable=False),
        sa.Column("manifest", postgresql.JSONB(), nullable=False),
        sa.Column("manifest_sha256", sa.Text(), nullable=False),
        sa.Column("weights_sha256", sa.Text()),
        sa.Column("base_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("commitment_block", sa.BigInteger(), nullable=False),
        sa.Column("total_bytes", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reject_reason", sa.Text()),
        sa.Column("mirror_repo", sa.Text()),
        sa.Column("screening_detail", postgresql.JSONB()),
        sa.Column(
            "screening_attempts", sa.Integer(), nullable=False, server_default="0"
        ),
        _ts("screening_lease_until"),
        _ts("created_at", nullable=False, now=True),
        _ts("updated_at", nullable=False, now=True),
        sa.CheckConstraint(
            "length(hotkey) BETWEEN 1 AND 64", name="model_context_submissions_hotkey"
        ),
        sa.CheckConstraint(
            "length(hf_repo) BETWEEN 3 AND 200",
            name="model_context_submissions_hf_repo",
        ),
        sa.CheckConstraint(
            "revision ~ '^[0-9a-f]{40}$'", name="model_context_submissions_revision"
        ),
        sa.CheckConstraint(
            "manifest_sha256 ~ '^[0-9a-f]{64}$' AND (weights_sha256 IS NULL "
            "OR weights_sha256 ~ '^[0-9a-f]{64}$')",
            name="model_context_submissions_digests",
        ),
        sa.CheckConstraint(
            "length(base_id) BETWEEN 1 AND 64", name="model_context_submissions_base"
        ),
        sa.CheckConstraint(
            "kind IN ('lora', 'full')", name="model_context_submissions_kind"
        ),
        sa.CheckConstraint(
            "status IN ('screening', 'queued', 'scored', 'rejected', 'superseded')",
            name="model_context_submissions_status",
        ),
        sa.CheckConstraint(
            "commitment_block >= 0 AND total_bytes > 0 AND screening_attempts >= 0",
            name="model_context_submissions_counts",
        ),
        sa.CheckConstraint(
            "reject_reason IS NULL OR length(reject_reason) <= 1000",
            name="model_context_submissions_reject_reason",
        ),
        sa.CheckConstraint(
            "mirror_repo IS NULL OR length(mirror_repo) <= 200",
            name="model_context_submissions_mirror_repo",
        ),
        sa.CheckConstraint(
            "(status = 'rejected') = (reject_reason IS NOT NULL)",
            name="model_context_submissions_rejected",
        ),
        sa.CheckConstraint(
            "status IN ('screening', 'rejected') OR weights_sha256 IS NOT NULL",
            name="model_context_submissions_screened",
        ),
    )
    op.create_index(
        "model_context_submissions_hotkey_idx",
        "model_context_submissions",
        ["hotkey", sa.text("created_at DESC")],
    )
    op.create_index(
        "model_context_submissions_status_idx",
        "model_context_submissions",
        ["status", "created_at"],
    )
    op.create_index(
        "model_context_submissions_weights_idx",
        "model_context_submissions",
        ["weights_sha256"],
    )
    op.create_index(
        "model_context_submissions_manifest_idx",
        "model_context_submissions",
        ["manifest_sha256"],
    )

    op.create_table(
        "model_context_seasons",
        sa.Column("season", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("open_block", sa.BigInteger(), nullable=False),
        sa.Column("open_block_hash", sa.Text(), nullable=False),
        sa.Column("seeds", postgresql.JSONB(), nullable=False),
        sa.Column("bench_version", sa.Integer(), nullable=False),
        sa.Column("cpm_score_version", sa.Integer(), nullable=False),
        sa.Column(
            "base_floors",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("gpu_sku", sa.Text()),
        sa.Column("settings_revision", sa.Integer(), nullable=False),
        _ts("opened_at", nullable=False, now=True),
        _ts("closes_at", nullable=False),
        _ts("closed_at"),
        sa.CheckConstraint("season >= 1", name="model_context_seasons_season"),
        sa.CheckConstraint(
            "open_block_hash ~ '^0x[0-9a-f]{64}$'",
            name="model_context_seasons_block_hash",
        ),
        sa.CheckConstraint(
            "open_block >= 0 AND bench_version >= 1 AND cpm_score_version >= 1",
            name="model_context_seasons_versions",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(seeds) = 'array' AND jsonb_array_length(seeds) = 3",
            name="model_context_seasons_seeds",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(base_floors) = 'object'",
            name="model_context_seasons_base_floors",
        ),
        sa.CheckConstraint(
            "gpu_sku IS NULL OR length(gpu_sku) <= 64",
            name="model_context_seasons_gpu_sku",
        ),
        sa.CheckConstraint(
            "closes_at > opened_at", name="model_context_seasons_window"
        ),
    )
    # At most one open season.
    op.create_index(
        "model_context_seasons_open_key",
        "model_context_seasons",
        [sa.text("(closed_at IS NULL)")],
        unique=True,
        postgresql_where=sa.text("closed_at IS NULL"),
    )

    op.create_table(
        "model_context_runs",
        sa.Column("run_id", sa.UUID(), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column(
            "submission_id",
            sa.UUID(),
            sa.ForeignKey(
                "model_context_submissions.submission_id", ondelete="CASCADE"
            ),
        ),
        sa.Column("base_id", sa.Text(), nullable=False),
        sa.Column(
            "season",
            sa.Integer(),
            sa.ForeignKey("model_context_seasons.season"),
            nullable=False,
        ),
        sa.Column("cpm_score_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("score", sa.Float()),
        sa.Column("components", postgresql.JSONB()),
        sa.Column("per_seed", postgresql.JSONB()),
        sa.Column("scorer_id", sa.Text()),
        sa.Column("scorer_version", sa.Text()),
        sa.Column("gpu_sku", sa.Text()),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_id", sa.UUID()),
        _ts("lease_expires_at"),
        _ts("created_at", nullable=False, now=True),
        _ts("started_at"),
        _ts("finished_at"),
        sa.Column("fault", sa.Text()),
        sa.CheckConstraint(
            "kind IN ('submission', 'reference')", name="model_context_runs_kind"
        ),
        sa.CheckConstraint(
            "(kind = 'submission') = (submission_id IS NOT NULL)",
            name="model_context_runs_subject",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'leased', 'scored', 'failed', 'abandoned')",
            name="model_context_runs_status",
        ),
        sa.CheckConstraint(
            "(status = 'scored') = (score IS NOT NULL) "
            "AND (score IS NULL OR (score >= 0 AND score <= 1))",
            name="model_context_runs_score",
        ),
        sa.CheckConstraint(
            "(status = 'leased') = (lease_id IS NOT NULL) "
            "AND (lease_id IS NULL) = (lease_expires_at IS NULL)",
            name="model_context_runs_lease",
        ),
        sa.CheckConstraint(
            "(status IN ('scored', 'failed', 'abandoned')) = (finished_at IS NOT NULL)",
            name="model_context_runs_finished",
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND cpm_score_version >= 1",
            name="model_context_runs_counts",
        ),
        sa.CheckConstraint(
            "(fault IS NULL OR length(fault) <= 2000) "
            "AND (scorer_id IS NULL OR length(scorer_id) <= 120) "
            "AND (scorer_version IS NULL OR length(scorer_version) <= 64) "
            "AND (gpu_sku IS NULL OR length(gpu_sku) <= 64) "
            "AND length(base_id) BETWEEN 1 AND 64",
            name="model_context_runs_text_bounds",
        ),
    )
    op.create_index(
        "model_context_runs_submission_idx",
        "model_context_runs",
        ["submission_id", "season"],
    )
    op.create_index(
        "model_context_runs_season_idx", "model_context_runs", ["season", "status"]
    )
    op.create_index(
        "model_context_runs_pending_idx",
        "model_context_runs",
        ["created_at"],
        postgresql_where=sa.text("status IN ('queued', 'leased')"),
    )
    # One open run per submission and season, and per reference base and season.
    op.create_index(
        "model_context_runs_open_submission_key",
        "model_context_runs",
        ["submission_id", "season"],
        unique=True,
        postgresql_where=sa.text(
            "kind = 'submission' AND status IN ('queued', 'leased')"
        ),
    )
    op.create_index(
        "model_context_runs_open_reference_key",
        "model_context_runs",
        ["season", "base_id"],
        unique=True,
        postgresql_where=sa.text(
            "kind = 'reference' AND status IN ('queued', 'leased')"
        ),
    )

    op.create_table(
        "model_context_scorer_heartbeats",
        sa.Column("scorer_id", sa.Text(), primary_key=True),
        sa.Column("scorer_version", sa.Text()),
        sa.Column("gpu_sku", sa.Text()),
        sa.Column("last_poll", sa.Text(), nullable=False),
        _ts("first_seen_at", nullable=False, now=True),
        _ts("last_seen_at", nullable=False, now=True),
        sa.CheckConstraint(
            "length(scorer_id) BETWEEN 1 AND 120 "
            "AND (scorer_version IS NULL OR length(scorer_version) <= 64) "
            "AND (gpu_sku IS NULL OR length(gpu_sku) <= 64) "
            "AND length(last_poll) <= 32",
            name="model_context_scorer_heartbeats_bounds",
        ),
    )


def downgrade() -> None:
    op.drop_table("model_context_scorer_heartbeats")
    op.drop_table("model_context_runs")
    op.drop_table("model_context_seasons")
    op.drop_table("model_context_submissions")
    op.drop_table("model_context_settings_revisions")
