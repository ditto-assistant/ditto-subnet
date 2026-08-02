"""Real-Postgres fleet-cap proof for source-era backfill during a rollout.

This used to run the lane against a RETIRED era: an activated v6 -> v7 rollout
with ``allow_retired_era_backfill=True``, the operator knob that re-opened v6
after the fleet had left it. That knob is gone and the lane refuses a retired
era outright, so the fixture it was built on cannot exist any more.

The concurrency property it proved is not about retirement, though, and the lane
still has a live case: while a rollout is OPEN, ``from_version`` is the version
the fleet is actually scoring, and this lane keeps a source-version validator
busy draining the source backlog instead of idling through the transition. The
fleet cap is what stops that from eating the desired era's capacity. So the same
assertions run against an open v7 -> v8 rollout with ``active_version`` equal to
``from_version`` -- the only shape this lane serves now -- and nothing about the
cap, the lock ordering or the resume path is weakened.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from ditto.api_models import benchmark_contract as benchmark_contract_module
from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.benchmark_contract import BenchmarkContract
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.api_models.ticket_status import TicketStatus
from ditto.api_server.endpoints import validator as validator_endpoint
from ditto.db import create_db_engine
from ditto.db.models import (
    Agent,
    BenchmarkDataset,
    BenchmarkRollout,
    ValidatorHeartbeat,
    ValidatorTicket,
)

pytestmark = pytest.mark.integration

# The open transition this lane rides. ``_SOURCE_VERSION`` is what the fleet is
# scoring right now and what the backfill tickets are for; ``_DESIRED_VERSION``
# is where the rollout is heading. Source must be at or above
# MIN_SCOREABLE_BENCH_VERSION or the lane declines before any of the concurrency
# machinery is reached -- that is the floor this file used to circumvent.
#
# Which forces the target one higher than anything shipped: v7 is both the floor
# and the newest contract, and a rollout moves forward. ``_next_contract_shipped``
# registers the target contract for the duration of the test, because the state
# being reproduced is "the next transition has opened", and the transition after
# the current one is the only remaining customer of this lane. Nothing under
# test reads the contract's contents; the registration exists so that
# ``benchmark_contract`` does not fail closed on a version the fixture names.
_SOURCE_VERSION = 7
_DESIRED_VERSION = 8


@pytest.fixture
def _next_contract_shipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        benchmark_contract_module._CONTRACTS,
        _DESIRED_VERSION,
        BenchmarkContract(_DESIRED_VERSION, SCREENING_POLICY_VERSION, True),
    )


async def test_concurrent_source_backfill_respects_atomic_fleet_cap(
    monkeypatch: pytest.MonkeyPatch,
    _next_contract_shipped: None,
) -> None:
    """Two replicas cannot both consume the sole permitted source-era slot."""
    engine = create_db_engine()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC).replace(microsecond=0)
    # Open, not activated: while a transition is collecting, ``from_version`` is
    # still the live era, which is exactly when this lane is legitimate.
    rollout = BenchmarkRollout(
        rollout_id=uuid4(),
        from_version=_SOURCE_VERSION,
        desired_version=_DESIRED_VERSION,
        status="collecting",
        cohort_size=5,
        created_at=now - timedelta(hours=1),
    )

    async def cohort_complete(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        return True

    def supports_version(
        heartbeat: ValidatorHeartbeat, *, now: datetime, version: int
    ) -> bool:
        del heartbeat, now
        return version in (_SOURCE_VERSION, _DESIRED_VERSION)

    monkeypatch.setattr(validator_endpoint, "rollout_cohort_complete", cohort_complete)
    monkeypatch.setattr(
        validator_endpoint, "heartbeat_supports_version", supports_version
    )

    async with maker() as session, session.begin():
        await session.execute(text("TRUNCATE TABLE benchmark_rollouts, agents CASCADE"))
        session.add(rollout)
        for index in range(2):
            agent_id = uuid4()
            session.add(
                Agent(
                    agent_id=agent_id,
                    miner_hotkey=f"source-miner-{index}",
                    name=f"source-agent-{index}",
                    sha256=f"{index + 1:x}" * 64,
                    status=AgentStatus.EVALUATING,
                    screening_policy_version=SCREENING_POLICY_VERSION,
                    screened_image_sha256=f"{index + 3:x}" * 64,
                    screened_image_size_bytes=1024,
                    screened_image_id="sha256:" + f"{index + 3:x}" * 64,
                    screened_image_ref=f"ditto-screen/{agent_id}:latest",
                    screened_image_upload_id=uuid4(),
                    screened_image_verified_at=now,
                    created_at=now - timedelta(minutes=index + 1),
                )
            )
            session.add(
                BenchmarkDataset(
                    agent_id=agent_id,
                    bench_version=_SOURCE_VERSION,
                    seed=index + 1,
                    sha256=f"{index + 5:x}" * 64,
                    run_size="full",
                )
            )
        for index in range(2):
            session.add(
                ValidatorHeartbeat(
                    validator_hotkey=f"validator-{index}",
                    software_version="1.2.2",
                    protocol_version=12,
                    code_digest="a" * 64,
                    state="polling",
                    benchmark_capacity={
                        "configured_slots": 1,
                        "healthy_slots": ["slot-0"],
                        "admission": "accepting",
                        "active": [],
                    },
                    reported_at=now,
                    seen_at=now,
                    signature="a" * 128,
                )
            )

    async def claim(validator_hotkey: str) -> ValidatorTicket | None:
        async with maker() as session, session.begin():
            heartbeat = await session.get(ValidatorHeartbeat, validator_hotkey)
            assert heartbeat is not None
            stored_rollout = await session.get(BenchmarkRollout, rollout.rollout_id)
            assert stored_rollout is not None
            return await validator_endpoint._issue_source_backfill_ticket(
                session,
                rollout=stored_rollout,
                heartbeat=heartbeat,
                validator_hotkey=validator_hotkey,
                now=now,
                # Equal to ``from_version``, which is what "a rollout is open"
                # means to this lane. The shipped carryover defaults are left
                # alone deliberately: no setting is needed to reach the lane in
                # this state, and none exists that could reach it in any other.
                active_version=_SOURCE_VERSION,
                artifact_mode="screened_only",
                validator_running_benchmark=False,
                slot_id="slot-0",
            )

    outcomes = await asyncio.gather(claim("validator-0"), claim("validator-1"))
    assert sum(ticket is not None for ticket in outcomes) == 1
    async with maker() as session:
        active = await session.scalar(
            select(func.count())
            .select_from(ValidatorTicket)
            .where(
                ValidatorTicket.bench_version == _SOURCE_VERSION,
                ValidatorTicket.status == TicketStatus.ISSUED,
                ValidatorTicket.deadline > now,
            )
        )
    assert active == 1

    # A pre-existing live source lease consumes the cap before either replica
    # enters. Neither may create a second source assignment.
    async with maker() as session, session.begin():
        await session.execute(delete(ValidatorTicket))
        source_agent = await session.scalar(select(Agent.agent_id).limit(1))
        assert source_agent is not None
        session.add(
            ValidatorTicket(
                agent_id=source_agent,
                bench_version=_SOURCE_VERSION,
                validator_hotkey="validator-existing",
                status=TicketStatus.ISSUED,
                issued_at=now,
                deadline=now + timedelta(minutes=90),
                attempt_count=1,
                slot_id="slot-0",
            )
        )
    outcomes = await asyncio.gather(claim("validator-0"), claim("validator-1"))
    assert outcomes == [None, None]

    # Desired-era allocation may already hold an owner lock before it falls
    # through to source backfill. A contended fleet gate must yield immediately,
    # not block and complete an owner<->fleet deadlock cycle.
    async with maker() as holder, holder.begin():
        await holder.execute(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtextextended(f"source-backfill:{rollout.rollout_id}", 0)
                )
            )
        )
        assert await asyncio.wait_for(claim("validator-0"), timeout=1) is None

    # Fleet contention gates only new admissions. A restarted slot must resume
    # its row-locked live lease without waiting for (or consuming) the cap.
    async with maker() as session, session.begin():
        await session.execute(delete(ValidatorTicket))
        source_agent = await session.scalar(select(Agent.agent_id).limit(1))
        assert source_agent is not None
        session.add(
            ValidatorTicket(
                agent_id=source_agent,
                bench_version=_SOURCE_VERSION,
                validator_hotkey="validator-0",
                status=TicketStatus.ISSUED,
                issued_at=now,
                deadline=now + timedelta(minutes=90),
                attempt_count=1,
                slot_id="slot-0",
            )
        )
    async with maker() as holder, holder.begin():
        await holder.execute(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtextextended(f"source-backfill:{rollout.rollout_id}", 0)
                )
            )
        )
        resumed = await asyncio.wait_for(claim("validator-0"), timeout=1)
        assert resumed is not None
        assert resumed.agent_id == source_agent
        assert resumed.attempt_count == 1
    await engine.dispose()
