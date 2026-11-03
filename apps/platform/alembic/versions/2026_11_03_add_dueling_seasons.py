"""add dueling seasons, season batteries and slice preparations (bench v16, OFF)

Revision ID: 05e61b757387
Revises: 6ae1bd48a278
Create Date: 2026-11-03

The dueling bench (``research/dittobench-datagen/docs/dueling-bench.md``,
section 7.6) runs every bench v16+ agent run on DittoBench datagen cases plus a
generator slice from a frozen season of up to five generators. This migration
only adds storage; nothing opens a season until ``dueling_enabled`` is set in
the generator competition settings and a v16+ contract is active or rolling
out.

- ``dueling_seasons``: commitment, Platform-private secret, anchor block, the
  season seed (served only after reveal), the frozen column pin, close/reveal
  bookkeeping and the audited early-rotation request.
- ``dueling_season_batteries``: each candidate generator's 32-seed season
  qualification battery.
- ``dueling_slice_preparations``: one generator slice per ``(season,
  run_seed)``, with its bytes in Platform-private storage and their SHA-256.
- ``generator_scorer_heartbeats`` gains ``dueling_work_kinds`` and accepts a
  ``dueling`` poll.
- ``validator_tickets`` gains ``dueling_season_id`` and ``slice_sha256``;
  ``scores`` gains ``dueling_season_id`` and the ``dueling`` aggregates;
  ``confirmation_scores`` gains ``dueling_season_id`` and the per-seed
  ``dueling`` aggregates the pair-masked fold reads.

Every bench-version constraint here is a floor (``>= 16``), never an
enumeration. The hot-table columns are nullable and undefaulted, so they go
through ``safe_add_column`` first (metadata-only, idempotent, each in its own
short transaction); the new tables and the NOT VALID ``scores`` floor follow in
the migration's own transaction, so a replay after a partial run is safe.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from ditto.db.migration_lock import safe_add_column, safe_drop_column

revision: str = "05e61b757387"
down_revision: str | Sequence[str] | None = "6ae1bd48a278"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _tz() -> sa.types.TypeEngine:
    return sa.TIMESTAMP(timezone=True)


def _json() -> sa.types.TypeEngine:
    return postgresql.JSONB(none_as_null=True)


def upgrade() -> None:
    # Hot tables first, one column per short transaction (see module doc).
    safe_add_column("validator_tickets", "dueling_season_id", "UUID")
    safe_add_column("validator_tickets", "slice_sha256", "TEXT")
    safe_add_column("scores", "dueling_season_id", "UUID")
    safe_add_column("scores", "dueling", "JSONB")
    safe_add_column("confirmation_scores", "dueling_season_id", "UUID")
    safe_add_column("confirmation_scores", "dueling", "JSONB")

    # Every existing row is NULL, so NOT VALID loses nothing and skips the
    # full-table validation scan under the lock.
    op.execute(
        "ALTER TABLE scores ADD CONSTRAINT ck_scores_scores_dueling_bench_floor "
        "CHECK ((dueling_season_id IS NULL AND dueling IS NULL) "
        "OR bench_version >= 16) NOT VALID"
    )

    # The generator scorer advertises its dueling work kinds on a new poll.
    op.add_column(
        "generator_scorer_heartbeats",
        sa.Column("dueling_work_kinds", postgresql.JSONB(none_as_null=True)),
    )
    op.create_check_constraint(
        "generator_scorer_heartbeats_dueling_kinds_check",
        "generator_scorer_heartbeats",
        "dueling_work_kinds IS NULL "
        "OR (jsonb_typeof(dueling_work_kinds) = 'array' "
        "AND jsonb_array_length(dueling_work_kinds) BETWEEN 1 AND 8)",
    )
    op.drop_constraint(
        "generator_scorer_heartbeats_poll_check",
        "generator_scorer_heartbeats",
        type_="check",
    )
    op.create_check_constraint(
        "generator_scorer_heartbeats_poll_check",
        "generator_scorer_heartbeats",
        "last_poll IN ('qualification', 'round', 'dueling')",
    )

    op.create_table(
        "dueling_seasons",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("bench_version", sa.Integer(), nullable=False),
        sa.Column("season_index", sa.BigInteger(), nullable=False),
        sa.Column("shadow", sa.Boolean(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("commitment", sa.Text(), nullable=False),
        sa.Column("secret", sa.LargeBinary(), nullable=False),
        sa.Column("committed_at", _tz(), nullable=False),
        sa.Column("rounds_per_season", sa.Integer(), nullable=False),
        sa.Column("settings_revision", sa.Integer()),
        sa.Column("anchor_block", sa.BigInteger()),
        sa.Column("anchor_block_hash", sa.Text()),
        sa.Column("season_seed", sa.Text()),
        sa.Column("columns", _json()),
        sa.Column("opened_at", _tz()),
        sa.Column("closes_at_round", sa.BigInteger()),
        sa.Column("closed_at", _tz()),
        sa.Column("close_epoch_index", sa.BigInteger()),
        sa.Column("reveal_at_epoch", sa.BigInteger()),
        sa.Column("revealed_at", _tz()),
        sa.Column("void_reason", sa.Text()),
        sa.Column("detail", _json()),
        sa.Column("rotation_requested_at", _tz()),
        sa.Column("rotation_immediate", sa.Boolean()),
        sa.Column("rotation_actor", sa.Text()),
        sa.Column("rotation_reason", sa.Text()),
        sa.Column("created_at", _tz(), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint(
            "bench_version", "season_index", name="dueling_seasons_index_key"
        ),
        sa.CheckConstraint("bench_version >= 16", name="dueling_seasons_bench_floor"),
        sa.CheckConstraint(
            "status IN ('committed', 'open', 'closed', 'revealed', 'void')",
            name="dueling_seasons_status_check",
        ),
        sa.CheckConstraint(
            "commitment ~ '^[0-9a-f]{64}$' AND octet_length(secret) = 32",
            name="dueling_seasons_commitment_check",
        ),
        sa.CheckConstraint(
            "season_index >= 0 AND rounds_per_season BETWEEN 1 AND 48",
            name="dueling_seasons_index_check",
        ),
        sa.CheckConstraint(
            "(status <> 'committed' OR season_seed IS NULL) AND "
            "(season_seed IS NOT NULL OR status IN ('committed', 'void'))",
            name="dueling_seasons_seed_check",
        ),
        sa.CheckConstraint(
            "season_seed IS NULL OR (season_seed ~ '^[0-9a-f]{64}$' AND "
            "anchor_block_hash IS NOT NULL AND columns IS NOT NULL)",
            name="dueling_seasons_open_check",
        ),
        sa.CheckConstraint(
            "(status = 'void') = (void_reason IS NOT NULL)",
            name="dueling_seasons_void_check",
        ),
        sa.CheckConstraint(
            "(status = 'revealed') = (revealed_at IS NOT NULL)",
            name="dueling_seasons_reveal_check",
        ),
        sa.CheckConstraint(
            "(rotation_requested_at IS NULL) = (rotation_reason IS NULL) AND "
            "(rotation_requested_at IS NULL) = (rotation_actor IS NULL)",
            name="dueling_seasons_rotation_check",
        ),
    )
    op.create_index(
        "dueling_seasons_one_open_idx",
        "dueling_seasons",
        ["bench_version"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )
    op.create_index(
        "dueling_seasons_one_committed_idx",
        "dueling_seasons",
        ["bench_version"],
        unique=True,
        postgresql_where=sa.text("status = 'committed'"),
    )
    op.create_index(
        "dueling_seasons_status_idx", "dueling_seasons", ["status", "season_index"]
    )

    op.create_table(
        "dueling_season_batteries",
        sa.Column("season_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("submission_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("miner_hotkey", sa.Text(), nullable=False),
        sa.Column("owner_key", sa.Text(), nullable=False),
        sa.Column("source_sha256", sa.Text(), nullable=False),
        sa.Column("tree_sha256", sa.Text(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("evidence", _json()),
        sa.Column("seeds", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("lease_id", postgresql.UUID(as_uuid=True)),
        sa.Column("lease_expires_at", _tz()),
        sa.Column(
            "attempts", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("grading_bench_version", sa.Integer()),
        sa.Column("error", sa.Text()),
        sa.Column("scorer_version", sa.Text()),
        sa.Column("created_at", _tz(), nullable=False, server_default=sa.text("now()")),
        sa.Column("finished_at", _tz()),
        sa.ForeignKeyConstraint(
            ["season_id"], ["dueling_seasons.id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'leased', 'passed', 'failed')",
            name="dueling_season_batteries_status_check",
        ),
        sa.CheckConstraint(
            "(lease_id IS NULL) = (lease_expires_at IS NULL) AND "
            "(status <> 'leased' OR lease_id IS NOT NULL)",
            name="dueling_season_batteries_lease_check",
        ),
        sa.CheckConstraint(
            "status <> 'passed' OR grading_bench_version >= 15",
            name="dueling_season_batteries_policy_check",
        ),
        sa.CheckConstraint(
            "attempts >= 0", name="dueling_season_batteries_attempts_check"
        ),
    )
    op.create_index(
        "dueling_season_batteries_status_idx",
        "dueling_season_batteries",
        ["status", "season_id"],
    )

    op.create_table(
        "dueling_slice_preparations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("season_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_seed", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("lease_id", postgresql.UUID(as_uuid=True)),
        sa.Column("lease_expires_at", _tz()),
        sa.Column(
            "attempts", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("slice_sha256", sa.Text()),
        sa.Column("slice_bytes", sa.LargeBinary()),
        sa.Column("columns", _json()),
        sa.Column("error", sa.Text()),
        sa.Column("scorer_version", sa.Text()),
        sa.Column("requested_by", sa.Text()),
        sa.Column("created_at", _tz(), nullable=False, server_default=sa.text("now()")),
        sa.Column("first_leased_at", _tz()),
        sa.Column("ready_at", _tz()),
        sa.Column("failed_at", _tz()),
        sa.Column("bytes_pruned_at", _tz()),
        sa.UniqueConstraint(
            "season_id", "run_seed", name="dueling_slice_preparations_seed_key"
        ),
        sa.ForeignKeyConstraint(
            ["season_id"], ["dueling_seasons.id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "run_seed >= 0", name="dueling_slice_preparations_seed_check"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'leased', 'ready', 'failed')",
            name="dueling_slice_preparations_status_check",
        ),
        sa.CheckConstraint(
            "(lease_id IS NULL) = (lease_expires_at IS NULL) AND "
            "(status <> 'leased' OR lease_id IS NOT NULL)",
            name="dueling_slice_preparations_lease_check",
        ),
        sa.CheckConstraint(
            "(status = 'ready') = (slice_sha256 IS NOT NULL) AND "
            "(status <> 'ready' OR slice_bytes IS NOT NULL "
            "OR bytes_pruned_at IS NOT NULL)",
            name="dueling_slice_preparations_ready_check",
        ),
        sa.CheckConstraint(
            "slice_sha256 IS NULL OR slice_sha256 ~ '^[0-9a-f]{64}$'",
            name="dueling_slice_preparations_digest_check",
        ),
        sa.CheckConstraint(
            "slice_bytes IS NULL OR (octet_length(slice_bytes) BETWEEN 1 AND "
            "33554432 AND slice_sha256 = encode(sha256(slice_bytes), 'hex'))",
            name="dueling_slice_preparations_bytes_check",
        ),
        sa.CheckConstraint(
            "attempts >= 0", name="dueling_slice_preparations_attempts_check"
        ),
    )
    op.create_index(
        "dueling_slice_preparations_status_idx",
        "dueling_slice_preparations",
        ["status", "created_at"],
    )


def downgrade() -> None:
    op.execute(
        "UPDATE generator_scorer_heartbeats SET last_poll = 'round' "
        "WHERE last_poll = 'dueling'"
    )
    op.drop_constraint(
        "generator_scorer_heartbeats_poll_check",
        "generator_scorer_heartbeats",
        type_="check",
    )
    op.create_check_constraint(
        "generator_scorer_heartbeats_poll_check",
        "generator_scorer_heartbeats",
        "last_poll IN ('qualification', 'round')",
    )
    op.drop_constraint(
        "generator_scorer_heartbeats_dueling_kinds_check",
        "generator_scorer_heartbeats",
        type_="check",
    )
    op.drop_column("generator_scorer_heartbeats", "dueling_work_kinds")
    op.drop_table("dueling_slice_preparations")
    op.drop_table("dueling_season_batteries")
    op.drop_table("dueling_seasons")
    op.execute(
        "ALTER TABLE scores DROP CONSTRAINT IF EXISTS "
        "ck_scores_scores_dueling_bench_floor"
    )
    safe_drop_column("confirmation_scores", "dueling")
    safe_drop_column("confirmation_scores", "dueling_season_id")
    safe_drop_column("scores", "dueling")
    safe_drop_column("scores", "dueling_season_id")
    safe_drop_column("validator_tickets", "slice_sha256")
    safe_drop_column("validator_tickets", "dueling_season_id")
