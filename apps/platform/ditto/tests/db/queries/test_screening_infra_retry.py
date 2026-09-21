"""Automatic bounded retry and the fleet breaker for infrastructure build failures.

Real Postgres, real claim transaction: the assertions are about what
``claim_screening_attempts`` does with persisted attempt history, never about
in-memory state, because the derivation must be identical on every worker and
after a restart (issue #475).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.db.models import (
    Agent,
    AgentStatus,
    ScreenedImageUpload,
    ScreenerNode,
    ScreeningAttempt,
    ScreeningQuarantine,
    ScreeningRetryOverride,
    SubmissionImageBuild,
    ValidatorQueueWithdrawal,
)
from ditto.db.queries.benchmark_rollout import active_bench_version
from ditto.db.queries.screening import (
    MAX_SCREENING_EXPIRIES,
    PROVIDER_BACKOFF_REASON_CODES,
    _inconclusive_attempt_count,
    claim_screening_attempts,
)
from ditto.db.queries.screening_infra_retry import (
    BREAKER_DISTINCT_AGENTS,
    BREAKER_HISTORY_LOOKBACK,
    BREAKER_OPEN_DURATION,
    BREAKER_PROBE_INTERVAL,
    BREAKER_WINDOW,
    INFRA_AUTO_RETRY_MAX_AGE,
    INFRA_AUTO_RETRY_MAX_STREAK,
    INFRA_AUTO_RETRY_REASON_CODES,
    INFRA_PLAN_MAX_CLAIMABLE,
    INFRA_RETRY_BASE_BACKOFF,
    INFRA_RETRY_JITTER_FRACTION,
    INFRA_RETRY_MAX_BACKOFF,
    failing_agents_query,
    infra_retry_delay,
    plan_infra_retries,
)
from ditto_screening_protocol import SCREENING_FLOOR_POLICY_VERSION

_SCREENER = "5GScreenerHotkeyForInfraRetryTests00000000000000000"
_CODE = INFRA_AUTO_RETRY_REASON_CODES[0]
_SECOND = timedelta(seconds=1)
_PROVIDER = "gcp"
_LANE = "buildkit"


def _id_with_jitter_unit(*, at_most: float) -> UUID:
    """A random attempt id whose jitter factor sits in the lowest ``at_most``."""
    while True:
        candidate = uuid4()
        low = 1 - INFRA_RETRY_JITTER_FRACTION
        span = 2 * INFRA_RETRY_JITTER_FRACTION
        factor = infra_retry_delay(1, candidate) / INFRA_RETRY_BASE_BACKOFF
        if (factor - low) / span <= at_most:
            return candidate


async def _seed_agent(
    session_maker: async_sessionmaker[AsyncSession],
    *,
    status: AgentStatus = AgentStatus.SCREENING_FAILED,
    created_at: datetime | None = None,
) -> UUID:
    agent_id = uuid4()
    async with session_maker() as session, session.begin():
        agent = Agent(
            agent_id=agent_id,
            miner_hotkey=f"5HK-infra-{agent_id.hex[:12]}",
            name=f"infra-{agent_id.hex[:8]}",
            sha256=uuid4().hex * 2,
            status=status,
            created_at=created_at or datetime.now(UTC) - timedelta(days=2),
        )
        agent.screening_policy_version = SCREENING_FLOOR_POLICY_VERSION
        session.add(agent)
    return agent_id


async def _add_attempt(
    session_maker: async_sessionmaker[AsyncSession],
    agent_id: UUID,
    *,
    status: str,
    started_at: datetime,
    finished_at: datetime | None,
    reason_code: str | None = None,
    attempt_id: UUID | None = None,
    provider: str | None = _PROVIDER,
    lane: str | None = _LANE,
    deadline: datetime | None = None,
    screener_hotkey: str = _SCREENER,
) -> UUID:
    attempt_id = attempt_id or uuid4()
    async with session_maker() as session, session.begin():
        session.add(
            ScreeningAttempt(
                attempt_id=attempt_id,
                agent_id=agent_id,
                screener_hotkey=screener_hotkey,
                policy_version=SCREENING_FLOOR_POLICY_VERSION,
                status=status,
                started_at=started_at,
                deadline=deadline or started_at + timedelta(minutes=70),
                finished_at=finished_at,
                reason_code=reason_code,
                failure_provider=provider if status == "failed" else None,
                failure_lane=lane if status == "failed" else None,
                public_reason="attempt",
            )
        )
    return attempt_id


async def _infra_failure(
    session_maker: async_sessionmaker[AsyncSession],
    agent_id: UUID,
    *,
    finished_at: datetime,
    attempt_id: UUID | None = None,
    provider: str | None = _PROVIDER,
    lane: str | None = _LANE,
) -> UUID:
    return await _add_attempt(
        session_maker,
        agent_id,
        status="failed",
        started_at=finished_at - timedelta(minutes=2),
        finished_at=finished_at,
        reason_code=_CODE,
        attempt_id=attempt_id,
        provider=provider,
        lane=lane,
    )


async def _failing_agent(
    session_maker: async_sessionmaker[AsyncSession],
    *,
    finished_at: datetime,
    attempt_id: UUID | None = None,
    provider: str | None = _PROVIDER,
    lane: str | None = _LANE,
) -> UUID:
    agent_id = await _seed_agent(session_maker)
    await _infra_failure(
        session_maker,
        agent_id,
        finished_at=finished_at,
        attempt_id=attempt_id,
        provider=provider,
        lane=lane,
    )
    return agent_id


async def _claim(
    session_maker: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
    limit: int = 10,
    hotkey: str = _SCREENER,
) -> list[UUID]:
    async with session_maker() as session, session.begin():
        claimed = await claim_screening_attempts(
            session,
            screener_hotkey=hotkey,
            now=now,
            ttl=timedelta(minutes=45),
            limit=limit,
        )
        return [agent.agent_id for agent, _, _ in claimed]


async def _plan(session_maker: async_sessionmaker[AsyncSession], *, now: datetime):
    async with session_maker() as session:
        return await plan_infra_retries(session, now=now)


async def _running(session_maker: async_sessionmaker[AsyncSession]) -> list[UUID]:
    async with session_maker() as session:
        return list(
            await session.scalars(
                select(ScreeningAttempt.agent_id).where(
                    ScreeningAttempt.status == "running"
                )
            )
        )


async def _verify_image(
    session_maker: async_sessionmaker[AsyncSession],
    agent_id: UUID,
    attempt_id: UUID,
    *,
    hotkey: str = _SCREENER,
) -> None:
    """The verified screened-image upload a worker-built passing verdict carries."""
    async with session_maker() as session, session.begin():
        session.add(
            ScreenedImageUpload(
                image_upload_id=uuid4(),
                agent_id=agent_id,
                attempt_id=attempt_id,
                screener_hotkey=hotkey,
                storage_upload_id="upload",
                sha256="f" * 64,
                size_bytes=1,
                image_id="sha256:" + "f" * 64,
                image_ref="registry/agent:screened",
                status="verified",
                expires_at=datetime.now(UTC) + timedelta(days=1),
                verified_at=datetime.now(UTC),
            )
        )


async def _settle_probe(
    session_maker: async_sessionmaker[AsyncSession],
    agent_id: UUID,
    *,
    status: str,
    at: datetime,
    reason_code: str | None = None,
) -> None:
    """Finish the running attempt for ``agent_id`` the way a worker verdict would."""
    async with session_maker() as session, session.begin():
        attempt = await session.scalar(
            select(ScreeningAttempt).where(
                ScreeningAttempt.agent_id == agent_id,
                ScreeningAttempt.status == "running",
            )
        )
        assert attempt is not None
        agent = await session.get(Agent, agent_id)
        assert agent is not None
        attempt.status = status
        attempt.finished_at = at
        attempt.reason_code = reason_code
        if status == "failed":
            attempt.failure_provider = _PROVIDER
            attempt.failure_lane = _LANE
        agent.status = (
            AgentStatus.SCREENING_FAILED
            if status == "failed"
            else AgentStatus.EVALUATING
        )
        attempt_id = attempt.attempt_id
    if status == "passed":
        await _verify_image(session_maker, agent_id, attempt_id)


# --- backoff policy -------------------------------------------------------


def test_delay_doubles_from_the_base_and_never_exceeds_the_cap() -> None:
    jitter = INFRA_RETRY_JITTER_FRACTION
    for streak in range(1, 12):
        nominal = min(
            INFRA_RETRY_MAX_BACKOFF, INFRA_RETRY_BASE_BACKOFF * 2 ** (streak - 1)
        )
        for _ in range(50):
            delay = infra_retry_delay(streak, uuid4())
            assert delay <= INFRA_RETRY_MAX_BACKOFF
            assert nominal * (1 - jitter) <= delay <= nominal * (1 + jitter)
    # The first hold preserves the existing 10 minute provider hold as its base.
    assert timedelta(minutes=10) == INFRA_RETRY_BASE_BACKOFF
    assert timedelta(minutes=60) == INFRA_RETRY_MAX_BACKOFF
    # Growth is real: the slowest first retry is faster than the fastest third.
    assert INFRA_RETRY_BASE_BACKOFF * (1 + jitter) < INFRA_RETRY_BASE_BACKOFF * 4 * (
        1 - jitter
    )
    # A large streak cannot overflow.
    assert infra_retry_delay(10_000, uuid4()) <= INFRA_RETRY_MAX_BACKOFF


def test_jitter_is_deterministic_bounded_and_spread() -> None:
    attempt_id = uuid4()
    assert infra_retry_delay(2, attempt_id) == infra_retry_delay(2, attempt_id)
    delays = {infra_retry_delay(1, uuid4()) for _ in range(200)}
    assert len(delays) > 100
    assert min(delays) >= INFRA_RETRY_BASE_BACKOFF * (1 - INFRA_RETRY_JITTER_FRACTION)
    assert max(delays) <= INFRA_RETRY_BASE_BACKOFF * (1 + INFRA_RETRY_JITTER_FRACTION)


def test_jitter_does_not_depend_on_the_process_hash_seed() -> None:
    """Python's ``hash()`` is salted per process; a restart must not move deadlines."""
    attempt_id = uuid4()
    script = (
        "from uuid import UUID;"
        "from ditto.db.queries.screening_infra_retry import infra_retry_delay;"
        f"print(infra_retry_delay(3, UUID('{attempt_id}')).total_seconds())"
    )
    outputs = {
        subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
        ).stdout
        for seed in ("1", "2", "random")
    }
    assert outputs == {f"{infra_retry_delay(3, attempt_id).total_seconds()}\n"}


def test_infra_code_is_split_from_the_park_cap_tuple() -> None:
    assert _CODE == "docker-build-infrastructure"
    assert _CODE not in PROVIDER_BACKOFF_REASON_CODES


# --- per-artifact retry ---------------------------------------------------


async def test_infra_failure_is_held_for_its_backoff_then_retried(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    attempt_id = uuid4()
    delay = infra_retry_delay(1, attempt_id)
    failed_at = now - timedelta(minutes=30)
    agent_id = await _failing_agent(
        session_maker, finished_at=failed_at, attempt_id=attempt_id
    )

    assert await _claim(session_maker, now=failed_at + delay - _SECOND) == []
    assert await _claim(session_maker, now=failed_at + delay + _SECOND) == [agent_id]

    async with session_maker() as session:
        agent = await session.get(Agent, agent_id)
        assert agent is not None
        assert agent.status == AgentStatus.SCREENING
    assert await _running(session_maker) == [agent_id]


async def test_streak_doubles_per_consecutive_failure_and_caps(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    agent_id = await _seed_agent(session_maker)
    failures = INFRA_AUTO_RETRY_MAX_STREAK - 1
    latest = uuid4()
    for index in range(failures):
        finished = now - timedelta(hours=4) + timedelta(hours=index * 0.25)
        await _infra_failure(
            session_maker,
            agent_id,
            finished_at=finished,
            attempt_id=latest if index == failures - 1 else None,
        )

    decision = (await _plan(session_maker, now=now)).decisions[agent_id]

    assert decision.streak == failures
    assert decision.backoff_until == decision.failed_at + infra_retry_delay(
        failures, latest
    )
    assert decision.backoff_until - decision.failed_at <= INFRA_RETRY_MAX_BACKOFF
    # Deep into the streak the hold has reached the (jittered) cap.
    assert decision.backoff_until - decision.failed_at >= INFRA_RETRY_MAX_BACKOFF * (
        1 - INFRA_RETRY_JITTER_FRACTION
    )


async def test_backoff_and_streak_are_recomputed_identically_after_restart(
    session_maker: async_sessionmaker[AsyncSession], engine
) -> None:
    now = datetime.now(UTC)
    agent_id = await _seed_agent(session_maker)
    for index in range(4):
        await _infra_failure(
            session_maker,
            agent_id,
            finished_at=now - timedelta(hours=4) + timedelta(hours=index),
        )
    before = (await _plan(session_maker, now=now)).decisions[agent_id]

    # A restarted Platform shares nothing but the database.
    restarted = async_sessionmaker(engine, expire_on_commit=False)
    after = (await _plan(restarted, now=now)).decisions[agent_id]

    assert after == before
    assert after.streak == 4


async def test_operator_manual_retry_resets_the_streak(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    agent_id = await _seed_agent(session_maker)
    first = await _infra_failure(
        session_maker, agent_id, finished_at=now - timedelta(hours=5)
    )
    await _infra_failure(session_maker, agent_id, finished_at=now - timedelta(hours=4))
    third = await _infra_failure(
        session_maker, agent_id, finished_at=now - timedelta(hours=3)
    )
    assert (await _plan(session_maker, now=now)).decisions[agent_id].streak == 3

    retry_at = now - timedelta(hours=2, minutes=30)
    async with session_maker() as session, session.begin():
        session.add(
            ScreeningRetryOverride(
                override_id=uuid4(),
                agent_id=agent_id,
                attempt_id=third,
                artifact_sha256="a" * 64,
                expected_score_count=0,
                force_full_review=False,
                reason="operator retry",
                actor="operator@example.com",
                created_at=retry_at,
            )
        )
    # The override authorizes the manual path: it is no longer auto-held.
    assert agent_id not in (await _plan(session_maker, now=now)).decisions
    assert first != third

    # The manual retry failed on infrastructure again: the streak starts over.
    await _infra_failure(session_maker, agent_id, finished_at=now - timedelta(hours=2))
    assert (await _plan(session_maker, now=now)).decisions[agent_id].streak == 1


async def test_operator_quarantine_release_resets_the_streak(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    agent_id = await _seed_agent(session_maker)
    await _infra_failure(session_maker, agent_id, finished_at=now - timedelta(hours=6))
    await _infra_failure(session_maker, agent_id, finished_at=now - timedelta(hours=5))
    old = await _add_attempt(
        session_maker,
        agent_id,
        status="quarantined",
        started_at=now - timedelta(days=1),
        finished_at=now - timedelta(days=1),
        reason_code="agentic-source-review-tripwire",
    )
    async with session_maker() as session, session.begin():
        session.add(
            ScreeningQuarantine(
                quarantine_id=uuid4(),
                agent_id=agent_id,
                attempt_id=old,
                screener_hotkey=_SCREENER,
                policy_version=SCREENING_FLOOR_POLICY_VERSION,
                manifest_digest="d" * 64,
                reason_code="agentic-source-review-tripwire",
                status="resolved",
                resolved_at=now - timedelta(hours=4, minutes=30),
                resolved_by="operator",
                resolution="release",
                resolution_reason="operator cleared the hold",
            )
        )
    await _infra_failure(session_maker, agent_id, finished_at=now - timedelta(hours=3))

    # Only the failure after the release counts.
    assert (await _plan(session_maker, now=now)).decisions[agent_id].streak == 1


async def test_infra_failures_never_park_reject_or_quarantine_by_count(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    agent_id = await _seed_agent(session_maker)
    failures = MAX_SCREENING_EXPIRIES + 1
    assert failures < INFRA_AUTO_RETRY_MAX_STREAK
    for index in range(failures):
        await _infra_failure(
            session_maker,
            agent_id,
            finished_at=now - timedelta(hours=failures + 1 - index),
        )
    # A peer passed inside every lease window: the evidence that makes the
    # provider codes count. It must not make this code count.
    peer = await _seed_agent(session_maker, status=AgentStatus.SCORED)
    for index in range(failures):
        at = now - timedelta(hours=failures + 1 - index)
        await _add_attempt(
            session_maker,
            peer,
            status="passed",
            started_at=at - timedelta(minutes=20),
            finished_at=at,
        )

    async with session_maker() as session:
        assert await _inconclusive_attempt_count(session, agent_id=agent_id) == 0

    assert await _claim(session_maker, now=now) == [agent_id]

    async with session_maker() as session:
        agent = await session.get(Agent, agent_id)
        assert agent is not None
        assert agent.status == AgentStatus.SCREENING
        assert (
            await session.scalar(select(func.count()).select_from(ScreeningQuarantine))
            == 0
        )
        statuses = set(
            await session.scalars(
                select(ScreeningAttempt.status).where(
                    ScreeningAttempt.agent_id == agent_id
                )
            )
        )
        assert statuses == {"failed", "running"}


async def test_two_concurrent_claimers_start_exactly_one_attempt(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    agent_id = await _failing_agent(
        session_maker, finished_at=now - INFRA_RETRY_MAX_BACKOFF * 2
    )

    results = await asyncio.gather(
        _claim(session_maker, now=now), _claim(session_maker, now=now)
    )

    assert sorted(results) == [[], [agent_id]]
    assert await _running(session_maker) == [agent_id]


async def test_expired_probe_lease_stays_parked_for_the_operator(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """A stale lease is the existing expiry path, not an infrastructure retry."""
    now = datetime.now(UTC)
    agent_id = await _failing_agent(session_maker, finished_at=now - timedelta(hours=3))
    # The auto-retry started, then its worker vanished and the lease ran out.
    stale = await _add_attempt(
        session_maker,
        agent_id,
        status="running",
        started_at=now - timedelta(hours=2),
        finished_at=None,
        deadline=now - timedelta(hours=1),
    )
    async with session_maker() as session, session.begin():
        agent = await session.get(Agent, agent_id)
        assert agent is not None
        agent.status = AgentStatus.SCREENING

    assert await _claim(session_maker, now=now) == []

    async with session_maker() as session:
        attempt = await session.get(ScreeningAttempt, stale)
        agent = await session.get(Agent, agent_id)
        assert attempt is not None and agent is not None
        assert attempt.status == "expired"
        assert agent.status == AgentStatus.SCREENING_FAILED
    # No second active attempt was created and nothing auto-retries an expiry.
    assert await _running(session_maker) == []
    assert agent_id not in (await _plan(session_maker, now=now)).decisions


async def test_live_running_attempt_blocks_a_second_one(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    agent_id = await _failing_agent(session_maker, finished_at=now - timedelta(hours=3))
    assert await _claim(session_maker, now=now) == [agent_id]
    assert await _claim(session_maker, now=now + timedelta(minutes=10)) == []
    assert await _running(session_maker) == [agent_id]


# --- fleet breaker --------------------------------------------------------


async def _tripped_fleet(
    session_maker: async_sessionmaker[AsyncSession],
    *,
    t0: datetime,
    provider: str | None = _PROVIDER,
    lane: str | None = _LANE,
) -> list[UUID]:
    """``BREAKER_DISTINCT_AGENTS`` agents failing one minute apart from ``t0``.

    Short jitter draws keep every per-artifact backoff shorter than the open
    period, so the breaker (not the backoff) is what holds them.
    """
    agents = []
    step = BREAKER_WINDOW / (BREAKER_DISTINCT_AGENTS + 1)
    for index in range(BREAKER_DISTINCT_AGENTS):
        agents.append(
            await _failing_agent(
                session_maker,
                finished_at=t0 + step * index,
                attempt_id=_id_with_jitter_unit(at_most=0.25),
                provider=provider,
                lane=lane,
            )
        )
    return agents


def _opened_at(t0: datetime) -> datetime:
    return t0 + BREAKER_WINDOW / (BREAKER_DISTINCT_AGENTS + 1) * (
        BREAKER_DISTINCT_AGENTS - 1
    )


async def test_breaker_holds_retries_then_admits_one_probe_per_interval(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    t0 = datetime.now(UTC) - timedelta(hours=1)
    agents = await _tripped_fleet(session_maker, t0=t0)
    opened = _opened_at(t0)
    open_until = opened + BREAKER_OPEN_DURATION
    # Per-artifact backoffs are over, so only the breaker can hold them.
    held_at = open_until - _SECOND
    plan = await _plan(session_maker, now=held_at)
    assert {d.state for d in plan.decisions.values()} == {"breaker_held"}
    assert all(d.backoff_until < held_at for d in plan.decisions.values())
    assert await _claim(session_maker, now=held_at) == []

    probe_at = open_until + _SECOND
    probed = await _claim(session_maker, now=probe_at)
    assert len(probed) == 1 and probed[0] in agents
    # Same instant, later, and just short of the interval: still one probe.
    assert await _claim(session_maker, now=probe_at + _SECOND) == []
    assert (
        await _claim(session_maker, now=probe_at + BREAKER_PROBE_INTERVAL - _SECOND)
        == []
    )

    # The probe fails on the same signature: the breaker stays open and the next
    # probe is admitted one interval after the previous one started.
    failed_at = probe_at + timedelta(minutes=1)
    await _settle_probe(
        session_maker, probed[0], status="failed", at=failed_at, reason_code=_CODE
    )
    assert (
        await _claim(session_maker, now=probe_at + BREAKER_PROBE_INTERVAL - _SECOND)
        == []
    )
    second = await _claim(
        session_maker, now=probe_at + BREAKER_PROBE_INTERVAL + _SECOND
    )
    assert len(second) == 1 and second[0] != probed[0]


async def test_successful_probe_closes_the_breaker(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    t0 = datetime.now(UTC) - timedelta(hours=1)
    agents = await _tripped_fleet(session_maker, t0=t0)
    probe_at = _opened_at(t0) + BREAKER_OPEN_DURATION + _SECOND
    (probe,) = await _claim(session_maker, now=probe_at)

    await _settle_probe(
        session_maker, probe, status="passed", at=probe_at + timedelta(minutes=1)
    )

    after = probe_at + timedelta(minutes=2)
    plan = await _plan(session_maker, now=after)
    assert all(not breaker.open for breaker in plan.breakers.values())
    # Everyone still parked is now simply due; no probe pacing applies.
    remaining = sorted(set(agents) - {probe})
    assert sorted(await _claim(session_maker, now=after)) == remaining


async def test_concurrent_claimers_select_exactly_one_probe(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    t0 = datetime.now(UTC) - timedelta(hours=1)
    await _tripped_fleet(session_maker, t0=t0)
    probe_at = _opened_at(t0) + BREAKER_OPEN_DURATION + _SECOND

    results = await asyncio.gather(
        *(_claim(session_maker, now=probe_at) for _ in range(4))
    )

    assert sorted(len(rows) for rows in results) == [0, 0, 0, 1]
    assert len(await _running(session_maker)) == 1


async def test_breaker_state_is_identical_after_restart(
    session_maker: async_sessionmaker[AsyncSession], engine
) -> None:
    t0 = datetime.now(UTC) - timedelta(hours=1)
    await _tripped_fleet(session_maker, t0=t0)
    now = _opened_at(t0) + BREAKER_OPEN_DURATION + _SECOND
    before = await _plan(session_maker, now=now)
    after = await _plan(async_sessionmaker(engine, expire_on_commit=False), now=now)
    assert after == before
    assert any(breaker.open for breaker in after.breakers.values())


async def test_mixed_queue_healthy_and_other_signatures_keep_flowing(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    t0 = datetime.now(UTC) - timedelta(hours=1)
    held = await _tripped_fleet(session_maker, t0=t0)
    now = _opened_at(t0) + BREAKER_OPEN_DURATION - _SECOND
    # A healthy fresh upload, and an agent failing on a different lane.
    healthy = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
    other_lane = await _failing_agent(
        session_maker,
        finished_at=t0,
        attempt_id=_id_with_jitter_unit(at_most=0.25),
        lane="other-lane",
    )

    claimed = await _claim(session_maker, now=now)

    assert sorted(claimed) == sorted([healthy, other_lane])
    assert not set(claimed) & set(held)


async def test_breaker_does_not_merge_unknown_or_different_lanes(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    at = now - timedelta(hours=1)
    # Three agents, one shared reason code, three different signatures.
    await _failing_agent(session_maker, finished_at=at, lane="buildkit")
    await _failing_agent(session_maker, finished_at=at, lane="build")
    await _failing_agent(session_maker, finished_at=at, provider=None, lane=None)

    plan = await _plan(session_maker, now=now)

    assert len(plan.decisions) == BREAKER_DISTINCT_AGENTS
    assert not any(breaker.open for breaker in plan.breakers.values())
    assert len({d.signature for d in plan.decisions.values()}) == 3


async def test_breaker_keys_on_reason_code_alone_without_metadata(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    at = now - timedelta(hours=1)
    for _ in range(BREAKER_DISTINCT_AGENTS):
        await _failing_agent(session_maker, finished_at=at, provider=None, lane=None)

    plan = await _plan(session_maker, now=now)

    (breaker,) = plan.breakers.values()
    assert breaker.open
    assert breaker.signature == (_CODE, None, None)


async def test_breaker_needs_distinct_agents_inside_the_window(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    t0 = now - timedelta(hours=1)
    # Same agent failing repeatedly is one agent, not a fleet.
    repeat = await _seed_agent(session_maker)
    for index in range(BREAKER_DISTINCT_AGENTS + 1):
        await _infra_failure(
            session_maker,
            repeat,
            finished_at=t0 - timedelta(hours=2) + timedelta(seconds=index),
        )
    # Distinct agents spread wider than the window do not trip it either.
    spread = BREAKER_WINDOW / (BREAKER_DISTINCT_AGENTS - 1) + _SECOND
    for index in range(BREAKER_DISTINCT_AGENTS):
        await _failing_agent(session_maker, finished_at=t0 + spread * index)

    plan = await _plan(session_maker, now=now)

    assert not any(breaker.open for breaker in plan.breakers.values())


async def test_operator_manual_retry_bypasses_and_can_close_the_breaker(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    t0 = datetime.now(UTC) - timedelta(hours=1)
    agents = await _tripped_fleet(session_maker, t0=t0)
    now = _opened_at(t0) + BREAKER_OPEN_DURATION - _SECOND
    target = agents[0]
    async with session_maker() as session, session.begin():
        latest = await session.scalar(
            select(ScreeningAttempt.attempt_id).where(
                ScreeningAttempt.agent_id == target
            )
        )
        assert latest is not None
        session.add(
            ScreeningRetryOverride(
                override_id=uuid4(),
                agent_id=target,
                attempt_id=latest,
                artifact_sha256="b" * 64,
                expected_score_count=0,
                force_full_review=False,
                reason="operator retry",
                actor="operator@example.com",
                created_at=now - timedelta(minutes=1),
            )
        )

    # Held while open, yet the explicit operator retry still runs.
    assert await _claim(session_maker, now=now) == [target]
    await _settle_probe(
        session_maker, target, status="passed", at=now + timedelta(minutes=1)
    )
    plan = await _plan(session_maker, now=now + timedelta(minutes=2))
    assert all(not breaker.open for breaker in plan.breakers.values())


# --- bounds on automatic retries -------------------------------------------


async def test_long_parked_agent_is_not_retried_automatically(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    old = await _failing_agent(
        session_maker, finished_at=now - INFRA_AUTO_RETRY_MAX_AGE - _SECOND
    )
    recent = await _failing_agent(
        session_maker, finished_at=now - INFRA_AUTO_RETRY_MAX_AGE + timedelta(hours=1)
    )

    assert old not in (await _plan(session_maker, now=now)).decisions
    assert await _claim(session_maker, now=now) == [recent]
    async with session_maker() as session:
        parked = await session.get(Agent, old)
        assert parked is not None
        assert parked.status == AgentStatus.SCREENING_FAILED
        assert (
            await session.scalar(select(func.count()).select_from(ScreeningQuarantine))
            == 0
        )


async def _capped_agent(
    session_maker: async_sessionmaker[AsyncSession], *, now: datetime
) -> UUID:
    agent_id = await _seed_agent(session_maker)
    for index in range(INFRA_AUTO_RETRY_MAX_STREAK):
        await _infra_failure(
            session_maker,
            agent_id,
            finished_at=now - timedelta(hours=6) + timedelta(minutes=30 * index),
        )
    return agent_id


async def test_streak_cap_hands_the_agent_to_the_operator_without_a_verdict(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    agent_id = await _capped_agent(session_maker, now=now)

    decision = (await _plan(session_maker, now=now)).decisions[agent_id]
    assert decision.streak == INFRA_AUTO_RETRY_MAX_STREAK
    assert decision.state == "capped"
    # Long after any backoff, it is still not claimed.
    assert await _claim(session_maker, now=now + timedelta(hours=3)) == []

    async with session_maker() as session:
        agent = await session.get(Agent, agent_id)
        assert agent is not None
        assert agent.status == AgentStatus.SCREENING_FAILED
        assert (
            await session.scalar(select(func.count()).select_from(ScreeningQuarantine))
            == 0
        )
        assert await _inconclusive_attempt_count(session, agent_id=agent_id) == 0
        statuses = set(
            await session.scalars(
                select(ScreeningAttempt.status).where(
                    ScreeningAttempt.agent_id == agent_id
                )
            )
        )
        assert statuses == {"failed"}


async def test_operator_retry_lifts_the_streak_cap(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    agent_id = await _capped_agent(session_maker, now=now)
    async with session_maker() as session, session.begin():
        latest = await session.scalar(
            select(ScreeningAttempt.attempt_id)
            .where(ScreeningAttempt.agent_id == agent_id)
            .order_by(ScreeningAttempt.started_at.desc())
            .limit(1)
        )
        assert latest is not None
        session.add(
            ScreeningRetryOverride(
                override_id=uuid4(),
                agent_id=agent_id,
                attempt_id=latest,
                artifact_sha256="c" * 64,
                expected_score_count=0,
                force_full_review=False,
                reason="operator retry",
                actor="operator@example.com",
                created_at=now - timedelta(minutes=30),
            )
        )

    assert await _claim(session_maker, now=now) == [agent_id]
    await _settle_probe(
        session_maker,
        agent_id,
        status="failed",
        at=now + timedelta(minutes=5),
        reason_code=_CODE,
    )
    # The streak started over, so the agent is auto-retried again, not capped.
    decision = (await _plan(session_maker, now=now + timedelta(minutes=6))).decisions[
        agent_id
    ]
    assert decision.streak == 1
    assert decision.state == "backoff"


async def test_quarantine_release_lifts_the_streak_cap(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    agent_id = await _capped_agent(session_maker, now=now)
    old = await _add_attempt(
        session_maker,
        agent_id,
        status="quarantined",
        started_at=now - timedelta(days=3),
        finished_at=now - timedelta(days=3),
        reason_code="agentic-source-review-tripwire",
    )
    async with session_maker() as session, session.begin():
        session.add(
            ScreeningQuarantine(
                quarantine_id=uuid4(),
                agent_id=agent_id,
                attempt_id=old,
                screener_hotkey=_SCREENER,
                policy_version=SCREENING_FLOOR_POLICY_VERSION,
                manifest_digest="d" * 64,
                reason_code="agentic-source-review-tripwire",
                status="resolved",
                resolved_at=now - timedelta(minutes=5),
                resolved_by="operator",
                resolution="release",
                resolution_reason="operator cleared the hold",
            )
        )

    decision = (await _plan(session_maker, now=now)).decisions[agent_id]
    assert decision.streak == 0
    assert decision.state != "capped"
    assert await _claim(session_maker, now=now + timedelta(hours=1)) == [agent_id]


async def test_agent_withdrawn_from_the_active_era_is_not_retried(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    withdrawn = await _failing_agent(
        session_maker, finished_at=now - timedelta(hours=1)
    )
    kept = await _failing_agent(session_maker, finished_at=now - timedelta(hours=1))
    async with session_maker() as session, session.begin():
        session.add(
            ValidatorQueueWithdrawal(
                withdrawal_id=uuid4(),
                agent_id=withdrawn,
                bench_version=await active_bench_version(session),
                actor="operator@example.com",
                reason="withdrawn from the era",
                expected_snapshot="x",
                score_count=0,
                ticket_snapshot=[],
                created_at=now - timedelta(days=1),
            )
        )

    assert await _claim(session_maker, now=now) == [kept]


async def test_planned_id_list_has_a_safety_bound_longest_waiting_first(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    bound = 5
    agents = [
        await _failing_agent(
            session_maker,
            # Spaced past the breaker window so only the id-list bound is tested.
            finished_at=now - timedelta(hours=20) + BREAKER_WINDOW * 1.5 * index,
        )
        for index in range(bound + 3)
    ]
    async with session_maker() as session:
        bounded = await plan_infra_retries(session, now=now, claimable_limit=bound)
        default = await plan_infra_retries(session, now=now)
    assert bounded.claimable_agent_ids == agents[:bound]
    # The default is the large safety bound, not a function of any claim limit.
    assert default.claimable_limit == INFRA_PLAN_MAX_CLAIMABLE >= 100
    assert default.claimable_agent_ids == agents


async def test_blocked_agents_cannot_starve_an_eligible_retry(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Oldest-first blocked agents must not fill the id list ahead of a healthy one."""
    now = datetime.now(UTC)
    limit = 1
    blocked = []
    for index in range(limit * 4 + 3):  # more than the old limit-times-4 window
        agent_id = await _failing_agent(
            session_maker,
            finished_at=now - timedelta(hours=20) + BREAKER_WINDOW * 1.5 * index,
        )
        blocked.append(agent_id)
    healthy = await _failing_agent(session_maker, finished_at=now - timedelta(hours=1))
    async with session_maker() as session, session.begin():
        version = await active_bench_version(session)
        for agent_id in blocked:
            session.add(
                ValidatorQueueWithdrawal(
                    withdrawal_id=uuid4(),
                    agent_id=agent_id,
                    bench_version=version,
                    actor="operator@example.com",
                    reason="withdrawn from the era",
                    expected_snapshot="x",
                    score_count=0,
                    ticket_snapshot=[],
                    created_at=now - timedelta(days=1),
                )
            )

    assert await _claim(session_maker, now=now, limit=limit) == [healthy]


async def test_partial_index_predicate_matches_the_scan_query(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The breaker scan must stay on ``screening_attempts_infra_failed_idx``.

    If the query's predicate stops implying the index predicate, Postgres falls
    back to a sequential scan under the global claim lock. Seq scans are
    disabled so any plan that can use the index must use it.
    """
    from sqlalchemy.dialects import postgresql

    now = datetime.now(UTC)
    await _failing_agent(session_maker, finished_at=now - timedelta(hours=1))
    sql = str(
        failing_agents_query(now - BREAKER_HISTORY_LOOKBACK).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    async with session_maker() as session, session.begin():
        await session.execute(text("SET LOCAL enable_seqscan = off"))
        plan = "\n".join(
            row[0] for row in await session.execute(text("EXPLAIN " + sql))
        )
    assert "screening_attempts_infra_failed_idx" in plan, plan


def test_partial_index_covers_exactly_one_reason_code() -> None:
    assert len(INFRA_AUTO_RETRY_REASON_CODES) == 1, (
        "The partial index screening_attempts_infra_failed_idx (models.py, its "
        "migration, and screening_infra_retry._infra_failure_filters) names one "
        "reason code; update all three together with INFRA_AUTO_RETRY_REASON_CODES"
    )


async def test_agent_only_plan_skips_the_fleet_history_query(
    session_maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    from ditto.db.queries import screening_infra_retry as module

    calls: list[datetime] = []
    real = module.failing_agents_query

    def spy(cutoff: datetime):  # noqa: ANN202
        calls.append(cutoff)
        return real(cutoff)

    monkeypatch.setattr(module, "failing_agents_query", spy)
    now = datetime.now(UTC)
    agent_id = await _failing_agent(
        session_maker, finished_at=now - timedelta(minutes=1)
    )

    async with session_maker() as session:
        plan = await plan_infra_retries(
            session, now=now, agent_ids=[agent_id], fleet_scan=False
        )
    assert calls == []
    assert plan.decisions[agent_id].state == "backoff"
    assert plan.breakers == {}

    async with session_maker() as session:
        await plan_infra_retries(session, now=now, agent_ids=[agent_id])
    assert len(calls) == 1


# --- lane-aware breaker ------------------------------------------------------

_HETZNER_HOTKEY = "5GHetznerNodeHotkeyForInfraRetryTests000000000000"
_HETZNER = "hetzner"


async def _enroll_node(
    session_maker: async_sessionmaker[AsyncSession], *, hotkey: str, provider: str
) -> None:
    async with session_maker() as session, session.begin():
        session.add(
            ScreenerNode(
                environment="prod",
                node_id=f"node-{provider}",
                provider=provider,
                provider_resource_id=f"resource-{provider}",
                screener_hotkey=hotkey,
                token_hash="a" * 64,
                token_expires_at=datetime.now(UTC) + timedelta(days=1),
            )
        )


async def _follow_up(
    session_maker: async_sessionmaker[AsyncSession],
    agent_id: UUID,
    *,
    status: str,
    at: datetime,
    hotkey: str,
    provider: str | None = None,
    lane: str | None = None,
    reason_code: str | None = None,
    with_image: bool = True,
) -> UUID:
    """A retry of ``agent_id`` that started at ``at`` on the worker ``hotkey``."""
    attempt_id = await _add_attempt(
        session_maker,
        agent_id,
        status=status,
        started_at=at,
        finished_at=at + timedelta(minutes=1),
        reason_code=reason_code or (_CODE if status == "failed" else None),
        provider=provider,
        lane=lane,
        screener_hotkey=hotkey,
    )
    if status == "passed" and with_image:
        await _verify_image(session_maker, agent_id, attempt_id, hotkey=hotkey)
    return attempt_id


async def _gcp_breaker(session_maker, *, now: datetime):
    plan = await _plan(session_maker, now=now)
    return plan.breakers[(_CODE, _PROVIDER, _LANE)]


async def test_cross_lane_success_does_not_close_the_breaker_or_use_its_probe(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _enroll_node(session_maker, hotkey=_HETZNER_HOTKEY, provider=_HETZNER)
    t0 = datetime.now(UTC) - timedelta(hours=1)
    agents = await _tripped_fleet(session_maker, t0=t0)
    opened = _opened_at(t0)
    # One failed agent is retried on Hetzner and passes there.
    await _follow_up(
        session_maker,
        agents[0],
        status="passed",
        at=opened + _SECOND,
        hotkey=_HETZNER_HOTKEY,
    )
    now = opened + BREAKER_OPEN_DURATION + _SECOND

    breaker = await _gcp_breaker(session_maker, now=now)

    assert breaker.open
    assert breaker.last_probe_at is None
    # The single GCE probe is still unconsumed and the rest stay held.
    assert len(await _claim(session_maker, now=now)) == 1


async def test_cross_lane_failure_belongs_to_its_own_signature(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _enroll_node(session_maker, hotkey=_HETZNER_HOTKEY, provider=_HETZNER)
    t0 = datetime.now(UTC) - timedelta(hours=1)
    agents = await _tripped_fleet(session_maker, t0=t0)
    opened = _opened_at(t0)
    await _follow_up(
        session_maker,
        agents[0],
        status="failed",
        at=opened + _SECOND,
        hotkey=_HETZNER_HOTKEY,
        provider=_HETZNER,
        lane=_LANE,
    )
    now = opened + BREAKER_OPEN_DURATION + _SECOND

    plan = await _plan(session_maker, now=now)

    gcp = plan.breakers[(_CODE, _PROVIDER, _LANE)]
    assert gcp.open
    assert gcp.last_probe_at is None
    # The Hetzner failure is one failure of its own signature: no trip.
    assert not plan.breakers[(_CODE, _HETZNER, _LANE)].open
    assert len(await _claim(session_maker, now=now)) == 1


async def test_same_lane_success_closes_and_other_lane_success_does_not(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _enroll_node(session_maker, hotkey=_HETZNER_HOTKEY, provider=_HETZNER)
    t0 = datetime.now(UTC) - timedelta(hours=1)
    agents = await _tripped_fleet(session_maker, t0=t0, provider=_HETZNER, lane=_LANE)
    opened = _opened_at(t0)
    # A pass on the legacy (GCE) fleet says nothing about the Hetzner lane.
    await _follow_up(
        session_maker, agents[0], status="passed", at=opened + _SECOND, hotkey=_SCREENER
    )
    now = opened + BREAKER_OPEN_DURATION + _SECOND
    plan = await _plan(session_maker, now=now)
    assert plan.breakers[(_CODE, _HETZNER, _LANE)].open

    # A pass on the Hetzner node closes it.
    await _follow_up(
        session_maker,
        agents[1],
        status="passed",
        at=now,
        hotkey=_HETZNER_HOTKEY,
    )
    plan = await _plan(session_maker, now=now + timedelta(minutes=2))
    assert not plan.breakers[(_CODE, _HETZNER, _LANE)].open


async def test_follow_up_through_the_image_build_lane_is_a_probe_not_a_recovery(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """A retry built by the Platform image-build lane still paces the probes.

    Placement (the claiming worker) is what a probe is; the lane it then used is
    not known at claim time. But a pass through the image-build lane is no proof
    the local build recovered, so it does not close the breaker.
    """
    t0 = datetime.now(UTC) - timedelta(hours=1)
    agents = await _tripped_fleet(session_maker, t0=t0)
    opened = _opened_at(t0)
    attempt = await _follow_up(
        session_maker, agents[0], status="passed", at=opened + _SECOND, hotkey=_SCREENER
    )
    async with session_maker() as session, session.begin():
        session.add(
            SubmissionImageBuild(
                build_id=uuid4(),
                agent_id=agents[0],
                attempt_id=attempt,
                environment="prod",
                artifact_sha256="e" * 64,
                image_ref=f"ditto-screen/{agents[0]}-{attempt}:latest",
                output_key="builds/x",
            )
        )
    now = opened + BREAKER_OPEN_DURATION + _SECOND

    breaker = await _gcp_breaker(session_maker, now=now)

    assert breaker.open
    assert breaker.last_probe_at == opened + _SECOND
    assert breaker.next_probe_at == max(
        opened + BREAKER_OPEN_DURATION, opened + _SECOND + BREAKER_PROBE_INTERVAL
    )


async def test_other_lane_worker_claims_held_retries_without_using_the_probe(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _enroll_node(session_maker, hotkey=_HETZNER_HOTKEY, provider=_HETZNER)
    t0 = datetime.now(UTC) - timedelta(hours=1)
    agents = await _tripped_fleet(session_maker, t0=t0)
    opened = _opened_at(t0)
    held_at = opened + BREAKER_OPEN_DURATION - _SECOND
    # The breaker holds retries on the failing GCE lane...
    assert await _claim(session_maker, now=held_at) == []
    # ...but not on Hetzner, and that run is no GCE probe.
    (elsewhere,) = await _claim(
        session_maker, now=held_at, limit=1, hotkey=_HETZNER_HOTKEY
    )
    assert elsewhere in agents
    probe_at = opened + BREAKER_OPEN_DURATION + _SECOND
    breaker = await _gcp_breaker(session_maker, now=probe_at)
    assert breaker.open and breaker.last_probe_at is None
    assert len(await _claim(session_maker, now=probe_at)) == 1


async def _probe_state(session_maker, *, now, signature=(_CODE, _PROVIDER, _LANE)):
    return (await _plan(session_maker, now=now)).breakers[signature]


async def test_probe_that_later_fails_elsewhere_still_consumes_the_interval(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Placement, not the recorded failure metadata, defines a probe."""
    t0 = datetime.now(UTC) - timedelta(hours=1)
    agents = await _tripped_fleet(session_maker, t0=t0)
    opened = _opened_at(t0)
    started = opened + BREAKER_OPEN_DURATION + _SECOND
    attempt = await _add_attempt(
        session_maker,
        agents[0],
        status="running",
        started_at=started,
        finished_at=None,
        screener_hotkey=_SCREENER,
    )
    now = started + _SECOND
    running = await _probe_state(session_maker, now=now)
    assert running.last_probe_at == started

    # It fails fast for a different reason on a different provider and lane.
    async with session_maker() as session, session.begin():
        row = await session.get(ScreeningAttempt, attempt)
        assert row is not None
        row.status = "failed"
        row.finished_at = started + timedelta(seconds=30)
        row.reason_code = _CODE
        row.failure_provider = "targon"
        row.failure_lane = "kaniko"
    finished = await _probe_state(session_maker, now=now)

    assert finished.last_probe_at == running.last_probe_at == started
    assert finished.next_probe_at == started + BREAKER_PROBE_INTERVAL
    # The next claim cannot probe again inside the interval.
    assert (
        await _claim(session_maker, now=started + BREAKER_PROBE_INTERVAL - _SECOND)
        == []
    )


async def test_probe_classification_is_identical_running_and_finished(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    t0 = datetime.now(UTC) - timedelta(hours=1)
    agents = await _tripped_fleet(session_maker, t0=t0)
    opened = _opened_at(t0)
    started = opened + _SECOND
    attempt = await _add_attempt(
        session_maker,
        agents[0],
        status="running",
        started_at=started,
        finished_at=None,
    )
    now = opened + BREAKER_OPEN_DURATION + _SECOND
    seen = [(await _probe_state(session_maker, now=now)).last_probe_at]
    async with session_maker() as session, session.begin():
        build = SubmissionImageBuild(
            build_id=uuid4(),
            agent_id=agents[0],
            attempt_id=attempt,
            environment="prod",
            artifact_sha256="e" * 64,
            image_ref=f"ditto-screen/{agents[0]}-{attempt}:latest",
            output_key="builds/x",
        )
        session.add(build)
    seen.append((await _probe_state(session_maker, now=now)).last_probe_at)
    async with session_maker() as session, session.begin():
        row = await session.get(ScreeningAttempt, attempt)
        assert row is not None
        row.status = "failed"
        row.finished_at = started + _SECOND
        row.reason_code = "targon-build-unavailable"
        row.failure_provider = "targon"
        row.failure_lane = "kaniko"
    seen.append((await _probe_state(session_maker, now=now)).last_probe_at)
    assert seen == [started] * 3


@pytest.mark.parametrize("provider", ["home", "test", "targon"])
async def test_probe_by_any_node_provider_paces_its_own_signature(
    session_maker: async_sessionmaker[AsyncSession], provider: str
) -> None:
    hotkey = f"5GNodeHotkey{provider}ForInfraRetryTests0000000000000000"
    await _enroll_node(session_maker, hotkey=hotkey, provider=provider)
    t0 = datetime.now(UTC) - timedelta(hours=1)
    agents = await _tripped_fleet(session_maker, t0=t0, provider=provider)
    opened = _opened_at(t0)
    started = opened + BREAKER_OPEN_DURATION + _SECOND
    await _add_attempt(
        session_maker,
        agents[0],
        status="running",
        started_at=started,
        finished_at=None,
        screener_hotkey=hotkey,
    )
    state = await _probe_state(
        session_maker, now=started + _SECOND, signature=(_CODE, provider, _LANE)
    )
    assert state.last_probe_at == started
    # A claimant on that provider is now held for the interval.
    assert await _claim(session_maker, now=started + _SECOND, hotkey=hotkey) == []


async def test_pass_closes_only_the_signatures_of_its_own_lane_in_a_mixed_history(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Fail on GCE (G), retry on Hetzner and fail (H), retry on GCE and pass."""
    await _enroll_node(session_maker, hotkey=_HETZNER_HOTKEY, provider=_HETZNER)
    t0 = datetime.now(UTC) - timedelta(hours=2)
    step = BREAKER_WINDOW / (BREAKER_DISTINCT_AGENTS + 1)
    # Trip G (gcp) and H (hetzner) with three failing agents each.
    gcp_agents = await _tripped_fleet(session_maker, t0=t0)
    hetzner_agents = await _tripped_fleet(
        session_maker, t0=t0 + BREAKER_WINDOW * 2, provider=_HETZNER
    )
    later = t0 + BREAKER_WINDOW * 4
    # One agent fails on GCE, then Hetzner, then passes on GCE.
    mixed = gcp_agents[0]
    await _follow_up(
        session_maker,
        mixed,
        status="failed",
        at=later,
        hotkey=_HETZNER_HOTKEY,
        provider=_HETZNER,
        lane=_LANE,
    )
    await _follow_up(
        session_maker, mixed, status="passed", at=later + step, hotkey=_SCREENER
    )
    now = later + BREAKER_OPEN_DURATION
    plan = await _plan(session_maker, now=now)
    assert not plan.breakers[(_CODE, _PROVIDER, _LANE)].open
    assert plan.breakers[(_CODE, _HETZNER, _LANE)].open
    assert hetzner_agents  # the H breaker's own agents are still parked


async def test_mixed_history_mirror_hetzner_pass_does_not_close_gcp(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _enroll_node(session_maker, hotkey=_HETZNER_HOTKEY, provider=_HETZNER)
    t0 = datetime.now(UTC) - timedelta(hours=2)
    step = BREAKER_WINDOW / (BREAKER_DISTINCT_AGENTS + 1)
    gcp_agents = await _tripped_fleet(session_maker, t0=t0)
    await _tripped_fleet(session_maker, t0=t0 + BREAKER_WINDOW * 2, provider=_HETZNER)
    later = t0 + BREAKER_WINDOW * 4
    mixed = gcp_agents[0]
    await _follow_up(
        session_maker,
        mixed,
        status="failed",
        at=later,
        hotkey=_SCREENER,
        provider=_PROVIDER,
        lane=_LANE,
    )
    await _follow_up(
        session_maker, mixed, status="passed", at=later + step, hotkey=_HETZNER_HOTKEY
    )
    plan = await _plan(session_maker, now=later + BREAKER_OPEN_DURATION)
    assert plan.breakers[(_CODE, _PROVIDER, _LANE)].open
    # The Hetzner pass followed only GCE failures: H is untouched.
    assert plan.breakers[(_CODE, _HETZNER, _LANE)].open


@pytest.mark.parametrize(
    "outcome",
    [
        # Policy-only rescreen: retained build evidence, never builds.
        {
            "status": "passed",
            "with_image": False,
            "reason_code": "policy-only-rescreen",
        },
        # Static tripwire fires before any build starts.
        {"status": "quarantined", "with_image": False},
        # Another reject (contract / duplicate) never reached the build.
        {
            "status": "rejected",
            "with_image": False,
            "reason_code": "container-harness-contract",
        },
    ],
)
async def test_outcomes_without_build_proof_do_not_close_the_breaker(
    session_maker: async_sessionmaker[AsyncSession], outcome: dict
) -> None:
    t0 = datetime.now(UTC) - timedelta(hours=1)
    agents = await _tripped_fleet(session_maker, t0=t0)
    opened = _opened_at(t0)
    await _follow_up(
        session_maker, agents[0], at=opened + _SECOND, hotkey=_SCREENER, **outcome
    )
    state = await _probe_state(session_maker, now=opened + BREAKER_OPEN_DURATION)
    assert state.open


async def test_build_rejection_and_verified_image_do_close_the_breaker(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    t0 = datetime.now(UTC) - timedelta(hours=1)
    agents = await _tripped_fleet(session_maker, t0=t0)
    opened = _opened_at(t0)
    await _follow_up(
        session_maker,
        agents[0],
        status="rejected",
        at=opened + _SECOND,
        hotkey=_SCREENER,
        reason_code="docker-build",
        with_image=False,
    )
    state = await _probe_state(session_maker, now=opened + BREAKER_OPEN_DURATION)
    assert not state.open


async def test_claimant_rule_holds_the_failing_provider_and_frees_others(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    gcp_node = "5GGcpNodeHotkeyForInfraRetryTests0000000000000000000"
    await _enroll_node(session_maker, hotkey=gcp_node, provider="gcp")
    await _enroll_node(session_maker, hotkey=_HETZNER_HOTKEY, provider=_HETZNER)
    t0 = datetime.now(UTC) - timedelta(hours=1)
    await _tripped_fleet(session_maker, t0=t0)
    held_at = _opened_at(t0) + BREAKER_OPEN_DURATION - _SECOND

    # Both flavours of GCE worker (legacy fleet and an enrolled node) are held...
    assert await _claim(session_maker, now=held_at) == []
    assert await _claim(session_maker, now=held_at, hotkey=gcp_node) == []
    # ...while a worker on another provider is not.
    assert len(await _claim(session_maker, now=held_at, hotkey=_HETZNER_HOTKEY)) > 0
