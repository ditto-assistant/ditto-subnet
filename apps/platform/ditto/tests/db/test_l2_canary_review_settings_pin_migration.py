"""The report-only canary posture pin round-trips and guards its scope (#2448).

Upgrade adds an all-or-nothing pin whose scope must live in the
``l2-report-canary`` namespace and must be the pinned revision's own scope, so
even a regressed scheduler cannot bind a canary to ``*``, ``bootstrap``, or a
node scope. Downgrade removes it cleanly.
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.screener_review_settings import (
    ScreenerReviewSettings,
    is_l2_report_canary_scope,
    review_settings_checksum,
)
from ditto.db.models import (
    Agent,
    ScreenerL2ReportCanary,
    ScreenerNode,
    ScreenerReviewSettingsRevision,
    ScreeningAttempt,
)

_PARENT = "5e2a8c4f9d17"
_PIN_COLUMNS = {
    "review_settings_revision",
    "review_settings_scope",
    "review_settings_checksum",
}


def _alembic(*args: str) -> None:
    subprocess.run(
        ["uv", "run", "alembic", *args],
        check=True,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )


async def _pin_columns(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'screener_l2_report_canaries'"
            )
        )
        return {row[0] for row in rows} & _PIN_COLUMNS


async def _seed_source(
    maker: async_sessionmaker[AsyncSession],
) -> tuple[str, UUID, UUID, dict[str, int]]:
    """A node, an exact source attempt, and one revision per probed scope."""
    node_id = f"pin-migration-{uuid4().hex[:8]}"
    agent_id, attempt_id = uuid4(), uuid4()
    now = datetime.now(UTC)
    settings = ScreenerReviewSettings(mode="enforce")
    revisions: dict[str, int] = {}
    async with maker() as session, session.begin():
        session.add(
            ScreenerNode(
                environment="prod",
                node_id=node_id,
                provider="hetzner",
                provider_resource_id=node_id,
                screener_hotkey=f"hotkey-{node_id}",
                token_hash="f" * 64,
                token_expires_at=now + timedelta(hours=1),
                status="active",
                capacity=1,
            )
        )
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey="5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm",
                name="pin-migration-agent",
                sha256="a" * 64,
                status=AgentStatus.REJECTED,
            )
        )
        await session.flush()
        session.add(
            ScreeningAttempt(
                attempt_id=attempt_id,
                agent_id=agent_id,
                artifact_sha256="a" * 64,
                screener_hotkey=f"hotkey-{node_id}",
                policy_version=13,
                status="rejected",
                started_at=now - timedelta(minutes=1),
                deadline=now,
                finished_at=now,
            )
        )
        for scope in ("*", node_id, "l2-report-canary-migration"):
            row = ScreenerReviewSettingsRevision(
                parent_revision=0,
                scope=scope,
                settings=settings.model_dump(mode="json"),
                checksum=review_settings_checksum(settings),
                reason="pin migration scope probe",
                actor="test",
            )
            session.add(row)
            await session.flush()
            revisions[scope] = row.revision
    return node_id, agent_id, attempt_id, revisions


async def _insert_canary(
    maker: async_sessionmaker[AsyncSession],
    *,
    node_id: str,
    agent_id: UUID,
    attempt_id: UUID,
    revision: int | None,
    scope: str | None,
    checksum: str | None = "b" * 64,
) -> None:
    async with maker() as session, session.begin():
        session.add(
            ScreenerL2ReportCanary(
                canary_id=uuid4(),
                request_id=uuid4(),
                agent_id=agent_id,
                source_attempt_id=attempt_id,
                artifact_sha256="a" * 64,
                policy_version=13,
                bench_version=13,
                target_node_id=node_id,
                expected_agent_status="rejected",
                expected_score_count=0,
                review_label="known_reject",
                review_settings_revision=revision,
                review_settings_scope=scope,
                review_settings_checksum=checksum,
                status="incomplete",
            )
        )


async def test_review_settings_pin_round_trip_and_scope_guard(
    engine: AsyncEngine, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    try:
        _alembic("downgrade", _PARENT)
        assert await _pin_columns(engine) == set()
        _alembic("upgrade", "head")
        assert await _pin_columns(engine) == _PIN_COLUMNS

        node_id, agent_id, attempt_id, revisions = await _seed_source(session_maker)
        canary_scope = "l2-report-canary-migration"
        # Production scopes cannot be pinned even when the revision exists.
        for scope in ("*", node_id):
            with pytest.raises(IntegrityError):
                await _insert_canary(
                    session_maker,
                    node_id=node_id,
                    agent_id=agent_id,
                    attempt_id=attempt_id,
                    revision=revisions[scope],
                    scope=scope,
                )
        # Nor by stamping a canary scope beside a production revision: the
        # stamped scope must be the pinned revision's own scope.
        for scope in ("*", node_id):
            with pytest.raises(IntegrityError):
                await _insert_canary(
                    session_maker,
                    node_id=node_id,
                    agent_id=agent_id,
                    attempt_id=attempt_id,
                    revision=revisions[scope],
                    scope=canary_scope,
                )
        # All three pin fields or none. SQL CHECKs pass on NULL, so each
        # partial shape must be refused explicitly.
        partial_pins: tuple[tuple[int | None, str | None, str | None], ...] = (
            (revisions[canary_scope], None, "b" * 64),
            (revisions[canary_scope], canary_scope, None),
            (None, canary_scope, "b" * 64),
        )
        for partial_revision, partial_scope, partial_checksum in partial_pins:
            with pytest.raises(IntegrityError):
                await _insert_canary(
                    session_maker,
                    node_id=node_id,
                    agent_id=agent_id,
                    attempt_id=attempt_id,
                    revision=partial_revision,
                    scope=partial_scope,
                    checksum=partial_checksum,
                )
        await _insert_canary(
            session_maker,
            node_id=node_id,
            agent_id=agent_id,
            attempt_id=attempt_id,
            revision=revisions[canary_scope],
            scope=canary_scope,
        )
        await _insert_canary(
            session_maker,
            node_id=node_id,
            agent_id=agent_id,
            attempt_id=attempt_id,
            revision=None,
            scope=None,
            checksum=None,
        )
        # A pinned revision cannot be deleted, or moved to another scope, out
        # from under its canary.
        for statement in (
            "DELETE FROM screener_review_settings_revisions WHERE revision = :revision",
            "UPDATE screener_review_settings_revisions "
            "SET scope = 'l2-report-canary-moved' WHERE revision = :revision",
        ):
            async with session_maker() as session:
                with pytest.raises(IntegrityError):
                    async with session.begin():
                        await session.execute(
                            text(statement), {"revision": revisions[canary_scope]}
                        )

        # Downgrade drops the pin even with pinned rows present.
        _alembic("downgrade", _PARENT)
        assert await _pin_columns(engine) == set()
    finally:
        _alembic("upgrade", "head")


# Scopes probing the edges of the namespace. The two ``l2X...``/``l2_...``
# spellings are the ones a reader who mistakes the hyphens for LIKE's ``_``
# wildcard would expect the database to admit.
_SCOPE_PROBES = (
    "l2-report-canary",
    "l2-report-canary-",
    "l2-report-canary-ctl137",
    "l2-report-canary-a.b_c",
    "l2-report-canary--x",
    "l2-report-canary-%",
    "l2-report-canaryctl137",
    "l2-report-canary_ctl137",
    "l2-report-canary%",
    "l2-report-canar",
    "l2XreportXcanary-evil",
    "l2_report_canary-evil",
    "l2-report_canary-x",
    "L2-report-canary-x",
    "xl2-report-canary-x",
    " l2-report-canary-x",
    "l2-report-canary\n",
    "bootstrap",
    "integrity-double-check",
)


async def test_pin_scope_check_admits_exactly_the_application_namespace(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The CHECK and ``is_l2_report_canary_scope`` accept the same scopes.

    Scheduling validates with the Python predicate; the CHECK is the database
    backstop. If the two ever disagree, one of them is not guarding what the
    other promises.
    """
    node_id, agent_id, attempt_id, _ = await _seed_source(session_maker)
    settings = ScreenerReviewSettings(mode="enforce")
    accepted: dict[str, bool] = {}
    for index, scope in enumerate(_SCOPE_PROBES, start=1):
        async with session_maker() as session, session.begin():
            row = ScreenerReviewSettingsRevision(
                parent_revision=index,
                scope=scope,
                settings=settings.model_dump(mode="json"),
                checksum=review_settings_checksum(settings),
                reason="pin scope namespace probe",
                actor="test",
            )
            session.add(row)
            await session.flush()
            revision = row.revision
        try:
            await _insert_canary(
                session_maker,
                node_id=node_id,
                agent_id=agent_id,
                attempt_id=attempt_id,
                revision=revision,
                scope=scope,
            )
        except IntegrityError:
            accepted[scope] = False
        else:
            accepted[scope] = True
    assert accepted == {
        scope: is_l2_report_canary_scope(scope) for scope in _SCOPE_PROBES
    }
    assert accepted["l2-report-canary"] and accepted["l2-report-canary-ctl137"]
    assert not accepted["l2XreportXcanary-evil"]
    assert not accepted["l2_report_canary-evil"]
