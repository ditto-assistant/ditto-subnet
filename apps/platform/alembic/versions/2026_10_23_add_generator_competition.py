"""Add the shadow DittoBench generator competition.

Generator miners upload the Go source of a CPU-only generator program. A
trusted scorer leases each active generator, builds it offline in a no-network
sandbox, runs it on fresh secret seeds plus the current probe seed, and runs
the resulting challenge packs against a panel of top agents. Platform scores
each run (research/dittobench-datagen/challenge), detects behavioral copies by
comparing generators' outputs on the shared probe seed, re-runs active
generators on new seeds periodically, and folds hotkeys into a weight-zero
generator ledger.

Revision ID: 5c1e9a7d2b40
Revises: 5f2c8e1a9d47
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "5c1e9a7d2b40"
down_revision: str | Sequence[str] | None = "5f2c8e1a9d47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NOW = sa.func.now()


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
        "generator_submissions",
        sa.Column("submission_id", sa.UUID(), primary_key=True),
        sa.Column("miner_hotkey", sa.Text(), nullable=False),
        sa.Column("owner_key", sa.Text(), nullable=False),
        sa.Column("source_sha256", sa.Text(), nullable=False),
        sa.Column("tree_sha256", sa.Text(), nullable=False),
        sa.Column("source_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("module", sa.Text(), nullable=False),
        sa.Column("go_version", sa.Text(), nullable=False),
        sa.Column("file_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("status_reason", sa.Text()),
        sa.Column("next_run_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_id", sa.UUID()),
        sa.Column("lease_run_id", sa.UUID()),
        sa.Column("lease_expires_at", sa.TIMESTAMP(timezone=True)),
        sa.Column(
            "submitted_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=_NOW,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=_NOW,
        ),
        sa.UniqueConstraint("source_sha256", name="generator_submissions_sha256_key"),
        sa.CheckConstraint(
            "source_sha256 ~ '^[0-9a-f]{64}$' AND tree_sha256 ~ '^[0-9a-f]{64}$'",
            name="generator_submissions_digest_check",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'superseded', 'rejected')",
            name="generator_submissions_status_check",
        ),
        sa.CheckConstraint(
            "size_bytes > 0 AND size_bytes = octet_length(source_bytes) "
            "AND size_bytes <= 4194304",
            name="generator_submissions_size_check",
        ),
        sa.CheckConstraint(
            "(lease_id IS NULL) = (lease_expires_at IS NULL) "
            "AND (lease_id IS NULL) = (lease_run_id IS NULL)",
            name="generator_submissions_lease_check",
        ),
        sa.CheckConstraint(
            "attempts >= 0", name="generator_submissions_attempts_check"
        ),
    )
    op.create_index(
        "generator_submissions_hotkey_idx",
        "generator_submissions",
        ["miner_hotkey", sa.text("submitted_at DESC")],
    )
    op.create_index(
        "generator_submissions_runnable_idx",
        "generator_submissions",
        ["next_run_at"],
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "generator_submissions_tree_idx", "generator_submissions", ["tree_sha256"]
    )

    op.create_table(
        "generator_probe_epochs",
        sa.Column("epoch", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("seed", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=_NOW,
        ),
        sa.Column("retired_at", sa.TIMESTAMP(timezone=True)),
        sa.CheckConstraint(
            "seed ~ '^[0-9a-f]{64}$'", name="generator_probe_epochs_seed_check"
        ),
    )
    op.create_index(
        "generator_probe_epochs_current_key",
        "generator_probe_epochs",
        [sa.text("(retired_at IS NULL)")],
        unique=True,
        postgresql_where=sa.text("retired_at IS NULL"),
    )

    op.create_table(
        "generator_operator_actions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("detail", postgresql.JSONB()),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=_NOW,
        ),
    )
    op.create_index(
        "generator_operator_actions_subject_idx",
        "generator_operator_actions",
        ["subject", "created_at"],
    )

    op.create_table(
        "generator_runs",
        sa.Column("run_id", sa.UUID(), primary_key=True),
        sa.Column(
            "submission_id",
            sa.UUID(),
            sa.ForeignKey("generator_submissions.submission_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("seeds", postgresql.JSONB(), nullable=False),
        sa.Column(
            "probe_epoch",
            sa.Integer(),
            sa.ForeignKey("generator_probe_epochs.epoch"),
            nullable=False,
        ),
        sa.Column("cases_per_seed", sa.Integer(), nullable=False),
        sa.Column("panel", postgresql.JSONB(), nullable=False),
        sa.Column("settings_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text()),
        sa.Column("score", sa.Float()),
        sa.Column("score_summary", postgresql.JSONB()),
        sa.Column("evaluation", postgresql.JSONB()),
        sa.Column("panel_results", postgresql.JSONB()),
        sa.Column("scorer_version", sa.Text()),
        sa.Column(
            "leased_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=_NOW,
        ),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True)),
        sa.CheckConstraint(
            "status IN ('leased', 'scored', 'invalid', 'insufficient_panel', "
            "'copy', 'abandoned')",
            name="generator_runs_status_check",
        ),
        sa.CheckConstraint(
            "(status = 'leased') = (finished_at IS NULL)",
            name="generator_runs_finished_check",
        ),
        sa.CheckConstraint(
            "(status = 'scored') = (score IS NOT NULL)",
            name="generator_runs_score_check",
        ),
        sa.CheckConstraint(
            "cases_per_seed BETWEEN 8 AND 64", name="generator_runs_cases_check"
        ),
    )
    op.create_index(
        "generator_runs_submission_idx",
        "generator_runs",
        ["submission_id", sa.text("leased_at DESC")],
    )
    op.create_index(
        "generator_runs_scored_idx",
        "generator_runs",
        ["finished_at"],
        postgresql_where=sa.text("status = 'scored'"),
    )

    op.create_table(
        "generator_probe_results",
        sa.Column(
            "submission_id",
            sa.UUID(),
            sa.ForeignKey("generator_submissions.submission_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "epoch",
            sa.Integer(),
            sa.ForeignKey("generator_probe_epochs.epoch"),
            primary_key=True,
        ),
        sa.Column(
            "run_id",
            sa.UUID(),
            sa.ForeignKey("generator_runs.run_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("fingerprints", postgresql.JSONB(), nullable=False),
        sa.Column(
            "recorded_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=_NOW,
        ),
    )
    op.create_index(
        "generator_probe_results_epoch_idx", "generator_probe_results", ["epoch"]
    )

    op.create_table(
        "generator_case_verdicts",
        sa.Column(
            "run_id",
            sa.UUID(),
            sa.ForeignKey("generator_runs.run_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("seed_index", sa.Integer(), primary_key=True),
        sa.Column("case_id", sa.Text(), primary_key=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("panel_size", sa.Integer(), nullable=False),
        sa.Column("solvers", sa.Integer(), nullable=False),
        sa.Column("solver_owners", sa.Integer(), nullable=False),
        sa.Column("mean_score", sa.Float(), nullable=False),
        sa.Column("champion_failed", sa.Boolean(), nullable=False),
        sa.Column("credit", sa.Float(), nullable=False),
        sa.CheckConstraint(
            "status IN ('discriminating', 'trivial', 'unsolved', 'duplicate')",
            name="generator_case_verdicts_status_check",
        ),
        sa.CheckConstraint(
            "fingerprint ~ '^[0-9a-f]{64}$'",
            name="generator_case_verdicts_fingerprint_check",
        ),
    )
    op.create_index(
        "generator_case_verdicts_fingerprint_idx",
        "generator_case_verdicts",
        ["fingerprint"],
    )


def downgrade() -> None:
    op.drop_table("generator_case_verdicts")
    op.drop_table("generator_probe_results")
    op.drop_table("generator_runs")
    op.drop_table("generator_operator_actions")
    op.drop_table("generator_probe_epochs")
    op.drop_table("generator_submissions")
    op.drop_table("generator_competition_settings_revisions")
