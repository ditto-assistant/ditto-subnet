"""Canary: the test database is real, migrated, isolated, and drift-free.

Every assertion here protects a property the rest of the suite silently
assumes. If one of these fails, other failures elsewhere are probably
symptoms rather than causes.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ditto.db.models import Base
from ditto.tests import pgharness
from ditto.tests.pgharness import _build_reset_sql, truncate_test_tables


async def test_legacy_truncate_restarts_identity_like_the_full_reset(
    session_maker,
) -> None:
    insert = text(
        "INSERT INTO admin_activity (action, method, details, source) "
        "VALUES ('fixture', 'POST', '{}', 'test') RETURNING id"
    )
    async with session_maker() as session, session.begin():
        await session.execute(insert)
        await session.execute(insert)
        await truncate_test_tables(session, "admin_activity")
        # A truncated table is fresh: its identity starts over at 1.
        assert (await session.execute(insert)).scalar_one() == 1


@pytest.mark.parametrize("mode", ["O", "A"])
@pytest.mark.parametrize("reset", ["legacy", "template"])
async def test_reset_restores_original_truncate_guard_mode(
    engine: AsyncEngine, mode: str, reset: str
) -> None:
    enable = {
        "O": "ALTER TABLE agent_cla_bindings ENABLE TRIGGER "
        "agent_cla_bindings_no_truncate",
        "A": "ALTER TABLE agent_cla_bindings ENABLE ALWAYS TRIGGER "
        "agent_cla_bindings_no_truncate",
    }
    async with engine.connect() as conn, conn.begin():
        await conn.execute(text(enable[mode]))
        if reset == "legacy":
            await truncate_test_tables(conn, "agents")
            assert await conn.scalar(text("SHOW lock_timeout")) == "10s"
        else:
            raw = await conn.get_raw_connection()
            driver = raw.driver_connection
            assert driver is not None
            await driver.execute(await _build_reset_sql(driver))
        assert (
            await conn.scalar(
                text(
                    "SELECT tgenabled::text FROM pg_trigger "
                    "WHERE tgrelid = 'agent_cla_bindings'::regclass "
                    "AND tgname = 'agent_cla_bindings_no_truncate'"
                )
            )
            == mode
        )
        with pytest.raises(DBAPIError, match="append-only"):
            await conn.execute(text("TRUNCATE agent_cla_bindings"))
        await conn.rollback()


_CANARY_SCHEMA = "models_canary"

_CONSTRAINT_DEFS = (
    "SELECT c.relname, pg_get_constraintdef(con.oid) AS def "
    "FROM pg_constraint con "
    "JOIN pg_class c ON c.oid = con.conrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE con.contype = 'c' AND n.nspname = :schema"
)


async def _check_predicates(session: AsyncSession, schema: str) -> dict[str, list[str]]:
    """Every CHECK predicate in ``schema``, grouped by table.

    Keyed on the *predicate*, never the constraint name. Names legitimately
    differ between the two sides: SQLAlchemy's ``ck_%(table_name)s_%(...)s``
    naming convention prefixes what ``create_all`` emits, while migrations
    that wrote raw DDL, or that let Postgres auto-name an inline column
    CHECK, did not. Comparing names would drown the real drift in ~44
    cosmetic mismatches.

    ``NOT VALID`` is stripped for the same reason, and only for that reason. It
    is not part of the predicate: it says whether Postgres re-examined the rows
    that were already in the table when the constraint was added, and a
    constraint carrying it is enforced on every INSERT and UPDATE exactly like
    one that does not. The retired-era floor is declared that way on purpose --
    the historical v2-v6 rows must survive -- and ``CheckConstraint`` has no way
    to say so, so ``create_all`` can only ever emit the bare predicate. Comparing
    the raw strings would report the floor as permanent, unfixable drift and
    train the next reader to ignore this test.
    """
    rows = (await session.execute(text(_CONSTRAINT_DEFS), {"schema": schema})).all()
    grouped: dict[str, list[str]] = {}
    for table, definition in rows:
        grouped.setdefault(table, []).append(definition.removesuffix(" NOT VALID"))
    return {table: sorted(defs) for table, defs in grouped.items()}


async def test_models_declare_every_constraint_the_migrations_create(
    session: AsyncSession,
) -> None:
    """Guard against models-vs-migrations drift.

    Drift is invisible while tests build their schema from the models: the
    database enforces a rule production has and the tests never see it. That
    was not hypothetical -- ``screening_quarantines`` had three CHECKs
    (``manifest_digest``, ``finding_digest``, ``reason_code`` formats) and
    ``validator_tickets`` one (``seed >= 0``) that ``models.py`` did not
    declare, so no test had ever exercised them.

    **Predicates, not counts.** This started as a count ratchet, which cannot
    see a constraint that exists on both sides with a *weaker* rule -- and one
    did: ``screening_attempts_reason_code_check`` was the migration's
    ``reason_code ~ '^[a-z0-9][a-z0-9-]{0,63}$'`` but the model's
    ``length(reason_code) BETWEEN 1 AND 64``, so the suite accepted reason
    codes production rejects. Both sides are rendered by the same server here,
    via ``pg_get_constraintdef``, so the comparison needs no normalization and
    cannot be fooled by formatting.

    The models side is built into a throwaway schema by ``create_all``. Not
    ``str(CreateTable(...))``: compiled DDL text does not tell you what
    Postgres will actually store, which is the thing the migrated side is
    reporting.
    """
    await session.rollback()
    async with session.begin():
        await session.execute(text(f'DROP SCHEMA IF EXISTS "{_CANARY_SCHEMA}" CASCADE'))
        await session.execute(text(f'CREATE SCHEMA "{_CANARY_SCHEMA}"'))
    try:
        connection = await session.connection(
            execution_options={"schema_translate_map": {None: _CANARY_SCHEMA}}
        )
        await connection.run_sync(Base.metadata.create_all)
        await session.commit()

        migrated = await _check_predicates(session, "public")
        declared = await _check_predicates(session, _CANARY_SCHEMA)

        assert set(migrated) - set(declared) == set(), (
            "tables whose CHECKs the migrations create and models.py does not "
            "declare at all"
        )
        drift = {
            table: {
                "in the migrations, not on the model": sorted(
                    set(defs) - set(declared.get(table, ()))
                ),
                "on the model, not in the migrations": sorted(
                    set(declared.get(table, ())) - set(defs)
                ),
            }
            for table, defs in migrated.items()
            if set(defs) != set(declared.get(table, ()))
        }
        assert drift == {}, (
            f"models.py and the migrations disagree about CHECK constraints: "
            f"{drift}. The migration is what production actually has, so make "
            f"the model match it -- do not write a migration to match a stale "
            f"model."
        )
    finally:
        await session.rollback()
        async with session.begin():
            await session.execute(
                text(f'DROP SCHEMA IF EXISTS "{_CANARY_SCHEMA}" CASCADE')
            )


async def test_reset_restores_migration_seeded_rows(session: AsyncSession) -> None:
    """The migration chain plants real defaults; the reset must keep them.

    Under SQLite's ``create_all`` these tables were empty, so the suite ran
    against a baseline production never has.
    """
    seeded = await session.scalar(
        text("SELECT count(*) FROM artifact_release_settings_revisions")
    )
    assert seeded == 2, "migration-seeded artifact release revisions were lost"


async def test_reset_leaves_no_rows_from_the_previous_test(
    session: AsyncSession,
) -> None:
    """Paired with the writer below; order-independent by construction."""
    count = await session.scalar(text("SELECT count(*) FROM banned_hotkeys"))
    assert count == 0


async def test_reset_leaves_no_rows_from_the_previous_test_writer(
    session: AsyncSession,
) -> None:
    """Commits a row the sibling test above must never observe."""
    async with session.begin():
        await session.execute(
            text(
                "INSERT INTO banned_hotkeys (hotkey, reason, banned_at) "
                "VALUES ('canary', 'harness isolation canary', now())"
            )
        )
    assert await session.scalar(text("SELECT count(*) FROM banned_hotkeys")) == 1


async def test_agent_uniqueness_constraint_is_enforced(engine: AsyncEngine) -> None:
    """``agents_hotkey_name_version_key`` is unconditional.

    It used to be ``ddl_if(dialect='postgresql')`` and so did not exist under
    SQLite, where two agents sharing ``(miner_hotkey, name, version)``
    committed happily -- while ``ditto/db/queries/agents.py:139`` comments
    that "the UNIQUE constraint remains the final invariant on both". The
    migration has always created it unconditionally; the model now says so
    too, and it is genuinely final.
    """
    from datetime import UTC, datetime
    from uuid import uuid4

    from ditto.api_models.agent_status import AgentStatus
    from ditto.db.models import Agent

    def _agent() -> Agent:
        return Agent(
            agent_id=uuid4(),
            miner_hotkey="dup-hotkey",
            name="dup-name",
            version=1,
            sha256=uuid4().hex + uuid4().hex,
            status=AgentStatus.UPLOADED,
            created_at=datetime.now(UTC),
        )

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s, s.begin():
        s.add(_agent())
    with pytest.raises(IntegrityError):
        async with maker() as s, s.begin():
            s.add(_agent())


async def test_truncate_lock_timeout_holds_on_an_autocommit_connection(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pgharness, "TRUNCATE_LOCK_TIMEOUT_MS", 200)
    async with engine.connect() as holder, holder.begin():
        await holder.execute(text("LOCK TABLE agents IN ACCESS SHARE MODE"))
        autocommit = await engine.connect()
        try:
            await autocommit.execution_options(isolation_level="AUTOCOMMIT")
            # A SET LOCAL before the truncate would be lost here and the
            # truncate would wait on the holder indefinitely.
            with pytest.raises(DBAPIError, match="lock timeout"):
                await asyncio.wait_for(
                    truncate_test_tables(autocommit, "agents"), timeout=10
                )
        finally:
            await autocommit.close()
