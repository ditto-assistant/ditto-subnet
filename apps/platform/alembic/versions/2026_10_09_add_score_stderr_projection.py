"""Store exact stderr JSON beside the large score audit document.

Revision ID: 19fa2bb794d6
Revises: f4d95a6c827b

Add nullable/undefaulted metadata first, install the old-writer-safe trigger in
a separate short transaction, then backfill in small committed page ranges.
The backfill assigns an empty object; the trigger computes the actual value
once from details. Never hold an exclusive table lock across the backfill.
NULL rows remain readable through the canonical details fallback throughout
an interrupted migration. JSON null represents a known missing/non-object
stderr, so completed rows never need to open the audit blob again.
"""

from collections.abc import Sequence

from sqlalchemy import Connection

from alembic import op
from ditto.db.migration_lock import run_with_retry, safe_add_column, safe_drop_column

revision: str = "19fa2bb794d6"
down_revision: str | Sequence[str] | None = "f4d95a6c827b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _backfill_eligible(bind: Connection) -> None:
    # Retired-era NOT VALID constraints forbid updating historical <v7 rows.
    # Leave them byte-identical and on the canonical fallback.
    predicate = "stderr_projection IS NULL AND bench_version >= 7"
    pages = (
        int(
            bind.exec_driver_sql(
                "SELECT pg_relation_size('scores') / "
                "current_setting('block_size')::bigint"
            ).scalar_one()
        )
        + 1
    )
    for start in range(0, pages, 4):
        run_with_retry(
            bind,
            [
                "UPDATE scores SET stderr_projection = '{}'::jsonb "
                f"WHERE {predicate} AND ctid >= '({start},0)'::tid "
                f"AND ctid < '({start + 4},0)'::tid"
            ],
            "backfill score stderr page range",
        )
    # The trigger populates concurrent old-writer inserts/updates. Catch any
    # untouched rows moved beyond the original page range in small transactions.
    for _ in range(12):
        if not bind.exec_driver_sql(
            f"SELECT EXISTS (SELECT 1 FROM scores WHERE {predicate})"
        ).scalar_one():
            return
        run_with_retry(
            bind,
            [
                "UPDATE scores SET stderr_projection = '{}'::jsonb WHERE ctid IN "
                f"(SELECT ctid FROM scores WHERE {predicate} LIMIT 100)"
            ],
            "backfill score stderr residue",
        )
    raise RuntimeError("score stderr backfill needs retry; canonical fallback is safe")


def upgrade() -> None:
    safe_add_column("scores", "stderr_projection", "JSONB")
    with op.get_context().autocommit_block():
        run_with_retry(
            op.get_bind(),
            [
                """CREATE OR REPLACE FUNCTION scores_stderr_projection_refresh()
                RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    NEW.stderr_projection := jsonb_build_object(
                        'composite_stderr', NEW.details->'composite_stderr');
                    RETURN NEW;
                END
                $$""",
                "DROP TRIGGER IF EXISTS scores_stderr_projection_refresh ON scores",
                """CREATE TRIGGER scores_stderr_projection_refresh
                BEFORE INSERT OR UPDATE OF details, stderr_projection ON scores
                FOR EACH ROW EXECUTE FUNCTION scores_stderr_projection_refresh()""",
            ],
            "install scores stderr projection trigger",
        )
        _backfill_eligible(op.get_bind())


def downgrade() -> None:
    with op.get_context().autocommit_block():
        run_with_retry(
            op.get_bind(),
            ["DROP TRIGGER IF EXISTS scores_stderr_projection_refresh ON scores"],
            "remove scores stderr projection trigger",
        )
    safe_drop_column("scores", "stderr_projection")
    op.execute("DROP FUNCTION IF EXISTS scores_stderr_projection_refresh()")
