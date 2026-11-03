"""Local resets that pass the statement TRUNCATE guards on history tables.

Append-only history tables (CLA bindings, treasury runtime revisions) reject
TRUNCATE with a statement trigger. ``TRUNCATE ... CASCADE`` reaches them from
their parents, so a wipe of ``agents`` fails even when the history is empty.

The test harness and the local simulator own their whole database, so they
disable only the currently enabled guards, truncate, and restore each guard's
original enable mode in one atomic ``DO`` block. Runtime code must never call
this: production history is never truncated.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

TRUNCATE_GUARDS_SQL = (
    "SELECT c.relname AS table_name, t.tgname AS trigger_name, "
    "t.tgenabled::text AS enable_mode "
    "FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = 'public' AND NOT t.tgisinternal "
    "AND t.tgenabled IN ('O', 'A') AND (t.tgtype & 32) != 0 "
    "ORDER BY c.relname, t.tgname"
)
"""Enabled statement-level TRUNCATE triggers, with their enable mode."""


def truncate_past_guards_body(
    tables: Sequence[str],
    guards: Sequence[tuple[str, str, str]],
    *,
    restart_identity: bool = False,
) -> list[str]:
    """PL/pgSQL statements that truncate ``tables`` with guards held off."""

    def quoted(value: str) -> str:
        return '"' + value.replace('"', '""') + '"'

    body = [
        f"  ALTER TABLE public.{quoted(table)} DISABLE TRIGGER {quoted(trigger)};"
        for table, trigger, _mode in guards
    ]
    restart = " RESTART IDENTITY" if restart_identity else ""
    body.append(
        "  TRUNCATE "
        + ", ".join(f"public.{quoted(name)}" for name in tables)
        + restart
        + " CASCADE;"
    )
    body += [
        f"  ALTER TABLE public.{quoted(table)} ENABLE "
        f"{'ALWAYS ' if mode == 'A' else ''}TRIGGER {quoted(trigger)};"
        for table, trigger, mode in guards
    ]
    return body


async def truncate_past_guards(
    conn: AsyncConnection | AsyncSession,
    tables: Sequence[str],
    *,
    restart_identity: bool = False,
    lock_timeout_ms: int | None = None,
) -> None:
    """Truncate ``tables`` with CASCADE inside the caller's transaction.

    ``lock_timeout_ms`` is set inside the same statement, so it also bounds the
    truncate on an autocommit connection, where a prior SET LOCAL would not.
    """
    if not tables or any(
        not table.isascii() or not table.isidentifier() for table in tables
    ):
        raise ValueError("invalid table names")
    rows = (await conn.execute(text(TRUNCATE_GUARDS_SQL))).all()
    body = truncate_past_guards_body(
        tables,
        [(row[0], row[1], row[2]) for row in rows],
        restart_identity=restart_identity,
    )
    if lock_timeout_ms is not None:
        body.insert(
            0,
            f"  PERFORM set_config('lock_timeout', '{int(lock_timeout_ms)}ms', true);",
        )
    await conn.execute(
        text("DO $ditto_reset$\nBEGIN\n" + "\n".join(body) + "\nEND\n$ditto_reset$")
    )
