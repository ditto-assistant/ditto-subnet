"""Alembic migration environment.

Connection URL is built at runtime from ``POSTGRES_*`` env vars;
nothing is baked into :file:`alembic.ini`. ``target_metadata`` is
wired to :data:`ditto.db.Base.metadata` so ``alembic revision
--autogenerate`` works.

Lock safety
-----------

Three guards here bound what a migration can do to live traffic. They exist
because ditto-platform#481 fixed one migration that deadlocked the deploy
twice, and the hazard is general: it recurs every time someone adds a column to
a hot table. :mod:`ditto.db.migration_lock` has the incident write-up and the
helpers migration authors should reach for; this file is the floor under
authors who do not.

1. **``lock_timeout`` on every migration**, set as an asyncpg *startup
   parameter* rather than a ``SET``. A startup parameter cannot be undone by a
   transaction rollback, so it still applies on the statement after a failure,
   which is exactly when it matters. It bounds lock *acquisition* only, never
   statement runtime, so it cannot truncate a long backfill.

2. **One transaction per migration**, so one migration's exclusive locks are
   released at its own commit instead of at the end of the batch.

3. **A bounded retry** on lock contention, which is what turns the short
   timeout in (1) from a new way to fail a deploy into a way to survive one.

Each is documented at its use site below, including what it costs.
"""

from __future__ import annotations

import asyncio
import logging
import os
from logging.config import fileConfig
from typing import TYPE_CHECKING

from sqlalchemy import exc, pool
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from ditto.db import Base
from ditto.db.migration_lock import LOCK_TIMEOUT, backoff_delay, is_retryable, sqlstate

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection


log = logging.getLogger("alembic.lock")

RUN_MAX_ATTEMPTS = 4
"""Replays of the whole pending batch on lock contention.

Far lower than the per-statement budget in :mod:`ditto.db.migration_lock`,
because a replay here re-runs an entire migration rather than one statement.
Four attempts is ~15s of backoff, enough to outlast the burst of inference
traffic that produced the contention without turning a genuinely stuck deploy
into a long silent hang.
"""


# Run logging config from alembic.ini (handlers + formatters).
if context.config.config_file_name is not None:
    fileConfig(context.config.config_file_name)


target_metadata = Base.metadata


def _db_url() -> str:
    """Build the async Postgres URL from ``POSTGRES_*`` env vars.

    Uses ``URL.create`` so credentials with reserved characters (``@``,
    ``:``, ``/``) survive serialisation. ``render_as_string`` with
    ``hide_password=False`` is required because plain ``str(URL)`` masks
    the password as ``***``, which then fails the asyncpg auth handshake.
    """
    return URL.create(
        "postgresql+asyncpg",
        username=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        database=os.environ["POSTGRES_DB"],
    ).render_as_string(hide_password=False)


def _do_run_migrations(connection: Connection) -> None:
    """Synchronous-side migration runner invoked from the async engine.

    ``transaction_per_migration`` is the lock-duration guard. Without it the
    whole pending batch runs in one transaction, so every exclusive lock any
    migration takes is held until the *last* one commits -- adding a slow
    migration to the end of the chain silently extends an earlier migration's
    ``AccessExclusiveLock`` on a hot table. That coupling is invisible at review
    time, since neither migration is wrong on its own.

    The cost is real and worth naming: a failure part-way through a batch no
    longer rolls the earlier migrations back, so a failed deploy can leave the
    schema part-advanced instead of untouched. Two things make that the better
    trade. It is *resumable* -- ``alembic_version`` moves per migration, so
    re-running continues from where it stopped rather than redoing the batch.
    And it is *already required to be safe*: this service deploys by migrating
    while the old build is still serving, so every migration must leave a schema
    the old build tolerates. A schema that is safe part-way through a rolling
    deploy is the same schema that is safe part-way through a batch. A migration
    for which that is not true is already unsafe here, batch transaction or not.

    Note that batch atomicity was never something the chain actually had:
    ``autocommit_block`` (used by any migration that backfills in batches)
    commits inside the batch transaction regardless.
    """
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    """Open an asyncpg-backed engine and apply migrations, retrying on locks.

    ``lock_timeout`` rides in as an asyncpg ``server_settings`` startup
    parameter rather than a ``SET``: a startup parameter is the session default
    and survives a transaction rollback, where a ``SET`` issued inside a
    transaction is reverted by one. The distinction matters precisely in the
    failure case -- after a contended statement rolls back, the retry must still
    be bounded.

    The retry is the other half of that timeout. On its own a short
    ``lock_timeout`` only converts "block until the deploy is killed" into "fail
    the deploy faster", which is not obviously an improvement; retrying is what
    makes it converge. ``40P01`` is retried alongside ``55P03`` because
    ``lock_timeout`` does not prevent deadlocks: the deadlock detector runs at
    ``deadlock_timeout`` (1s by default), so on a genuine lock cycle it fires
    first and the victim sees ``40P01`` long before a 3s ``lock_timeout`` could.

    **This is only sound for idempotent migrations, and only because of
    ``transaction_per_migration``.** A replay re-runs whatever did not commit.
    With one transaction per migration, a migration that fails inside its own
    transaction is fully rolled back -- PostgreSQL DDL is transactional -- so
    replaying it is clean and needs nothing from the author. A migration using
    ``autocommit_block`` has committed part of its work, and replaying it is
    safe only if each step is idempotent. That is the discipline
    :mod:`ditto.db.migration_lock` enforces by construction; a hand-rolled
    ``autocommit_block`` migration that is not idempotent must not rely on this
    retry.

    A fresh engine per attempt, because the connection that hit the error is in
    an aborted transaction and its session state is no longer trustworthy.
    """
    for attempt in range(1, RUN_MAX_ATTEMPTS + 1):
        engine = create_async_engine(
            _db_url(),
            poolclass=pool.NullPool,
            connect_args={"server_settings": {"lock_timeout": LOCK_TIMEOUT}},
        )
        try:
            async with engine.connect() as connection:
                await connection.run_sync(_do_run_migrations)
            return
        except exc.DBAPIError as error:
            if not is_retryable(error) or attempt == RUN_MAX_ATTEMPTS:
                raise
            delay = backoff_delay(attempt)
            log.warning(
                "migration run: %s on attempt %d/%d; retrying in %.1fs",
                sqlstate(error),
                attempt,
                RUN_MAX_ATTEMPTS,
                delay,
            )
            await asyncio.sleep(delay)
        finally:
            await engine.dispose()


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it against a live DB."""
    context.configure(
        url=_db_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against the live database via asyncpg."""
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
