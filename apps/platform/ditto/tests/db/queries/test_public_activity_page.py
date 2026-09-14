"""Large-fixture/query-budget coverage for bounded public activity reads."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ditto.api_models.agent_status import AgentStatus
from ditto.db.models import (
    Agent,
    ScreeningAttempt,
    ScreeningRetryOverride,
)
from ditto.db.queries.agents import query_public_activity_page

logger = logging.getLogger(__name__)


async def test_failed_submission_is_demand_only_with_latest_attempt_override(
    session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    parked = Agent(
        agent_id=uuid4(),
        miner_hotkey="5ParkedFailed",
        name="parked-failed",
        sha256="a1" * 32,
        status=AgentStatus.SCREENING_FAILED,
        screening_policy_version=9,
    )
    retry = Agent(
        agent_id=uuid4(),
        miner_hotkey="5AuthorizedRetry",
        name="authorized-retry",
        sha256="b2" * 32,
        status=AgentStatus.SCREENING_FAILED,
        screening_policy_version=9,
    )
    stale = Agent(
        agent_id=uuid4(),
        miner_hotkey="5StaleRetry",
        name="stale-retry",
        sha256="c3" * 32,
        status=AgentStatus.SCREENING_FAILED,
        screening_policy_version=9,
    )
    parked_attempt = ScreeningAttempt(
        attempt_id=uuid4(),
        agent_id=parked.agent_id,
        screener_hotkey="5Screener",
        policy_version=9,
        status="failed",
        started_at=now - timedelta(minutes=3),
        deadline=now - timedelta(minutes=2),
        finished_at=now - timedelta(minutes=2),
    )
    retry_attempt = ScreeningAttempt(
        attempt_id=uuid4(),
        agent_id=retry.agent_id,
        screener_hotkey="5Screener",
        policy_version=9,
        status="failed",
        started_at=now - timedelta(minutes=3),
        deadline=now - timedelta(minutes=2),
        finished_at=now - timedelta(minutes=2),
    )
    stale_attempt = ScreeningAttempt(
        attempt_id=uuid4(),
        agent_id=stale.agent_id,
        screener_hotkey="5Screener",
        policy_version=9,
        status="failed",
        started_at=now - timedelta(minutes=4),
        deadline=now - timedelta(minutes=3),
        finished_at=now - timedelta(minutes=3),
    )
    latest_stale_attempt = ScreeningAttempt(
        attempt_id=uuid4(),
        agent_id=stale.agent_id,
        screener_hotkey="5Screener",
        policy_version=9,
        status="failed",
        started_at=now - timedelta(minutes=2),
        deadline=now - timedelta(minutes=1),
        finished_at=now - timedelta(minutes=1),
    )
    session.add_all(
        [
            parked,
            retry,
            stale,
            parked_attempt,
            retry_attempt,
            stale_attempt,
            latest_stale_attempt,
            ScreeningRetryOverride(
                override_id=uuid4(),
                agent_id=retry.agent_id,
                attempt_id=retry_attempt.attempt_id,
                artifact_sha256=retry.sha256,
                expected_score_count=0,
                reason="retry exact latest attempt",
                actor="test",
            ),
            ScreeningRetryOverride(
                override_id=uuid4(),
                agent_id=stale.agent_id,
                attempt_id=stale_attempt.attempt_id,
                artifact_sha256=stale.sha256,
                expected_score_count=0,
                reason="stale earlier retry grant",
                actor="test",
            ),
        ]
    )
    await session.commit()

    result = await query_public_activity_page(
        session,
        bench_version=7,
        page=1,
        limit=10,
        requested_statuses=set(),
        downloadable_only=False,
        downloadable_agent_ids=set(),
        query=None,
        ath_only=False,
        active_validation_agent_ids=set(),
        active_assignment_agent_ids=set(),
        score_continuation_floor=None,
    )

    statuses = {row.agent.agent_id: row.public_status for row in result.rows}
    assert statuses[parked.agent_id] == "not_queued"
    assert statuses[retry.agent_id] == "waiting_screening"
    assert statuses[stale.agent_id] == "not_queued"
    assert result.status_counts == {"not_queued": 2, "waiting_screening": 1}


async def _seed_large_activity(session: AsyncSession, *, count: int) -> None:
    await session.execute(
        text(
            """
            INSERT INTO agents (
                agent_id, miner_hotkey, name, version, sha256, size_bytes,
                status, screening_policy_version, created_at
            )
            SELECT
                ('00000000-0000-4000-8000-' || lpad(gs::text, 12, '0'))::uuid,
                '5LargeFixture' || gs::text,
                'activity-' || gs::text,
                1,
                md5(gs::text) || md5(gs::text),
                1,
                CASE WHEN gs <= 100 THEN 'uploaded' ELSE 'scored' END::agentstatus,
                9,
                now() - make_interval(secs => gs)
            FROM generate_series(1, :count) AS gs
            """
        ),
        {"count": count},
    )


async def test_activity_page_hydrates_only_one_page_in_five_round_trips(
    engine: AsyncEngine,
    session: AsyncSession,
) -> None:
    await _seed_large_activity(session, count=10_000)
    await session.commit()
    statements: list[str] = []
    emitted: list[tuple[str, object]] = []

    def count_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement)
        emitted.append((statement, parameters))

    event.listen(engine.sync_engine, "before_cursor_execute", count_statement)
    try:
        result = await query_public_activity_page(
            session,
            bench_version=7,
            page=2,
            limit=25,
            requested_statuses=set(),
            downloadable_only=False,
            downloadable_agent_ids=set(),
            query=None,
            ath_only=False,
            active_validation_agent_ids=set(),
            active_assignment_agent_ids=set(),
            score_continuation_floor=None,
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", count_statement)

    assert len(statements) == 5
    assert len(result.rows) == 25
    assert result.total == 10_000
    assert result.status_counts == {"scored": 9_900, "waiting_screening": 100}
    assert result.waiting_agent_ids == []
    assert (
        sum(isinstance(value, Agent) for value in session.identity_map.values()) == 25
    )

    detail_sql, detail_parameters = next(
        (statement, parameters)
        for statement, parameters in emitted
        if "public_activity_selected" in statement
        and "evaluation_payments" in statement
    )
    connection = await session.connection()
    plan = list(
        (
            await connection.exec_driver_sql(
                "EXPLAIN (ANALYZE, BUFFERS) " + detail_sql,
                cast(Any, detail_parameters),
            )
        ).scalars()
    )
    rendered_plan = "\n".join(str(line) for line in plan)
    logger.info("ORM public activity page plan:\n%s", rendered_plan)
    assert "actual time=" in rendered_plan
    assert "Buffers:" in rendered_plan
    assert "rows=25" in rendered_plan


async def test_operations_query_filters_in_sql_and_bounds_terminal_history(
    engine: AsyncEngine,
    session: AsyncSession,
) -> None:
    await _seed_large_activity(session, count=10_000)
    await session.commit()
    statements: list[str] = []

    def count_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", count_statement)
    try:
        result = await query_public_activity_page(
            session,
            bench_version=7,
            page=1,
            limit=1,
            requested_statuses=set(),
            downloadable_only=False,
            downloadable_agent_ids=set(),
            query=None,
            ath_only=False,
            active_validation_agent_ids=set(),
            active_assignment_agent_ids=set(),
            score_continuation_floor=None,
            operations_terminal_history_limit=50,
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", count_statement)

    assert len(statements) == 5
    assert len(result.rows) == 150
    assert [row.public_status for row in result.rows].count("waiting_screening") == 100
    assert [row.public_status for row in result.rows].count("scored") == 50
    assert result.total == 10_000
    assert (
        sum(isinstance(value, Agent) for value in session.identity_map.values()) == 150
    )


async def test_search_and_status_are_applied_before_pagination(
    session: AsyncSession,
) -> None:
    await _seed_large_activity(session, count=1_000)
    await session.commit()

    result = await query_public_activity_page(
        session,
        bench_version=7,
        page=1,
        limit=25,
        requested_statuses={"waiting_screening"},
        downloadable_only=False,
        downloadable_agent_ids=set(),
        query="activity-42",
        ath_only=False,
        active_validation_agent_ids=set(),
        active_assignment_agent_ids=set(),
        score_continuation_floor=None,
    )

    assert result.total == 1
    # Totals intentionally describe the search population before the requested
    # status filter, matching the public facet-count contract.
    assert result.status_counts == {"scored": 10, "waiting_screening": 1}
    assert [row.agent.name for row in result.rows] == ["activity-42"]
