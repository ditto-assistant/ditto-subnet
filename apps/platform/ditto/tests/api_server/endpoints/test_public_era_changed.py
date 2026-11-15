"""Previous-era rows the active bench never admits read ``era_changed`` everywhere.

SN-462, SN-483 and SN-485: after bench v18 activated, earlier-era submissions
it never admitted read "evaluating" on miner pages (the profile read
``agents.status``), and finalized v17 rows read ``scored`` with nothing saying
they no longer compete. Backroom's stuck list called the same rows "not enough
expired tickets to restore quorum" or "operator-authorized validator retry is
queued", neither of which can ever happen.

The status is a read-time projection of the queue's own admission predicate,
so these tests also pin that it changes no state and that it flips back by
itself when the row is admitted.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.ticket_status import TicketStatus
from ditto.api_server.dependencies import get_session
from ditto.api_server.submission_status import closed_submission_statuses
from ditto.db.models import (
    Agent,
    BenchmarkRollout,
    BenchmarkRolloutMember,
    Score,
    ScreeningAttempt,
    ValidatorQueueWithdrawal,
    ValidatorTicket,
)
from ditto.db.queries.benchmark_era_stranded import stranded_previous_era_agents
from ditto_screening_protocol import SCREENING_FLOOR_POLICY_VERSION

_FROM = 17
_ACTIVE = 18
_NOW = datetime.now(UTC)
_ROLLOUT_START = _NOW - timedelta(days=3)
_ACTIVATED_AT = _ROLLOUT_START + timedelta(hours=1)
_BEFORE = _ROLLOUT_START - timedelta(days=1)
_AFTER = _ACTIVATED_AT + timedelta(hours=2)

_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_TOKEN}", "X-Admin-Actor": "operator"}
_BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_LABEL = f"Era changed · resubmit for v{_ACTIVE}"
_ERA_REASON = (
    f"historical submission is not admitted to the v{_ACTIVE} validator queue "
    f"(not_admitted_to_active_bench); miners see '{_LABEL}', next step: "
    f"resubmit to compete on v{_ACTIVE}"
)


def _hotkey(name: str) -> str:
    """A deterministic, SS58-shaped hotkey unique to ``name``."""
    digest = sha256(name.encode()).digest()
    body = "".join(_BASE58[byte % len(_BASE58)] for byte in digest)
    return ("5" + (body * 2))[:48]


@pytest.fixture(autouse=True)
def floor_policy_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the no-scheduled-activation screening floor for this module."""
    import ditto.screener_policy_state as policy_state

    monkeypatch.setattr(
        policy_state, "_required_policy_version", SCREENING_FLOOR_POLICY_VERSION
    )


@pytest.fixture
def maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_TOKEN)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


async def _agent(
    session: AsyncSession,
    *,
    name: str,
    status: AgentStatus,
    created_at: datetime,
) -> UUID:
    agent_id = uuid4()
    session.add(
        Agent(
            agent_id=agent_id,
            miner_hotkey=_hotkey(name),
            name=name,
            version=1,
            sha256=agent_id.hex * 2,
            size_bytes=1024,
            status=status,
            screening_policy_version=SCREENING_FLOOR_POLICY_VERSION,
            created_at=created_at,
            screened_image_sha256="a" * 64,
            screened_image_size_bytes=1024,
            screened_image_id="sha256:" + "b" * 64,
            screened_image_ref=f"ditto-screen/{agent_id}:latest",
            screened_image_upload_id=uuid4(),
            screened_image_verified_at=created_at + timedelta(minutes=30),
        )
    )
    await session.flush()
    finished = created_at + timedelta(minutes=30)
    session.add(
        ScreeningAttempt(
            attempt_id=uuid4(),
            agent_id=agent_id,
            screener_hotkey=_hotkey("screener"),
            policy_version=SCREENING_FLOOR_POLICY_VERSION,
            status="passed",
            started_at=finished - timedelta(minutes=20),
            deadline=finished + timedelta(minutes=20),
            finished_at=finished,
            build_only=True,
        )
    )
    await session.flush()
    return agent_id


async def _scores(session: AsyncSession, agent_id: UUID, *, count: int) -> None:
    for index in range(count):
        session.add(
            Score(
                agent_id=agent_id,
                bench_version=_FROM,
                validator_hotkey=_hotkey(f"validator-{index}"),
                run_id=f"{agent_id}-{index}",
                signature=None,
                seed=7 + index,
                composite=0.61,
                tool_mean=0.61,
                memory_mean=0.61,
                median_ms=1,
                n=350,
                details={"bench_version": _FROM},
                generated_at=_BEFORE + timedelta(hours=1),
            )
        )
    await session.flush()


class _Seeded:
    rollout_id: UUID
    finalized: UUID
    waiting: UUID
    dead_retry: UUID
    withdrawn: UUID
    current: UUID


async def _seed(maker: async_sessionmaker[AsyncSession]) -> _Seeded:
    seeded = _Seeded()
    async with maker() as session, session.begin():
        rollout = BenchmarkRollout(
            rollout_id=uuid4(),
            from_version=_FROM,
            desired_version=_ACTIVE,
            status="activated",
            cohort_size=8,
            created_at=_ROLLOUT_START,
            activated_at=_ACTIVATED_AT,
        )
        session.add(rollout)
        await session.flush()
        seeded.rollout_id = rollout.rollout_id
        # Finalized on v17 outside the frozen cohort: the stranded listing's row.
        seeded.finalized = await _agent(
            session, name="v17-final", status=AgentStatus.SCORED, created_at=_BEFORE
        )
        await _scores(session, seeded.finalized, count=3)
        # Queued before v18 opened, never leased: the stuck "queued" shape.
        seeded.waiting = await _agent(
            session,
            name="v17-waiting",
            status=AgentStatus.EVALUATING,
            created_at=_BEFORE,
        )
        # Two v17 scores and an operator grant on a v17 lease nobody serves:
        # the dead "operator-authorized validator retry is queued" shape.
        seeded.dead_retry = await _agent(
            session,
            name="v17-dead-retry",
            status=AgentStatus.EVALUATING,
            created_at=_BEFORE,
        )
        await _scores(session, seeded.dead_retry, count=2)
        session.add(
            ValidatorTicket(
                agent_id=seeded.dead_retry,
                validator_hotkey=_hotkey("validator-9"),
                status=TicketStatus.EXPIRED,
                issued_at=_BEFORE + timedelta(hours=2),
                deadline=_BEFORE + timedelta(hours=3),
                bench_version=_FROM,
                attempt_count=1,
                manual_retry_grants=1,
                retry_after=None,
            )
        )
        # Current-era work an operator withdrew: still ``not_queued``.
        seeded.withdrawn = await _agent(
            session,
            name="v18-withdrawn",
            status=AgentStatus.EVALUATING,
            created_at=_AFTER,
        )
        session.add(
            ValidatorQueueWithdrawal(
                withdrawal_id=uuid4(),
                agent_id=seeded.withdrawn,
                bench_version=_ACTIVE,
                actor="operator",
                reason="duplicate of a newer version",
                expected_snapshot="test",
                score_count=0,
                ticket_snapshot=[],
                created_at=_AFTER + timedelta(hours=1),
            )
        )
        seeded.current = await _agent(
            session,
            name="v18-current",
            status=AgentStatus.EVALUATING,
            created_at=_AFTER,
        )
    return seeded


def _change(*, scored_bench_version: int | None) -> dict[str, object]:
    return {
        "reason": "not_admitted_to_active_bench",
        "bench_version": _ACTIVE,
        "scored_bench_version": scored_bench_version,
        "next_step": "resubmit",
    }


async def _activity(client: httpx.AsyncClient) -> tuple[dict, dict[str, dict]]:
    body = (await client.get("/api/v1/public/activity")).json()
    return body, {entry["agent_id"]: entry for entry in body["entries"]}


async def test_previous_era_rows_read_era_changed_on_every_public_surface(
    app: FastAPI,
    client: httpx.AsyncClient,
    maker: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed(maker)
    _install(app, maker)

    body, by_id = await _activity(client)
    finalized = by_id[str(seeded.finalized)]
    waiting = by_id[str(seeded.waiting)]
    assert finalized["status"] == "era_changed"
    assert finalized["era_change"] == _change(scored_bench_version=_FROM)
    assert waiting["status"] == "era_changed"
    assert waiting["era_change"] == _change(scored_bench_version=None)
    assert by_id[str(seeded.dead_retry)]["status"] == "era_changed"
    # An operator withdrawal keeps its own, earlier label.
    assert by_id[str(seeded.withdrawn)]["status"] == "not_queued"
    assert by_id[str(seeded.withdrawn)]["queue_withdrawn"] is True
    assert by_id[str(seeded.withdrawn)]["era_change"] is None
    assert by_id[str(seeded.current)]["status"] == "waiting_validator"
    assert by_id[str(seeded.current)]["era_change"] is None
    # The unfiltered projection relabels the finalized row on its returned
    # page only (activity is a polling hot path), so the count keeps it under
    # its stored status. The status filters below are exact.
    assert body["status_counts"]["era_changed"] == 2
    assert body["status_counts"]["scored"] == 1

    filtered = (
        await client.get("/api/v1/public/activity", params={"status": "era_changed"})
    ).json()
    assert {entry["agent_id"] for entry in filtered["entries"]} == {
        str(seeded.finalized),
        str(seeded.waiting),
        str(seeded.dead_retry),
    }
    assert filtered["total"] == 3
    assert {entry["status"] for entry in filtered["entries"]} == {"era_changed"}
    assert filtered["status_counts"]["era_changed"] == 3
    scored_only = (
        await client.get("/api/v1/public/activity", params={"status": "scored"})
    ).json()
    assert str(seeded.finalized) not in {
        entry["agent_id"] for entry in scored_only["entries"]
    }
    assert scored_only["total"] == 0

    for agent_id, scored_on in (
        (seeded.finalized, _FROM),
        (seeded.waiting, None),
    ):
        summary = (await client.get(f"/api/v1/public/agent/{agent_id}/summary")).json()
        assert summary["status"] == "era_changed"
        assert summary["era_change"] == _change(scored_bench_version=scored_on)
        profile = (
            await client.get(f"/api/v1/public/miners/{summary['miner_hotkey']}")
        ).json()
        assert [
            (row["status"], row["era_change"]) for row in profile["submissions"]
        ] == [("era_changed", _change(scored_bench_version=scored_on))]

    finalized_pipeline = (
        await client.get(f"/api/v1/public/agent/{seeded.finalized}/pipeline")
    ).json()
    assert finalized_pipeline["status"] == "era_changed"
    assert finalized_pipeline["era_change"] == _change(scored_bench_version=_FROM)
    # The v17 history stays visible on the detail page.
    assert finalized_pipeline["score_bench_version"] == _FROM
    assert finalized_pipeline["score_count"] == 3

    # A retry on a version nobody serves is not published as live retry state.
    dead_pipeline = (
        await client.get(f"/api/v1/public/agent/{seeded.dead_retry}/pipeline")
    ).json()
    assert dead_pipeline["status"] == "era_changed"
    assert dead_pipeline["validator_retry"] is None

    withdrawn_profile = (
        await client.get(f"/api/v1/public/miners/{_hotkey('v18-withdrawn')}")
    ).json()
    assert withdrawn_profile["submissions"][0]["status"] == "not_queued"


async def test_backroom_names_the_label_miners_see(
    app: FastAPI,
    client: httpx.AsyncClient,
    maker: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed(maker)
    _install(app, maker)

    stuck = (
        await client.get(
            "/api/v1/admin/validation-retries",
            params={"generation": "all"},
            headers=_HEADERS,
        )
    ).json()
    rows = {row["agent_id"]: row for row in stuck["submissions"]}

    waiting = rows[str(seeded.waiting)]
    assert waiting["blocking_reason"] == _ERA_REASON
    assert waiting["era_change"] == _change(scored_bench_version=None)
    assert waiting["serves_active_bench"] is False

    dead = rows[str(seeded.dead_retry)]
    assert dead["bench_version"] == _FROM
    assert dead["blocking_reason"] == (
        f"bench v{_FROM} is no longer served (validators score only v{_ACTIVE}), "
        f"so this v{_FROM} retry state will not run; {_ERA_REASON}"
    )
    assert dead["serves_active_bench"] is False
    # Labels only: the classifier's own retry state is unchanged.
    assert dead["retry_state"] in {"retry_available", "queued", "exhausted"}

    current = rows.get(str(seeded.current))
    if current is not None:
        assert current["era_change"] is None
        assert current["serves_active_bench"] is True

    readiness = (
        await client.get(
            f"/api/v1/admin/agents/{seeded.waiting}/scoring-readiness",
            headers=_HEADERS,
        )
    ).json()
    assert readiness["leaseable"] is False
    assert _ERA_REASON in readiness["blocking_reasons"]
    assert readiness["era_change"] == _change(scored_bench_version=None)


async def test_era_changed_flips_back_when_admitted_and_writes_nothing(
    app: FastAPI,
    client: httpx.AsyncClient,
    maker: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed(maker)
    _install(app, maker)

    async def _counts() -> tuple[int, int, int]:
        async with maker() as session:
            return (
                int(await session.scalar(select(func.count()).select_from(Agent)) or 0),
                int(
                    await session.scalar(
                        select(func.count()).select_from(ValidatorTicket)
                    )
                    or 0
                ),
                int(
                    await session.scalar(
                        select(func.count()).select_from(BenchmarkRolloutMember)
                    )
                    or 0
                ),
            )

    before = await _counts()
    _, by_id = await _activity(client)
    assert by_id[str(seeded.finalized)]["status"] == "era_changed"
    async with maker() as session:
        report = await stranded_previous_era_agents(session)
        # One population: the stranded listing names the finalized row.
        assert [item.agent_id for item in report.items] == [seeded.finalized]
    assert await _counts() == before

    # An operator admission (a rollout member appended after activation) is
    # the existing admission path. Nothing else changes, and the labels follow.
    async with maker() as session, session.begin():
        for position, agent_id in enumerate((seeded.finalized, seeded.waiting), 100):
            session.add(
                BenchmarkRolloutMember(
                    rollout_id=seeded.rollout_id,
                    agent_id=agent_id,
                    position=position,
                    frozen_miner_hotkey=_hotkey(str(agent_id)),
                    frozen_composite=0.61,
                )
            )

    _, by_id = await _activity(client)
    assert by_id[str(seeded.finalized)]["status"] == "scored"
    assert by_id[str(seeded.finalized)]["era_change"] is None
    assert by_id[str(seeded.waiting)]["status"] == "waiting_validator"
    assert by_id[str(seeded.waiting)]["era_change"] is None
    async with maker() as session:
        agents = list(
            await session.scalars(
                select(Agent).where(
                    Agent.agent_id.in_([seeded.finalized, seeded.waiting])
                )
            )
        )
        closed = await closed_submission_statuses(
            session, agents=agents, active_version=_ACTIVE
        )
    assert closed == {}


async def test_unfiltered_page_pays_one_bounded_lookup(
    engine: AsyncEngine,
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Activity polls every eight seconds; the finalized-row label is page-bound.

    The projection keeps its five statements, and the returned page adds one
    lookup keyed by that page's own finalized rows, never a scan of history.
    """
    from sqlalchemy import event

    from ditto.db.queries.agents import query_public_activity_page

    seeded = await _seed(maker)
    statements: list[str] = []

    def count(_conn, _cursor, statement, *_args) -> None:  # type: ignore[no-untyped-def]
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", count)
    try:
        async with maker() as session:
            page = await query_public_activity_page(
                session,
                bench_version=_ACTIVE,
                page=1,
                limit=50,
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
        event.remove(engine.sync_engine, "before_cursor_execute", count)

    by_id = {row.agent.agent_id: row.public_status for row in page.rows}
    assert by_id[seeded.finalized] == "era_changed"
    assert by_id[seeded.waiting] == "era_changed"
    # The projection's five statements (rollout read included, as in
    # test_activity_page_hydrates_only_one_page_in_five_round_trips) plus the
    # one page lookup.
    assert len(statements) == 6, statements
