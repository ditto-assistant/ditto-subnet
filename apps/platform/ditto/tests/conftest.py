"""Root fixtures: every database test runs against a real Postgres.

The suite used to run on ``sqlite+aiosqlite:///:memory:``. That is not a
weaker test of the same code, it is a test of *different* code: SQLAlchemy
emits no ``FOR UPDATE`` on SQLite, ``pg_advisory_xact_lock`` has no SQLite
counterpart so fifteen call sites take a no-op branch, and SQLite has no
concurrent writers at all. ditto-platform#438 is the proof -- the identity
map served pre-lock attribute values and three concurrent reservations
recorded ``request_count=1``, silently overspending miner grants. No SQLite
test could have failed on that, and none did.

Fixture names are deliberately unchanged (``engine`` / ``session_maker`` /
``session``), so a test file migrates by *deleting* its local SQLite
fixtures rather than by being rewritten.

Isolation model
---------------
* one **database per xdist worker**, cloned from a once-migrated template;
* the database is reset to its pristine post-migration state **before**
  each test.

Per-worker databases are not a performance tweak, they are the fix for the
``DeadlockDetectedError`` the integration suite hits under ``-n auto``.
Sharing one database means worker A's ``TRUNCATE ... CASCADE``
(``ACCESS EXCLUSIVE`` on ``agents`` and everything referencing it) queues
behind worker B's ``FOR UPDATE`` row locks while B's next ``TRUNCATE``
queues behind A's -- a lock-order inversion guaranteed by the topology, not
an unlucky interleaving. When the unit of database ownership equals the unit
of parallelism there is nothing left to invert.

``POSTGRES_*`` is exported to point at the worker's own database, so the
integration files that call ``create_db_engine()`` inline -- with no fixture
at all -- become worker-isolated without being edited.

Environment
-----------
The database is not the only thing a fresh checkout used to be missing.
:func:`pytest_configure` seeds the rest of the required environment from
``ditto/tests/env_defaults.py`` before collection, so ``uv run pytest`` is
green on a clone with no ``.env`` and nothing exported. Those defaults are
test-only on purpose -- see that module for why production must keep failing
loudly instead.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from typing import TYPE_CHECKING

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ditto.tests import minioharness, pgharness
from ditto.tests.env_defaults import apply_test_env_defaults

if TYPE_CHECKING:
    from ditto.tests.pgharness import Dsn, WorkerDatabase


def pytest_configure(config: pytest.Config) -> None:
    """Seed the test-only environment defaults before anything is collected.

    A hook rather than an autouse fixture because fixtures run *after*
    collection, and module-level code in a test file is entitled to read the
    environment. Runs once per process, which under ``-n auto`` means once
    per xdist worker -- each worker is its own interpreter with its own
    ``os.environ``.
    """
    del config
    apply_test_env_defaults()


def _worker_id() -> str:
    """Identity of this xdist worker (``gw0``…), or ``main`` when serial."""
    return os.environ.get("PYTEST_XDIST_WORKER", "main")


def _run_id() -> str:
    """Identity of this *pytest invocation*.

    Without it, two concurrent runs on one machine -- a developer's suite and
    an agent's -- would both claim ``ditto_test_gw0`` and silently truncate
    each other's rows.
    """
    uid = os.environ.get("PYTEST_XDIST_TESTRUNUID")
    if uid:
        return uid[:8]
    return f"p{os.getppid()}"


@pytest.fixture(scope="session")
def postgres_admin_dsn() -> Dsn:
    """Admin connection target, provisioning the ambient container if needed.

    A provisioning failure is a **skip** on a laptop and a **hard error**
    under ``DITTO_REQUIRE_POSTGRES=1``. CI must set that variable: a database
    suite that turns green because the database was unreachable is precisely
    the failure mode this migration exists to delete.
    """
    try:
        return pgharness.resolve_admin_dsn()
    except Exception as exc:
        if pgharness._require():
            raise
        pytest.skip(f"real Postgres unavailable: {exc}")


@pytest.fixture(scope="session")
def worker_database(postgres_admin_dsn: Dsn) -> Iterator[WorkerDatabase]:
    """This worker's private, migrated database.

    Built by cloning a template that the real Alembic chain migrated once --
    not by ``Base.metadata.create_all``. ``create_all`` builds the schema the
    models *claim*; only Alembic builds the schema production has, so this is
    also the first test coverage the migration chain has ever had.
    """
    name = f"{pgharness.DB_PREFIX}{_run_id()}_{_worker_id()}"
    database = pgharness.provision_worker_database(postgres_admin_dsn, name)
    previous = {key: os.environ.get(key) for key in database.dsn.env}
    os.environ.update(database.dsn.env)
    try:
        yield database
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        pgharness.drop_worker_database(postgres_admin_dsn, name)


@pytest.fixture(scope="session")
def object_storage() -> Iterator[None]:
    """A real S3 endpoint with the agents bucket, provisioned on demand.

    The storage counterpart of :func:`worker_database`, and the same
    argument: the four ``integration`` upload tests build their config from
    the environment and stream real bytes, so they need a store that
    actually stores. Provisioning it here is what removes ``make stack-up``
    from the list of things a fresh clone has to know about.

    Unlike the database there is no per-worker isolation, because there is
    nothing to isolate: object keys are UUID-prefixed, so two workers cannot
    collide, and no test lists the bucket.

    A provisioning failure is a **skip** on a laptop and a **hard error**
    under ``DITTO_REQUIRE_OBJECT_STORAGE=1``.
    """
    try:
        resolved = minioharness.resolve_storage_env()
    except Exception as exc:
        if minioharness._require():
            raise
        pytest.skip(f"real object storage unavailable: {exc}")
    previous = {key: os.environ.get(key) for key in resolved}
    os.environ.update(resolved)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture
async def engine(worker_database: WorkerDatabase) -> AsyncIterator[AsyncEngine]:
    """Per-test engine over this worker's database, reset before the test.

    Function-scoped on purpose. An ``AsyncEngine``'s pool is bound to the
    event loop that used it, and pytest-asyncio gives each test its own loop,
    so a session-scoped engine would hand the second test a pool full of
    connections belonging to a closed loop. Engine construction does no I/O;
    the real cost is the reset plus a connect, and both are milliseconds.

    Commits are **real commits**. There is no outer-transaction/rollback
    trick here, and there must not be one: you cannot observe two
    concurrently-committing transactions from inside a single transaction, so
    a rollback-isolated fixture would quietly re-hide exactly the class of bug
    (#438) that motivated this migration. If the suite ever needs a faster
    tier, it belongs alongside this fixture, not in place of it.
    """
    eng = create_async_engine(worker_database.dsn.sqlalchemy)
    try:
        async with eng.begin() as conn:
            await pgharness.reset_database(conn, worker_database.reset_sql)
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
def session_maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Session factory bound to this test's engine."""
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def session(
    session_maker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """One :class:`AsyncSession` scoped to a single test function."""
    async with session_maker() as sess:
        yield sess
