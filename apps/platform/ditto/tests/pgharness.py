"""Real-Postgres provisioning for the test suite.

The whole suite runs against a real Postgres, because the bugs that reach
production are concurrency and locking bugs and SQLite cannot model them:
no concurrent writers, no ``SELECT ... FOR UPDATE``, no
``pg_advisory_xact_lock``. A SQLite suite does not fail on those bugs, it
*passes* on them -- see ditto-platform#438, where the identity map served
pre-lock attribute values and three concurrent reservations recorded
``request_count=1``.

Layout, mirrored from ``ditto-assistant/backend``'s ``pkg/testutil/testdb.go``:

* one **ambient** container, started on demand and never torn down, so the
  second ``pytest`` of the day pays no container cost;
* one **template** database, migrated once with the real Alembic chain and
  reused across runs while the migration set is unchanged;
* one database **per xdist worker**, cloned from the template with
  ``CREATE DATABASE ... TEMPLATE`` (~0.1s, versus ~2s to re-migrate).

The per-worker database is what removes the ``DeadlockDetectedError`` class.
Today every integration test truncates shared tables (``ACCESS EXCLUSIVE``)
on one shared database while sibling workers hold row locks on those same
tables in the opposite order -- a textbook lock-order inversion. When the
unit of database ownership equals the unit of parallelism there is nothing
left to invert.

Environment:

``DITTO_TEST_POSTGRES_URI``
    Admin DSN of an already-running Postgres (a CI service container). When
    set, no Docker command is ever issued. The harness still creates its own
    template and per-worker databases from it, so the DSN must name a
    superuser-ish role that may ``CREATE DATABASE``.
``DITTO_REQUIRE_POSTGRES``
    ``1`` turns any provisioning failure into a hard error instead of a skip.
    CI must set this: a silently-skipped database suite is exactly the
    green-on-broken failure mode this migration exists to delete.
``DITTO_TEST_KEEP_DATABASES``
    ``1`` leaves per-worker databases behind for post-mortem inspection.
``DITTO_TEST_POSTGRES_PORT`` / ``_CONTAINER`` / ``_IMAGE``
    Override the ambient container's host port, name, and image.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import os
import subprocess
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import asyncpg

# ─── Ambient container ───────────────────────────────────────────────────────

CONTAINER_NAME = os.environ.get(
    "DITTO_TEST_POSTGRES_CONTAINER", "ditto-platform-test-postgres"
)
"""Long-lived, named container. Never torn down by the test run."""

CONTAINER_IMAGE = os.environ.get("DITTO_TEST_POSTGRES_IMAGE", "postgres:16-alpine")
"""Matches ``docker-compose.yml``'s ``postgres`` service, so the test
database is the same engine and major version as dev and production."""

HOST_PORT = int(os.environ.get("DITTO_TEST_POSTGRES_PORT", "15433"))
"""Deliberately not 5432 and not 55432 (the compose dev database), so a
test run can never be pointed at, or confused with, a database holding
data anybody cares about."""

ADMIN_USER = "ditto_test"
ADMIN_PASSWORD = "ditto_test"
ADMIN_DB = "postgres"
"""Maintenance database. Per-run databases are created from a connection
to this one; nothing test-owned ever lives here."""

TEMPLATE_DB = "ditto_test_template"
DB_PREFIX = "ditto_test_"

_TEMPLATE_LOCK_KEY = 0x4454_5F54_454D_504C  # "DT_TEMPL"
"""Advisory lock serialising template build and clone across xdist workers
and across concurrent pytest invocations on the same machine."""

_ORPHAN_MAX_AGE_SECONDS = 6 * 60 * 60
"""Per-worker databases older than this with no live connections are reaped
at session start. The Go harness never drops anything and had ~230 orphaned
databases / ~4 GB live in its container; this is the fix for that."""

_REPO_ROOT = Path(__file__).resolve().parents[2]


class PostgresUnavailable(RuntimeError):
    """Raised when a real Postgres could not be provisioned."""


@dataclass(frozen=True)
class Dsn:
    """A resolved connection target, renderable for asyncpg or SQLAlchemy."""

    host: str
    port: int
    user: str
    password: str
    database: str

    @property
    def asyncpg(self) -> str:
        return (
            f"postgresql://{quote(self.user, safe='')}:"
            f"{quote(self.password, safe='')}@{self.host}:{self.port}/"
            f"{quote(self.database, safe='')}"
        )

    @property
    def sqlalchemy(self) -> str:
        return f"postgresql+asyncpg://{self.asyncpg.split('://', 1)[1]}"

    def with_database(self, database: str) -> Dsn:
        return Dsn(self.host, self.port, self.user, self.password, database)

    @property
    def env(self) -> dict[str, str]:
        """``POSTGRES_*`` mapping accepted by :func:`parse_postgres_config_from_env`.

        Exporting this is what lets the eleven integration files that call
        ``create_db_engine()`` inline -- with no fixture at all -- land on the
        per-worker database without a single edit.
        """
        return {
            "POSTGRES_HOST": self.host,
            "POSTGRES_PORT": str(self.port),
            "POSTGRES_USER": self.user,
            "POSTGRES_PASSWORD": self.password,
            "POSTGRES_DB": self.database,
        }


def _require() -> bool:
    return os.environ.get("DITTO_REQUIRE_POSTGRES", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _docker(*args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        check=check,
        timeout=180,
    )


def _container_state() -> str:
    """``running`` / ``exited`` / ``""`` when the container does not exist."""
    proc = _docker("inspect", "--format", "{{.State.Status}}", CONTAINER_NAME)
    return proc.stdout.strip() if proc.returncode == 0 else ""


@contextlib.contextmanager
def _startup_lock() -> Iterator[None]:
    """Serialise container startup across every worker on this machine.

    ``pytest -n auto`` fans out before anything has touched Docker, so on a
    cold start every worker races to ``docker run`` the same container name.
    One wins and the rest get ``Conflict. The container name ... is already
    in use``. A file lock makes exactly one worker do the work while the
    others wait and then find it already up.
    """
    path = Path(tempfile.gettempdir()) / f"{CONTAINER_NAME}.startup.lock"
    handle = path.open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def _start_container() -> None:
    """Bring the ambient container up, creating it only if it is absent.

    Durability is disabled: this database is rebuilt from migrations on
    demand, so a crash costing us its contents costs nothing, and the write
    path gets materially faster.
    """
    state = _container_state()
    if state == "running":
        return
    if state:
        _docker("start", CONTAINER_NAME)
        return
    proc = _docker(
        "run",
        "-d",
        "--name",
        CONTAINER_NAME,
        "-e",
        f"POSTGRES_USER={ADMIN_USER}",
        "-e",
        f"POSTGRES_PASSWORD={ADMIN_PASSWORD}",
        "-e",
        f"POSTGRES_DB={ADMIN_DB}",
        "-p",
        f"127.0.0.1:{HOST_PORT}:5432",
        CONTAINER_IMAGE,
        "-c",
        "fsync=off",
        "-c",
        "synchronous_commit=off",
        "-c",
        "full_page_writes=off",
        "-c",
        "max_connections=300",
    )
    if proc.returncode != 0:
        # A racing worker may have created it a moment ago. Existing in any
        # state is enough -- the caller polls for readiness -- so only a
        # genuinely absent container is a failure.
        if _container_state():
            _docker("start", CONTAINER_NAME)
            return
        raise PostgresUnavailable(
            f"could not start {CONTAINER_NAME}: {proc.stderr.strip()}"
        )


async def _await_ready(dsn: Dsn, *, attempts: int = 60) -> None:
    last: Exception | None = None
    for _ in range(attempts):
        try:
            conn = await asyncpg.connect(dsn.asyncpg, timeout=5)
        except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover - timing
            last = exc
            await asyncio.sleep(0.5)
            continue
        await conn.close()
        return
    raise PostgresUnavailable(
        f"postgres at {dsn.host}:{dsn.port} never came up: {last}"
    )


def resolve_admin_dsn() -> Dsn:
    """Resolve, and if necessary provision, an admin connection target.

    ``DITTO_TEST_POSTGRES_URI`` short-circuits Docker entirely; that is the
    CI path, where a service container already exists.
    """
    override = os.environ.get("DITTO_TEST_POSTGRES_URI") or os.environ.get(
        "TEST_POSTGRES_URI"
    )
    if override:
        parsed = _parse_uri(override)
        asyncio.run(_await_ready(parsed, attempts=20))
        return parsed

    dsn = Dsn("127.0.0.1", HOST_PORT, ADMIN_USER, ADMIN_PASSWORD, ADMIN_DB)
    try:
        asyncio.run(_await_ready(dsn, attempts=1))
        return dsn
    except PostgresUnavailable:
        pass
    with _startup_lock():
        # Re-check under the lock: by the time we got it, the worker that
        # held it first has usually finished the whole job.
        try:
            asyncio.run(_await_ready(dsn, attempts=1))
            return dsn
        except PostgresUnavailable:
            pass
        _start_container()
        asyncio.run(_await_ready(dsn))
    return dsn


def _parse_uri(uri: str) -> Dsn:
    from urllib.parse import unquote, urlsplit

    parts = urlsplit(uri)
    if not parts.hostname:
        raise PostgresUnavailable(f"unparseable postgres URI: {uri!r}")
    return Dsn(
        host=parts.hostname,
        port=parts.port or 5432,
        user=unquote(parts.username or "postgres"),
        password=unquote(parts.password or ""),
        database=(parts.path or "/postgres").lstrip("/") or "postgres",
    )


# ─── Template database ───────────────────────────────────────────────────────


def migration_fingerprint() -> str:
    """Content hash of the Alembic chain.

    Keyed on file *contents*, not on the head revision id, so editing a
    migration in place still invalidates the template. Cheap insurance
    against the classic "I amended the migration and the tests kept the old
    schema" afternoon.
    """
    digest = hashlib.sha256()
    versions = _REPO_ROOT / "alembic" / "versions"
    for path in sorted(versions.glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    for extra in ("alembic/env.py", "alembic.ini"):
        candidate = _REPO_ROOT / extra
        if candidate.exists():
            digest.update(candidate.read_bytes())
    return digest.hexdigest()


def _run_alembic_upgrade(target: Dsn, revision: str = "head") -> None:
    """Apply the real migration chain -- not ``Base.metadata.create_all``.

    ``create_all`` builds the schema the models *claim*; only Alembic builds
    the schema production actually has. Running the real chain is what closes
    the models-vs-migrations drift class, which no test looks at today.

    ``alembic/env.py`` reads ``POSTGRES_*`` and calls ``asyncio.run`` itself,
    so this must be invoked from synchronous context with no running loop.
    """
    from alembic.config import Config

    from alembic import command

    previous = {k: os.environ.get(k) for k in target.env}
    os.environ.update(target.env)
    try:
        cfg = Config(str(_REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
        command.upgrade(cfg, revision)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


async def _terminate_backends(conn: asyncpg.Connection, database: str) -> None:
    await conn.execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE datname = $1 AND pid <> pg_backend_pid()",
        database,
    )


async def _drop_database(conn: asyncpg.Connection, database: str) -> None:
    await _terminate_backends(conn, database)
    await conn.execute(f'DROP DATABASE IF EXISTS "{database}"')


async def _database_comment(conn: asyncpg.Connection, database: str) -> str | None:
    return await conn.fetchval(
        "SELECT shobj_description(oid, 'pg_database') FROM pg_database "
        "WHERE datname = $1",
        database,
    )


async def _reap_orphans(conn: asyncpg.Connection, *, keep: set[str]) -> int:
    """Drop stale per-worker databases left by crashed or killed runs.

    Creation epoch is stashed in the database comment, so age is knowable
    without a catalog column for it. Databases with live backends are never
    touched -- a concurrent run idling between tests must survive.
    """
    now = time.time()
    dropped = 0
    rows = await conn.fetch(
        "SELECT d.datname, shobj_description(d.oid, 'pg_database') AS note, "
        "  (SELECT count(*) FROM pg_stat_activity a WHERE a.datname = d.datname) "
        "    AS backends "
        "FROM pg_database d WHERE d.datname LIKE $1 AND d.datname <> $2",
        f"{DB_PREFIX}%",
        TEMPLATE_DB,
    )
    for row in rows:
        if row["datname"] in keep or row["backends"]:
            continue
        try:
            created = float((row["note"] or "").split("=", 1)[1])
        except (IndexError, ValueError):
            created = 0.0
        if now - created < _ORPHAN_MAX_AGE_SECONDS:
            continue
        with contextlib.suppress(asyncpg.PostgresError):
            await _drop_database(conn, row["datname"])
            dropped += 1
    return dropped


async def _ensure_template(admin: Dsn, fingerprint: str) -> None:
    """Build the template, or confirm the existing one is current.

    The advisory lock is held across the *whole* operation -- check, drop,
    create, migrate, stamp -- and released only at the end. Releasing it
    before the migration ran would leave a window in which the template
    exists but is empty and unstamped, and every other worker would look at
    it, decide it is stale, and drop it out from under the migration in
    progress. Cloning during that window yields a half-migrated database.

    The fingerprint is stamped **last**, so an interrupted build leaves an
    unstamped template that the next run correctly treats as garbage.
    """
    conn = await asyncpg.connect(admin.asyncpg)
    try:
        await conn.execute("SELECT pg_advisory_lock($1)", _TEMPLATE_LOCK_KEY)
        try:
            current = await _database_comment(conn, TEMPLATE_DB)
            exists = await conn.fetchval(
                "SELECT 1 FROM pg_database WHERE datname = $1", TEMPLATE_DB
            )
            if exists and current == f"fingerprint={fingerprint}":
                return
            await _drop_database(conn, TEMPLATE_DB)
            await conn.execute(f'CREATE DATABASE "{TEMPLATE_DB}"')
            # alembic/env.py calls asyncio.run itself, so it cannot run on
            # this loop. A worker thread gets its own; this connection just
            # sits idle holding the lock while it works.
            await asyncio.get_running_loop().run_in_executor(
                None, _run_alembic_upgrade, admin.with_database(TEMPLATE_DB)
            )
            await conn.execute(
                f"COMMENT ON DATABASE \"{TEMPLATE_DB}\" IS 'fingerprint={fingerprint}'"
            )
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _TEMPLATE_LOCK_KEY)
    finally:
        await conn.close()


async def _clone(admin: Dsn, database: str) -> None:
    conn = await asyncpg.connect(admin.asyncpg)
    try:
        await conn.execute("SELECT pg_advisory_lock($1)", _TEMPLATE_LOCK_KEY)
        try:
            await _drop_database(conn, database)
            await _terminate_backends(conn, TEMPLATE_DB)
            await conn.execute(f'CREATE DATABASE "{database}" TEMPLATE "{TEMPLATE_DB}"')
            await conn.execute(
                f"COMMENT ON DATABASE \"{database}\" IS 'created={time.time():.0f}'"
            )
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _TEMPLATE_LOCK_KEY)
    finally:
        await conn.close()


@dataclass(frozen=True)
class WorkerDatabase:
    """A migrated database owned exclusively by one xdist worker."""

    dsn: Dsn
    reset_sql: str
    """Pre-computed from the *pristine* clone, before any test could write
    to it, so the seed rows it restores are exactly the migration chain's
    own output."""


def provision_worker_database(admin: Dsn, database: str) -> WorkerDatabase:
    """Build (or reuse) the template, then clone it into ``database``.

    Synchronous on purpose. Provisioning owns its own short-lived event loop
    so it composes with pytest-asyncio's function-scoped test loops instead
    of fighting them.
    """
    result: dict[str, str] = {}

    async def _run() -> None:
        await _ensure_template(admin, migration_fingerprint())
        conn = await asyncpg.connect(admin.asyncpg)
        try:
            await _reap_orphans(conn, keep={database})
        finally:
            await conn.close()
        await _clone(admin, database)
        conn = await asyncpg.connect(admin.with_database(database).asyncpg)
        try:
            result["reset"] = await _build_reset_sql(conn)
        finally:
            await conn.close()

    asyncio.run(_run())
    return WorkerDatabase(admin.with_database(database), result["reset"])


def drop_worker_database(admin: Dsn, database: str) -> None:
    """Drop a per-worker database. Honours ``DITTO_TEST_KEEP_DATABASES``."""
    if os.environ.get("DITTO_TEST_KEEP_DATABASES", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }:
        return

    async def _run() -> None:
        conn = await asyncpg.connect(admin.asyncpg)
        try:
            await _drop_database(conn, database)
        finally:
            await conn.close()

    with contextlib.suppress(Exception):
        asyncio.run(_run())


# ─── Per-test reset ──────────────────────────────────────────────────────────

SCHEMA_TABLE = "alembic_version"
"""Never truncated: it records which schema the database is on."""


async def _build_reset_sql(conn: asyncpg.Connection) -> str:
    """Compose the one statement that restores a pristine, post-migration DB.

    Two things have to be true after it runs, and only one of them is
    obvious:

    1. every table a test could have written is empty; and
    2. every row the *migration chain itself* seeded is back.

    (2) matters because the migrations plant real defaults --
    ``artifact_release_settings_revisions`` and
    ``submission_settings_revisions`` both ship a genesis revision. Under
    SQLite's ``create_all`` those tables were simply empty, so tests have
    been running against a baseline production never has. Restoring the
    seed rather than merely truncating is what makes the test database
    equal production instead of equal to the old fiction.

    Emitted as a single ``DO`` block: one round trip, and atomic.

    Tables are discovered from ``pg_tables``. The Go harness enumerates ~60
    by hand; a table added later then silently bleeds across tests until
    somebody notices. The dynamic form benchmarks identically, so the
    hand-written list buys nothing and costs correctness.
    """
    tables = [
        row["tablename"]
        for row in await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "ORDER BY tablename"
        )
        if row["tablename"] != SCHEMA_TABLE
    ]
    if not tables:  # pragma: no cover - means the template never migrated
        raise PostgresUnavailable("no public tables found; template is not migrated")

    body = [
        "  TRUNCATE "
        + ", ".join(f'public."{name}"' for name in tables)
        + " RESTART IDENTITY CASCADE;"
    ]
    for name in tables:
        seed = await conn.fetchval(
            f'SELECT json_agg(t)::text FROM public."{name}" t'  # noqa: S608
        )
        if seed is None:
            continue
        body.append(
            f'  INSERT INTO public."{name}" SELECT * FROM '
            f'json_populate_recordset(NULL::public."{name}", '
            f"$ditto_seed${seed}$ditto_seed$::json);"
        )
        # Re-inserted rows carry their original ids, so any identity column's
        # sequence -- just reset to 1 by RESTART IDENTITY -- must be pushed
        # back past them or the next insert collides with a seeded row.
        for col in await conn.fetch(
            "SELECT a.attname AS col, "
            "       pg_get_serial_sequence($1, a.attname) AS seq "
            "  FROM pg_attribute a "
            " WHERE a.attrelid = $1::regclass AND a.attnum > 0 "
            "   AND NOT a.attisdropped "
            "   AND pg_get_serial_sequence($1, a.attname) IS NOT NULL",
            f'public."{name}"',
        ):
            body.append(
                f"  PERFORM setval('{col['seq']}', "
                f'(SELECT max("{col["col"]}") FROM public."{name}"));'
            )

    return "DO $ditto_reset$\nBEGIN\n" + "\n".join(body) + "\nEND\n$ditto_reset$"


async def reset_database(conn: Any, reset_sql: str) -> None:
    """Return the database to its pristine post-migration state.

    Called *before* each test, never after: a crashed test then cannot
    poison its successor, and its rows are still sitting there to look at.

    Driver-level on purpose. ``text()`` would read the ``:`` in the embedded
    seed JSON as bind-parameter syntax and reject the statement.
    """
    # Evict anything still attached to this worker's database before touching
    # it. A test that fails while it holds an open transaction can leave its
    # connection CHECKED OUT -- `engine.dispose()` returns pooled connections
    # but cannot force-close one the application never handed back, so the
    # locks it holds outlive the test that took them. The TRUNCATE below then
    # blocks on those locks forever: the whole run hangs, with no failing test
    # to point at and nothing in the output but a stalled progress bar.
    #
    # Each xdist worker owns its database exclusively (that is the entire point
    # of the per-worker clone), so any other backend on it at reset time is by
    # definition a straggler from a previous test and safe to terminate.
    await conn.exec_driver_sql(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE datname = current_database() AND pid <> pg_backend_pid()"
    )
    # Belt and braces: if something still cannot be evicted, FAIL rather than
    # hang. A reset that cannot take its locks in ten seconds is a bug that
    # deserves a traceback naming this function, not an infinite wait that
    # looks like a slow test.
    await conn.exec_driver_sql("SET LOCAL lock_timeout = '10s'")
    await conn.exec_driver_sql(reset_sql)
