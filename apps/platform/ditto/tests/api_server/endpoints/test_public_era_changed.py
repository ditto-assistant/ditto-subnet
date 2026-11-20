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
    SubmissionRetirement,
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


async def _scores(
    session: AsyncSession, agent_id: UUID, *, count: int, bench_version: int = _FROM
) -> None:
    for index in range(count):
        session.add(
            Score(
                agent_id=agent_id,
                bench_version=bench_version,
                validator_hotkey=_hotkey(f"validator-{index}"),
                run_id=f"{agent_id}-{bench_version}-{index}",
                signature=None,
                seed=7 + index,
                composite=0.61,
                tool_mean=0.61,
                memory_mean=0.61,
                median_ms=1,
                n=350,
                details={"bench_version": bench_version},
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


async def _readiness(client: httpx.AsyncClient, agent_id: UUID) -> dict:
    response = await client.get(
        f"/api/v1/admin/agents/{agent_id}/scoring-readiness", headers=_HEADERS
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_readiness_names_a_previous_era_score_only_when_one_exists(
    app: FastAPI,
    client: httpx.AsyncClient,
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Review r4576442191: status alone is not evidence of a v17 score.

    A refused ``scored`` row with only older-era scores, or with a score on the
    active version, must not be reported as finalized on the previous version.
    The miner projection and the stranded listing use the same predicate.

    Review r4605892617: such a row keeps ``scored`` on the miner's side, so
    readiness names the era boundary without the ``era_change`` payload or a
    quoted miner label.
    """
    seeded = await _seed(maker)
    async with maker() as session, session.begin():
        older_only = await _agent(
            session,
            name="v16-only",
            status=AgentStatus.SCORED,
            created_at=_BEFORE - timedelta(days=5),
        )
        await _scores(session, older_only, count=3, bench_version=_FROM - 1)
        with_active = await _agent(
            session, name="v17-and-v18", status=AgentStatus.SCORED, created_at=_BEFORE
        )
        await _scores(session, with_active, count=3)
        await _scores(session, with_active, count=1, bench_version=_ACTIVE)
    _install(app, maker)

    finalized = await _readiness(client, seeded.finalized)
    assert finalized["era_change"] == _change(scored_bench_version=_FROM)
    for agent_id in (older_only, with_active):
        body = await _readiness(client, agent_id)
        assert body["leaseable"] is False
        assert body["era_change"] is None, body
        assert _ERA_REASON not in body["blocking_reasons"]
        assert (
            f"historical submission is not admitted to the v{_ACTIVE} validator queue"
        ) in body["blocking_reasons"]


async def _issue_active_ticket(
    maker: async_sessionmaker[AsyncSession], agent_id: UUID, *, deadline: datetime
) -> None:
    async with maker() as session, session.begin():
        session.add(
            ValidatorTicket(
                agent_id=agent_id,
                validator_hotkey=_hotkey("validator-live"),
                status=TicketStatus.ISSUED,
                issued_at=deadline - timedelta(hours=1),
                deadline=deadline,
                bench_version=_ACTIVE,
                attempt_count=1,
                manual_retry_grants=0,
            )
        )


async def test_an_expired_active_era_lease_does_not_hold_the_waiting_label(
    app: FastAPI,
    client: httpx.AsyncClient,
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Review r4576442366: only an unexpired lease is a validator holding it.

    ``list_active_validator_assignments`` (behind the activity projection)
    counts ``issued`` rows whose deadline has not passed. The miner lists used
    every ``issued`` row, so an expired one kept "evaluating" on the profile.
    """
    seeded = await _seed(maker)
    await _issue_active_ticket(
        maker, seeded.waiting, deadline=datetime.now(UTC) - timedelta(minutes=5)
    )
    _install(app, maker)

    profile = (
        await client.get(f"/api/v1/public/miners/{_hotkey('v17-waiting')}")
    ).json()
    assert [(row["status"], row["era_change"]) for row in profile["submissions"]] == [
        ("era_changed", _change(scored_bench_version=None))
    ]
    _, by_id = await _activity(client)
    assert by_id[str(seeded.waiting)]["status"] == "era_changed"


async def test_a_live_active_era_lease_keeps_the_waiting_label_on_both_surfaces(
    app: FastAPI,
    client: httpx.AsyncClient,
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Review r4576442515: Backroom and the miner page agree while a lease drains.

    A refused row a validator still holds keeps its waiting label for the
    miner, so the stuck list must not call it era-changed either.
    """
    seeded = await _seed(maker)
    await _issue_active_ticket(
        maker, seeded.waiting, deadline=datetime.now(UTC) + timedelta(hours=1)
    )
    _install(app, maker)

    profile = (
        await client.get(f"/api/v1/public/miners/{_hotkey('v17-waiting')}")
    ).json()
    assert [(row["status"], row["era_change"]) for row in profile["submissions"]] == [
        ("evaluating", None)
    ]
    _, by_id = await _activity(client)
    assert by_id[str(seeded.waiting)]["status"] != "era_changed"
    assert by_id[str(seeded.waiting)]["era_change"] is None

    stuck = (
        await client.get(
            "/api/v1/admin/validation-retries",
            params={"generation": "all"},
            headers=_HEADERS,
        )
    ).json()
    row = next(
        item for item in stuck["submissions"] if item["agent_id"] == str(seeded.waiting)
    )
    assert row["retry_state"] == "running"
    assert row["era_change"] is None
    assert "Era changed" not in (row["blocking_reason"] or "")
    # Unrelated rows on the same list keep their era-changed reading.
    dead = next(
        item
        for item in stuck["submissions"]
        if item["agent_id"] == str(seeded.dead_retry)
    )
    assert dead["era_change"] == _change(scored_bench_version=None)


async def test_readiness_defers_to_the_miner_projection_for_a_retired_waiting_row(
    app: FastAPI,
    client: httpx.AsyncClient,
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Review r4605892617 follow-up: a retired waiting row reads ``retired``.

    The miner projection closes retired waiting rows before the era boundary,
    so readiness must not quote the era-changed label for one, even with no
    queue removal in force.
    """
    seeded = await _seed(maker)
    async with maker() as session, session.begin():
        session.add(
            SubmissionRetirement(
                retirement_id=uuid4(),
                agent_id=seeded.waiting,
                bench_version=_FROM,
                superseded_by_version=_ACTIVE,
                actor="operator",
                reason="superseded by a newer version from the same miner",
                expected_snapshot="ab" * 32,
                score_count=0,
                ticket_snapshot=[],
                created_at=_ACTIVATED_AT,
            )
        )
    _install(app, maker)

    async with maker() as session:
        agent = await session.get(Agent, seeded.waiting)
        assert agent is not None
        closed = await closed_submission_statuses(
            session, agents=[agent], active_version=_ACTIVE
        )
    assert closed[seeded.waiting].status == "retired"

    body = await _readiness(client, seeded.waiting)
    assert body["era_change"] is None, body
    assert _ERA_REASON not in body["blocking_reasons"]
    assert (
        f"historical submission is not admitted to the v{_ACTIVE} validator queue"
    ) in body["blocking_reasons"]

    # The plain waiting row with no close-out still quotes the miner's label.
    other = await _readiness(client, seeded.dead_retry)
    assert other["era_change"] is not None
    assert any("miners see" in r for r in other["blocking_reasons"])


async def _terminal_withdraw(session: AsyncSession, agent_id: UUID) -> None:
    """The TerminalFailureSweeper's close-out: an in-force active-era removal
    carrying an agent-attributable terminal failure code."""
    session.add(
        ValidatorQueueWithdrawal(
            withdrawal_id=uuid4(),
            agent_id=agent_id,
            bench_version=_ACTIVE,
            actor="platform:terminal-artifact-failure",
            reason="agent failed scoring on three validators",
            expected_snapshot="test",
            score_count=2,
            ticket_snapshot=[],
            terminal_failure_code="inference_allowance_exhausted",
            created_at=_AFTER + timedelta(hours=1),
        )
    )
    await session.flush()


async def test_closed_rows_never_appear_as_provisional_board_rows(
    app: FastAPI,
    client: httpx.AsyncClient,
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """SN-483 (comment 18121): a 2-of-3 row that can never finish is not a
    provisional score.

    marlowe v1's shape: current-era work with two active-version scores, then
    closed out by the terminal-failure sweeper. Its stored status stays
    ``evaluating``, which is exactly what the provisional overlay selected, so
    its partial composite read as the agent's score on the public board. An
    era-refused waiting row holding a stray active-version score is closed the
    same way. A same-owner row that can still finish keeps its provisional row.
    """
    from ditto.db.queries.scores import list_provisional_ledger

    seeded = await _seed(maker)
    async with maker() as session, session.begin():
        terminal = await _agent(
            session,
            name="v18-terminal",
            status=AgentStatus.EVALUATING,
            created_at=_AFTER,
        )
        await _scores(session, terminal, count=2, bench_version=_ACTIVE)
        await _terminal_withdraw(session, terminal)
        # A sibling of the same owner (same hotkey) that can still finish, so
        # the per-owner dedupe must not let the closed row hide it.
        sibling = await _agent(
            session,
            name="v18-terminal-sibling",
            status=AgentStatus.EVALUATING,
            created_at=_AFTER + timedelta(minutes=5),
        )
        sibling_row = await session.get(Agent, sibling)
        assert sibling_row is not None
        sibling_row.miner_hotkey = _hotkey("v18-terminal")
        await _scores(session, sibling, count=1, bench_version=_ACTIVE)
        # Era-refused waiting row that somehow holds one active-version score.
        await _scores(session, seeded.waiting, count=1, bench_version=_ACTIVE)
        # Ordinary open current-era work: still provisional.
        await _scores(session, seeded.current, count=2, bench_version=_ACTIVE)
    _install(app, maker)

    async with maker() as session:
        provisional = await list_provisional_ledger(session, bench_version=_ACTIVE)
    shown = {row.agent_id for row, _count in provisional}
    assert terminal not in shown
    assert seeded.waiting not in shown
    assert sibling in shown
    assert seeded.current in shown

    # A historical board judges close-out against the live active era too:
    # the v17 dead-retry row (two v17 scores, era_changed on v18) must not
    # come back in v17's provisional preview (r4637685436).
    async with maker() as session:
        historical = await list_provisional_ledger(session, bench_version=_FROM)
    assert seeded.dead_retry not in {row.agent_id for row, _count in historical}

    board = (await client.get("/api/v1/public/leaderboard")).json()
    board_ids = {entry["agent_id"] for entry in board["entries"]}
    assert str(terminal) not in board_ids
    assert str(seeded.waiting) not in board_ids
    assert str(sibling) in board_ids
    assert str(seeded.current) in board_ids

    # The miner surfaces already read the terminal row honestly.
    summary = (await client.get(f"/api/v1/public/agent/{terminal}/summary")).json()
    assert summary["status"] == "scoring_failed"


async def test_backroom_reports_the_status_the_miner_sees(
    app: FastAPI,
    client: httpx.AsyncClient,
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """SN-483: Backroom shows the miner-visible status beside the stored one,
    so a closed row is never read as live ``evaluating`` work."""
    seeded = await _seed(maker)
    async with maker() as session, session.begin():
        terminal = await _agent(
            session,
            name="v18-terminal",
            status=AgentStatus.EVALUATING,
            created_at=_AFTER,
        )
        await _scores(session, terminal, count=2, bench_version=_ACTIVE)
        await _terminal_withdraw(session, terminal)
    _install(app, maker)

    expected = {
        terminal: "scoring_failed",
        seeded.waiting: "era_changed",
        seeded.withdrawn: "not_queued",
        seeded.current: "evaluating",
    }
    for agent_id, public_status in expected.items():
        readiness = await _readiness(client, agent_id)
        assert readiness["status"] == "evaluating"
        assert readiness["public_status"] == public_status, agent_id

        row = (
            await client.get(
                f"/api/v1/admin/screening-submissions/{agent_id}", headers=_HEADERS
            )
        ).json()
        assert row["agent_status"] == "evaluating"
        assert row["public_status"] == public_status, agent_id

    listing = (
        await client.get(
            "/api/v1/admin/screening-submissions",
            params={"generation": "all", "agent_status": "evaluating"},
            headers=_HEADERS,
        )
    ).json()
    by_id = {item["agent_id"]: item for item in listing["items"]}
    for agent_id, public_status in expected.items():
        assert by_id[str(agent_id)]["public_status"] == public_status, agent_id
