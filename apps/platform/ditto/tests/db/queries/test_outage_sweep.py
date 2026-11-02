"""The post-outage sweep voids outage-tainted retests and nothing else.

Fixture: the 2026-10-31 Wonder v6 shape. One retest's grant had 40 of 70 chat
calls refused for exhausted credits (``upstream_http_402``) between 03:55 and
04:09; another validator's retest of a different seed ran clean. The first
validator's canonical score was accepted from the same grant.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.ticket_status import TicketStatus
from ditto.db.models import (
    Agent,
    AgentStatus,
    ConfirmationScore,
    ConfirmationScoreVoid,
    InferenceGrant,
    InferenceRequest,
    OutageSweepRun,
    Score,
    ValidatorTicket,
)
from ditto.db.queries import outage_sweep
from ditto.db.queries.outage_sweep import (
    SWEEP_ACTOR,
    SWEEP_FIRST_LOOKBACK,
    SWEEP_REASON,
    SWEEP_SETTLE_LAG,
    sweep_outage_voids,
)

pytestmark = pytest.mark.asyncio

V = 14
NOW = datetime(2026, 10, 31, 4, 20, tzinfo=UTC)
SWEEP_AT = NOW + timedelta(minutes=10)
HEO = "5HEouuDaDASnWdqDwYtwQATS5z916W9DyDAtK6WyB2jPRReb"
CF = "5CFtzzb4vym9eysfeF9cxxp6D7gksuUVTKYNq1mchnrMs118"
BAD_SEED = 2_639_827_674_349_253_378
GOOD_SEED = 4_656_752_738_245_642_784
OUTAGE_START = datetime(2026, 10, 31, 3, 55, tzinfo=UTC)
OUTAGE_END = datetime(2026, 10, 31, 4, 9, tzinfo=UTC)


def _grant(agent_id: UUID, validator: str, created_at: datetime) -> InferenceGrant:
    return InferenceGrant(
        grant_id=uuid4(),
        agent_id=agent_id,
        bench_version=V,
        validator_hotkey=validator,
        slot_id="slot-0",
        ticket_deadline=created_at + timedelta(hours=3),
        status="revoked",
        generation=1,
        allowed_models=["openai/gpt-oss-20b"],
        route_provider="openrouter",
        route_profile="openrouter-route-test-v1",
        request_budget=16384,
        token_budget=75_000_000,
        embedding_model="e",
        embedding_profile="openrouter-embedding-test-v1",
        embedding_provider="p",
        embedding_dimensions=768,
        embedding_request_budget=1000,
        embedding_token_budget=1_000_000,
        embedding_request_count=0,
        embedding_tokens=0,
        embedding_cost_microusd=0,
        embedding_active_requests=0,
        request_count=0,
        prompt_tokens=0,
        completion_tokens=0,
        cost_microusd=0,
        active_requests=0,
        expires_at=created_at + timedelta(hours=3),
        created_at=created_at,
        updated_at=created_at,
    )


def _call(
    grant: InferenceGrant,
    *,
    status: str,
    started_at: datetime,
    code: str | None = None,
) -> InferenceRequest:
    return InferenceRequest(
        grant_id=grant.grant_id,
        nonce=uuid4(),
        generation=1,
        status=status,
        request_kind="chat",
        model="openai/gpt-oss-20b",
        reserved_tokens=16,
        prompt_tokens=0,
        completion_tokens=0,
        cost_microusd=0,
        terminal_error_code=code,
        timed_out=False,
        started_at=started_at,
        completed_at=started_at,
    )


def _score(agent_id: UUID, validator: str, accepted_at: datetime) -> Score:
    return Score(
        agent_id=agent_id,
        validator_hotkey=validator,
        bench_version=V,
        run_id="canon-heo",
        seed=7,
        composite=0.61,
        tool_mean=0.6,
        memory_mean=0.6,
        median_ms=1000,
        n=40,
        generated_at=accepted_at,
        created_at=accepted_at,
        updated_at=accepted_at,
    )


async def seed_outage_fixture(session: AsyncSession) -> UUID:
    """One outage-tainted retest, one clean retest, one tainted canonical."""
    agent = Agent(
        agent_id=uuid4(),
        miner_hotkey="5HTUbRK5bN7md878oMKByDYp5chz2STqoyW8uwGG9c1vZdLy",
        name="wonder",
        sha256=uuid4().hex * 2,
        status=AgentStatus.SCORED,
        created_at=NOW - timedelta(hours=4),
    )
    bad = _grant(agent.agent_id, HEO, NOW - timedelta(minutes=30))
    good = _grant(agent.agent_id, CF, NOW - timedelta(minutes=12))
    span = (OUTAGE_END - OUTAGE_START) / 39
    async with session.begin():
        session.add(agent)
        await session.flush()
        session.add_all(
            [
                ValidatorTicket(
                    agent_id=agent.agent_id,
                    validator_hotkey=validator,
                    slot_id="slot-0",
                    status=TicketStatus.SCORED,
                    issued_at=NOW - timedelta(hours=1),
                    deadline=NOW + timedelta(hours=2),
                    bench_version=V,
                    attempt_count=2,
                )
                for validator in (HEO, CF)
            ]
        )
        await session.flush()
        session.add_all([bad, good])
        await session.flush()
        session.add_all(
            [
                *(
                    _call(bad, status="completed", started_at=bad.created_at)
                    for _ in range(30)
                ),
                *(
                    _call(
                        bad,
                        status="failed",
                        started_at=(
                            OUTAGE_END if index == 39 else OUTAGE_START + span * index
                        ),
                        code="upstream_http_402",
                    )
                    for index in range(40)
                ),
                # A non-outage failure before the window does not widen it.
                _call(
                    bad,
                    status="failed",
                    started_at=bad.created_at,
                    code="agent_timeout",
                ),
                *(
                    _call(good, status="completed", started_at=good.created_at)
                    for _ in range(60)
                ),
                _call(
                    good,
                    status="failed",
                    started_at=good.created_at,
                    code="upstream_http_402",
                ),
                ConfirmationScore(
                    agent_id=agent.agent_id,
                    validator_hotkey=HEO,
                    bench_version=V,
                    seed=BAD_SEED,
                    composite=0.651813,
                    run_id="fd227604",
                    created_at=NOW - timedelta(minutes=8),
                ),
                ConfirmationScore(
                    agent_id=agent.agent_id,
                    validator_hotkey=CF,
                    bench_version=V,
                    seed=GOOD_SEED,
                    composite=0.919667,
                    run_id="351dd771",
                    created_at=NOW - timedelta(minutes=2),
                ),
                _score(agent.agent_id, HEO, NOW - timedelta(minutes=5)),
            ]
        )
    return agent.agent_id


async def _sweep(
    session_maker: async_sessionmaker[AsyncSession], at: datetime, **kwargs: object
) -> OutageSweepRun:
    async with session_maker() as session, session.begin():
        run = await sweep_outage_voids(session, now=at, bench_version=V, **kwargs)  # type: ignore[arg-type]
    assert run is not None
    return run


async def test_sweep_voids_the_tainted_retest_with_its_window_and_is_idempotent(
    session_maker: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    agent_id = await seed_outage_fixture(session)

    first = await _sweep(session_maker, SWEEP_AT)

    # The first run walks the active version's whole history (backfill).
    async with session_maker() as read:
        earliest = await read.scalar(
            select(func.min(ConfirmationScore.created_at)).where(
                ConfirmationScore.bench_version == V
            )
        )
    assert earliest is not None
    assert first.watermark_from == min(
        SWEEP_AT - SWEEP_FIRST_LOOKBACK, earliest - timedelta(microseconds=1)
    )
    assert first.watermark_to == SWEEP_AT - SWEEP_SETTLE_LAG
    assert first.watermark_to_key is None
    assert (first.examined_count, first.voided_count, first.error_count) == (2, 1, 0)
    async with session_maker() as read:
        voids = list(await read.scalars(select(ConfirmationScoreVoid)))
    assert len(voids) == 1
    void = voids[0]
    assert (void.agent_id, void.validator_hotkey, void.seed) == (
        agent_id,
        HEO,
        BAD_SEED,
    )
    assert (void.run_id, void.composite) == ("fd227604", 0.651813)
    assert (void.source, void.actor, void.reason) == (
        "outage_sweep",
        SWEEP_ACTOR,
        SWEEP_REASON,
    )
    assert void.outage_started_at == OUTAGE_START
    assert void.outage_ended_at == OUTAGE_END
    assert void.evidence["outage_failed_requests"] == 40
    # The clean retest is untouched.
    assert not any(v.seed == GOOD_SEED for v in voids)

    assert first.voids == [
        {
            "void_id": str(void.void_id),
            "agent_id": str(agent_id),
            "validator_hotkey": HEO,
            "seed": str(BAD_SEED),
            "void_generation": 0,
            "run_id": "fd227604",
            "composite": 0.651813,
            "accepted_at": (NOW - timedelta(minutes=8)).isoformat(),
            "grant_id": void.evidence["grant_id"],
            "chat_requests": 71,
            "outage_failed_requests": 40,
            "outage_started_at": OUTAGE_START.isoformat(),
            "outage_ended_at": OUTAGE_END.isoformat(),
        }
    ]
    assert [w["sample"] for w in first.outage_windows] == ["confirmation"]
    assert first.outage_windows[0]["started_at"] == OUTAGE_START.isoformat()
    # The canonical score from the same grant is flagged, never replaced.
    (flag,) = first.canonical_flagged
    assert (flag["agent_id"], flag["validator_hotkey"], flag["run_id"]) == (
        str(agent_id),
        HEO,
        "canon-heo",
    )
    assert flag["outage_failed_requests"] == 40
    assert flag["failure_codes"] == {"agent_timeout": 1, "upstream_http_402": 40}
    async with session_maker() as read:
        composite = await read.scalar(
            select(Score.composite).where(Score.agent_id == agent_id)
        )
    assert composite == 0.61

    # The next run resumes from the watermark and finds nothing new.
    second = await _sweep(session_maker, SWEEP_AT + timedelta(minutes=5))
    assert second.watermark_from == first.watermark_to
    assert second.watermark_to == SWEEP_AT + timedelta(minutes=5) - SWEEP_SETTLE_LAG
    assert (second.examined_count, second.voided_count) == (0, 0)
    assert second.voids == [] and second.canonical_flagged == []
    async with session_maker() as read:
        assert await read.scalar(select(func.count(ConfirmationScoreVoid.void_id))) == 1
        assert await read.scalar(select(func.count(OutageSweepRun.sweep_id))) == 2


async def test_full_batch_resumes_after_the_last_examined_row(
    session_maker: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    await seed_outage_fixture(session)

    first = await _sweep(session_maker, SWEEP_AT, batch_limit=1)
    assert (first.examined_count, first.voided_count) == (1, 1)
    assert first.watermark_to == NOW - timedelta(minutes=8)
    assert first.watermark_to_key is not None
    assert first.watermark_to_key["seed"] == str(BAD_SEED)

    second = await _sweep(session_maker, SWEEP_AT, batch_limit=1)
    assert (second.examined_count, second.voided_count) == (1, 0)
    assert second.watermark_from == first.watermark_to
    assert second.watermark_to == NOW - timedelta(minutes=2)
    assert second.watermark_to_key is not None
    assert second.watermark_to_key["seed"] == str(GOOD_SEED)

    third = await _sweep(session_maker, SWEEP_AT, batch_limit=1)
    assert third.examined_count == 0
    assert third.watermark_to == SWEEP_AT - SWEEP_SETTLE_LAG
    assert third.watermark_to_key is None


async def test_one_rows_error_is_recorded_and_does_not_abort_the_sweep(
    session_maker: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    await seed_outage_fixture(session)
    real = outage_sweep.grant_outage_window
    calls = 0

    async def flaky(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient")
        return await real(*args, **kwargs)  # type: ignore[arg-type]

    with patch.object(outage_sweep, "grant_outage_window", flaky):
        run = await _sweep(session_maker, SWEEP_AT)

    assert (run.examined_count, run.voided_count, run.error_count) == (2, 0, 1)
    assert run.errors == [
        {
            "agent_id": run.errors[0]["agent_id"],
            "validator_hotkey": HEO,
            "seed": str(BAD_SEED),
            "void_generation": 0,
            "error": "RuntimeError",
        }
    ]
    # The canonical read after it still ran.
    assert len(run.canonical_flagged) == 1
    async with session_maker() as read:
        assert await read.scalar(select(func.count(ConfirmationScoreVoid.void_id))) == 0


async def test_first_sweep_backfills_the_active_versions_history(
    session_maker: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    """The 2026-10-31 outage predates the sweep: a first run days later still
    voids it, where a six-hour lookback would have missed it."""
    await seed_outage_fixture(session)

    late = await _sweep(session_maker, SWEEP_AT + timedelta(days=3))

    assert (late.examined_count, late.voided_count) == (2, 1)
    async with session_maker() as read:
        assert len(list(await read.scalars(select(ConfirmationScoreVoid)))) == 1

    narrow = await _sweep(session_maker, SWEEP_AT + timedelta(days=3, minutes=5))
    assert (narrow.examined_count, narrow.voided_count) == (0, 0)
