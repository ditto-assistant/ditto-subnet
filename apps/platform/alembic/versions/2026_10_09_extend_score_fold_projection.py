"""Extend the derived score projection with frozen confirmation fold fields.

Revision ID: 7d3ca1e6a290
Revises: 19fa2bb794d6

The existing nullable JSONB projection remains compatible with old readers and
writers. Preserve raw JSON values, including known missing/null values. Readers
fall back to canonical details unless every requested projection key exists.
"""

from collections.abc import Sequence

from sqlalchemy import Connection

from alembic import op
from ditto.db.migration_lock import run_with_retry

revision: str = "7d3ca1e6a290"
down_revision: str | Sequence[str] | None = "19fa2bb794d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _backfill_eligible(bind: Connection) -> None:
    predicate = (
        "bench_version >= 7 AND (stderr_projection IS NULL OR NOT "
        "(stderr_projection ?& ARRAY['composite_stderr', 'confirmation_seeds', "
        "'confirmation_composites']))"
    )
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
            "backfill score fold page range",
        )
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
            "backfill score fold residue",
        )
    raise RuntimeError("score fold backfill needs retry; canonical fallback is safe")


def upgrade() -> None:
    with op.get_context().autocommit_block():
        run_with_retry(
            op.get_bind(),
            [
                """CREATE OR REPLACE FUNCTION scores_stderr_projection_refresh()
                RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    NEW.stderr_projection := jsonb_build_object(
                        'composite_stderr', NEW.details->'composite_stderr',
                        'confirmation_seeds', NEW.details->'confirmation_seeds',
                        'confirmation_composites',
                        NEW.details->'confirmation_composites');
                    RETURN NEW;
                END
                $$"""
            ],
            "extend scores fold projection trigger",
        )
        _backfill_eligible(op.get_bind())


def downgrade() -> None:
    # Old readers ignore added keys. Do not rewrite rows on rollback; the
    # restored trigger derives the original shape on the next old-writer update.
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
                $$"""
            ],
            "restore scores stderr projection trigger",
        )
