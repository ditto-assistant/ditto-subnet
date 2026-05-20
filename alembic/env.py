"""Alembic migration environment.

Reads ``POSTGRES_*`` env vars to build the connection URL at run time;
nothing is baked into :file:`alembic.ini`. ``target_metadata`` is
``None`` by design: this project does not use a SQLAlchemy ORM, so
``--autogenerate`` is intentionally unavailable and every migration is
written by hand. SQLAlchemy is only pulled in transitively by alembic
for the engine + connection scaffolding around our raw SQL.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig
from typing import TYPE_CHECKING

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection


# Run logging config from alembic.ini (handlers + formatters).
if context.config.config_file_name is not None:
    fileConfig(context.config.config_file_name)


# No ORM => no metadata to diff against => --autogenerate is intentionally
# unavailable. All migrations are hand-written.
target_metadata = None


def _db_url() -> str:
    """Build the async Postgres URL from ``POSTGRES_*`` env vars."""
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    name = os.environ["POSTGRES_DB"]
    return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{name}"


def _do_run_migrations(connection: Connection) -> None:
    """Synchronous-side migration runner invoked from the async engine."""
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    """Open an asyncpg-backed engine and apply migrations."""
    engine = create_async_engine(_db_url(), poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(_do_run_migrations)
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
