"""scope the efficiency bonus to an epoch, not just a benchmark version

``efficiency_bonuses`` was keyed ``(agent_id, bench_version)`` with no epoch
column at all, while ``efficiency_cohort_snapshots`` -- the row it points at --
has always carried ``epoch_index``. That asymmetry made the bonus insert-once
per benchmark contract FOREVER: ``_materialize_epoch`` skips any agent already
present, so whatever epoch an agent was first measured in fixed its bonus for
the life of the version. An agent that became more efficient could never earn a
better one, and an agent measured mid-transition kept that measurement. The
latter happened at 04:43Z on 2026-07-26, part-way through the 4M -> 25M
token-budget change.

Bench-version scoping is correct and stays: a bonus earned under one benchmark
contract is meaningless under another. Epoch scoping goes *inside* it.

Recomputation stays additive. A new epoch inserts a NEW row; prior rows are
never updated or deleted, so ``/public/efficiency/snapshots/{id}`` remains
immutable and the mid-transition epoch stays visible as history rather than
needing a destructive cleanup.

Backfill takes each row's true epoch from the snapshot it already references,
so historical rows keep the epoch they were really assigned in rather than
collapsing to zero.

Lock safety: ``efficiency_bonuses`` is small and cold, but a primary-key swap
takes ACCESS EXCLUSIVE and ``inference_grants`` deadlocked a production deploy
twice on 2026-07-26 doing less than this (#481, #483). So the column add goes
through ``safe_add_column`` and the key swap runs under the same bounded
``lock_timeout`` + retry helper rather than an unbounded ``op`` call.

Revision ID: e8c31f4a76d2
Revises: b7e41c93a05d
Create Date: 2026-07-26
"""

from collections.abc import Sequence

from alembic import op
from ditto.db.migration_lock import run_with_retry, safe_add_column, safe_drop_column

revision: str = "e8c31f4a76d2"
down_revision: str | Sequence[str] | None = "b7e41c93a05d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Three-phase add: nullable, backfilled from the referenced snapshot's own
    # epoch, then pinned. The default is withheld until the end so a row written
    # by the still-running old build mid-backfill cannot silently claim epoch 0.
    safe_add_column(
        "efficiency_bonuses",
        "epoch_index",
        "BIGINT",
        backfill=(
            "(SELECT s.epoch_index FROM efficiency_cohort_snapshots s "
            "WHERE s.snapshot_id = efficiency_bonuses.snapshot_id)"
        ),
        not_null=True,
        server_default="0",
    )
    run_with_retry(
        op.get_bind(),
        [
            "ALTER TABLE efficiency_bonuses "
            "DROP CONSTRAINT IF EXISTS efficiency_bonuses_pkey",
            "ALTER TABLE efficiency_bonuses ADD CONSTRAINT efficiency_bonuses_pkey "
            "PRIMARY KEY (agent_id, bench_version, epoch_index)",
            # The doubled name is the metadata naming convention
            # ("ck_%(table_name)s_%(constraint_name)s") applied to a
            # constraint the model already names for its table, matching the
            # two CHECKs the create-table migration emitted.
            "ALTER TABLE efficiency_bonuses DROP CONSTRAINT IF EXISTS "
            "ck_efficiency_bonuses_efficiency_bonuses_epoch_index_check",
            "ALTER TABLE efficiency_bonuses ADD CONSTRAINT "
            "ck_efficiency_bonuses_efficiency_bonuses_epoch_index_check "
            "CHECK (epoch_index >= 0)",
            "CREATE INDEX IF NOT EXISTS efficiency_bonuses_epoch_idx "
            "ON efficiency_bonuses (bench_version, epoch_index)",
        ],
        "re-key efficiency_bonuses on (agent_id, bench_version, epoch_index)",
    )


def downgrade() -> None:
    # Collapsing back to (agent_id, bench_version) can only succeed when at most
    # one epoch per agent exists. Keep the newest row for each agent so the
    # narrower key is satisfiable; older epochs are history and are dropped with
    # the column that gave them meaning.
    run_with_retry(
        op.get_bind(),
        [
            "DELETE FROM efficiency_bonuses a USING efficiency_bonuses b "
            "WHERE a.agent_id = b.agent_id "
            "AND a.bench_version = b.bench_version "
            "AND a.epoch_index < b.epoch_index",
            "DROP INDEX IF EXISTS efficiency_bonuses_epoch_idx",
            "ALTER TABLE efficiency_bonuses DROP CONSTRAINT IF EXISTS "
            "ck_efficiency_bonuses_efficiency_bonuses_epoch_index_check",
            "ALTER TABLE efficiency_bonuses "
            "DROP CONSTRAINT IF EXISTS efficiency_bonuses_pkey",
            "ALTER TABLE efficiency_bonuses ADD CONSTRAINT efficiency_bonuses_pkey "
            "PRIMARY KEY (agent_id, bench_version)",
        ],
        "collapse efficiency_bonuses back to (agent_id, bench_version)",
    )
    safe_drop_column("efficiency_bonuses", "epoch_index")
