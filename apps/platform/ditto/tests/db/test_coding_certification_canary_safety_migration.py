"""Data round trip for the coding-certification canary-safety migration."""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from uuid import UUID, uuid4

import asyncpg
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ditto.db.models import Agent, CodingCapabilityCertification
from ditto.db.queries.coding_certification_leases import (
    CodingCertificationLeaseNotAvailableError,
    database_now,
)
from ditto.tests import pgharness
from ditto.tests.db.queries import test_coding_certification_canary_safety as canary

_REVISION = "c7a2e5d19b43"
_PARENT = "4b7f09043317"


def _alembic(target: pgharness.Dsn, action: str, revision: str) -> None:
    from alembic.config import Config

    from alembic import command

    previous = {key: os.environ.get(key) for key in target.env}
    os.environ.update(target.env)
    try:
        cfg = Config(str(pgharness._REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(pgharness._REPO_ROOT / "alembic"))
        getattr(command, action)(cfg, revision)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


async def _seed(dsn: pgharness.Dsn) -> tuple[dict[str, UUID], dict[str, Agent]]:
    # A scratch database, not this worker's: the migration is downgraded here.
    engine = create_async_engine(dsn.sqlalchemy)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            agent = await canary._qualified_agent(session)
            receipted = await canary._issue_and_claim(session, agent)
            await canary._record_receipt(session, receipted)
            other = await canary._qualified_agent(session, evidence="22")
            aborted = await canary._issue_and_claim(session, other)
            await canary._set_allowlist(session, [canary._entry(agent)])
            third = await canary._qualified_agent(session, evidence="33")
            expired = await canary._issue_and_claim(session, third)
            await canary._backdate(session, expired)
            assert await canary._issue(session, third) != expired

            # Renewed identity with a new lease in flight: two completed runs
            # whose results expired, then a fresh issued lease.
            renewing = await canary._qualified_agent(session, evidence="44")
            first_run = await canary._issue_and_claim(session, renewing)
            await canary._record_receipt(
                session, first_run, valid_for=-timedelta(seconds=1)
            )
            second_run = await canary._issue_and_claim(session, renewing)
            await canary._record_receipt(
                session, second_run, valid_for=-timedelta(seconds=1)
            )
            in_flight = await canary._issue(session, renewing)

            # Renewed identity with nothing in flight: an expired run, then a
            # still-valid one.
            renewed = await canary._qualified_agent(session, evidence="55")
            old_run = await canary._issue_and_claim(session, renewed)
            await canary._record_receipt(
                session, old_run, valid_for=-timedelta(seconds=1)
            )
            await canary._backdate(session, old_run, ago=timedelta(minutes=50))
            new_run = await canary._issue_and_claim(session, renewed)
            await canary._record_receipt(session, new_run)

            # A legacy receipt without a lease, still valid.
            legacy = await canary._qualified_agent(session, evidence="66")
            async with session.begin():
                now = await database_now(session)
                row = canary._receipt_row(
                    agent_id=legacy.agent_id,
                    artifact_sha256=legacy.sha256,
                    screened_image_sha256=legacy.screened_image_sha256 or "",
                    lease_id=None,
                    validator=canary._THIRD_VALIDATOR,
                    issued_at=now,
                    expires_at=now + timedelta(hours=1),
                )
                session.add(row)
            legacy_receipt = row.certification_row_id

            # A legacy claimed lease with a failed receipt that is still
            # unexpired: completed, but it blocks no retry.
            failing = await canary._qualified_agent(session, evidence="88")
            failed_run = await canary._issue_and_claim(session, failing)
            await canary._record_receipt(session, failed_run, status="failed")

            # A claimed lease that never produced a certification.
            never = await canary._qualified_agent(session, evidence="77")
            unreceipted = await canary._issue_and_claim(session, never)
            await canary._backdate(session, unreceipted)
    finally:
        await engine.dispose()
    return (
        {
            "receipted": receipted,
            "aborted": aborted,
            "expired": expired,
            "first_run": first_run,
            "second_run": second_run,
            "in_flight": in_flight,
            "old_run": old_run,
            "new_run": new_run,
            "failed_run": failed_run,
            "unreceipted": unreceipted,
            "legacy_receipt": legacy_receipt,
        },
        {
            "agent": agent,
            "renewed": renewed,
            "legacy": legacy,
            "never": never,
            "failing": failing,
        },
    )


async def _state(dsn: pgharness.Dsn, ids: dict[str, UUID]) -> tuple[dict, bool]:
    conn = await asyncpg.connect(dsn.asyncpg)
    try:
        statuses = {
            name: await conn.fetchval(
                "SELECT status FROM coding_certification_leases WHERE lease_id = $1",
                lease_id,
            )
            for name, lease_id in ids.items()
            if name != "legacy_receipt"
        }
        statuses["legacy_receipt"] = await conn.fetchval(
            "SELECT count(*) FROM coding_capability_certifications "
            "WHERE certification_row_id = $1 AND lease_id IS NULL",
            ids["legacy_receipt"],
        )
        validated = await conn.fetchval(
            "SELECT bool_and(convalidated) FROM pg_constraint "
            "WHERE conrelid = 'coding_certification_leases'::regclass "
            "AND contype = 'c'"
        )
        return statuses, bool(validated)
    finally:
        await conn.close()


_AT_HEAD = {
    "receipted": "completed",
    "aborted": "aborted",
    "expired": "expired",
    "first_run": "completed",
    "second_run": "completed",
    "in_flight": "issued",
    "old_run": "completed",
    "new_run": "completed",
    "failed_run": "completed",
    "unreceipted": "claimed",
    "legacy_receipt": 1,
}


async def _renewal_behaviour(dsn: pgharness.Dsn, agents: dict[str, Agent]) -> None:
    """After the round trip, the renewal rule reads the normalized rows."""

    engine = create_async_engine(dsn.sqlalchemy)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            # The downgrade dropped the allowlist; admit the tuples again.
            await canary._set_allowlist(
                session, [canary._entry(agent) for agent in agents.values()]
            )
            for name in ("agent", "renewed", "legacy"):
                refused = await canary._issue_error(session, agents[name])
                assert isinstance(refused, CodingCertificationLeaseNotAvailableError), (
                    name
                )
                assert "still valid" in str(refused), name
            # A claimed lease that never produced a certification expires on
            # the next issue and blocks nothing; it predates the allowlist
            # stamp, so it spends none of the attempt budget either.
            assert await canary._issue_error(session, agents["never"]) is None
            # An unexpired failed result is not a certification: it retries.
            assert await canary._issue_error(session, agents["failing"]) is None
            async with session.begin():
                now = await database_now(session)
                await session.execute(
                    update(CodingCapabilityCertification)
                    .where(
                        CodingCapabilityCertification.agent_id
                        == agents["legacy"].agent_id
                    )
                    .values(issued_at=now - timedelta(hours=2), expires_at=now)
                )
            assert await canary._issue_error(session, agents["legacy"]) is None
    finally:
        await engine.dispose()


async def test_canary_safety_migration_round_trips_with_lease_history(
    postgres_admin_dsn: pgharness.Dsn,
) -> None:
    name = f"{pgharness.DB_PREFIX}canary_migration_{uuid4().hex[:12]}"
    database = await asyncio.to_thread(
        pgharness.provision_worker_database, postgres_admin_dsn, name
    )
    try:
        ids, agents = await _seed(database.dsn)
        assert await _state(database.dsn, ids) == (_AT_HEAD, True)

        await asyncio.to_thread(_alembic, database.dsn, "downgrade", _PARENT)
        # The prior schema has no completed status and no allowlist abort, and
        # admits one issued or claimed lease per identity.
        statuses, _ = await _state(database.dsn, ids)
        assert statuses == {
            "receipted": "claimed",
            "aborted": "expired",
            "expired": "expired",
            "first_run": "expired",
            "second_run": "expired",
            "in_flight": "issued",
            "old_run": "expired",
            "new_run": "claimed",
            "failed_run": "claimed",
            "unreceipted": "claimed",
            "legacy_receipt": 1,
        }

        await asyncio.to_thread(_alembic, database.dsn, "upgrade", _REVISION)
        # Every receipted lease is completed again, nothing was deleted, and
        # every lease CHECK is validated.
        assert await _state(database.dsn, ids) == (
            {**_AT_HEAD, "aborted": "expired"},
            True,
        )
        await _renewal_behaviour(database.dsn, agents)
    finally:
        await asyncio.to_thread(
            pgharness.drop_worker_database, postgres_admin_dsn, name
        )
