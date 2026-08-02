"""Real-Postgres proof that parallel slots preserve completion-first FIFO."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.api_models.ticket_status import TicketStatus
from ditto.db import create_db_engine
from ditto.db.models import (
    Agent,
    BenchmarkDataset,
    EvaluationPayment,
    Score,
    ValidatorTicket,
)
from ditto.db.queries.tickets import issue_ticket

pytestmark = pytest.mark.integration

# The era these queues are ordered in. Nothing here is about a benchmark
# version -- the subject is FIFO order and owner convergence under real
# concurrency -- and it used to be left to ``issue_ticket``'s default of 2. That
# default is unreachable now: the ``validator_tickets`` floor trigger refuses a
# lease under MIN_SCOREABLE_BENCH_VERSION, so the era is named, and naming the
# live one brings its contract with it -- v7 requires a verified screened image
# and a pinned dataset per candidate, hence ``_provisioned_agent``.
_BENCH_VERSION = 7


def _provisioned_agent(agent: Agent, *, at: datetime) -> Agent:
    """Give ``agent`` the screened artifact the live contract demands.

    Without it the agent is not a queue candidate at all and every assertion
    below would pass vacuously against an empty queue.
    """
    agent.screened_image_sha256 = f"{agent.agent_id.int % (16**64):064x}"
    agent.screened_image_size_bytes = 4096
    agent.screened_image_id = "sha256:" + f"{agent.agent_id.int % (16**64):064x}"
    agent.screened_image_ref = f"ditto-screen/{agent.agent_id}:latest"
    agent.screened_image_upload_id = uuid4()
    agent.screened_image_verified_at = at
    return agent


def _dataset(agent_id, *, seed: int) -> BenchmarkDataset:
    """The era's pinned dataset, which the allocator requires per candidate."""
    return BenchmarkDataset(
        agent_id=agent_id,
        bench_version=_BENCH_VERSION,
        seed=seed,
        sha256=f"{seed + 1:064x}",
        run_size="full",
    )


async def test_same_validator_slots_walk_the_fifo_head_downwards() -> None:
    """Concurrent sibling slots take the two oldest rows -- and stop there.

    This asserted the pre-#433 single-slot invariant: one slot wins the head
    and the sibling parks on ``None`` until the lease drains. ditto-platform#449
    deliberately relaxed that. One ticket per ``(agent, bench_version,
    validator)`` is the composite primary key, so a validator already holding
    the head can never add a second score to it -- the head's remaining quorum
    slots belong to other validators no matter what the sibling does. Parking
    bought the head nothing and idled a slot for the whole ninety-minute lease.

    What completion-first still guarantees is that a slot takes the oldest row
    it is *eligible* for. So the two slots must land on the two oldest rows and
    leave ``newest`` alone: advancing past the head is not permission to reach
    arbitrarily far down the queue.
    """
    engine = create_db_engine()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC).replace(microsecond=0)
    oldest = uuid4()
    newer = uuid4()
    newest = uuid4()
    async with maker() as session, session.begin():
        await session.execute(text("TRUNCATE TABLE agents CASCADE"))
        rows: list[object] = []
        for index, (agent_id, label, letter) in enumerate(
            [
                (oldest, "oldest", "a"),
                (newer, "newer", "b"),
                (newest, "newest", "c"),
            ]
        ):
            rows.append(
                _provisioned_agent(
                    Agent(
                        agent_id=agent_id,
                        miner_hotkey=f"completion-first-{label}",
                        name=f"completion-first-{label}",
                        sha256=letter * 64,
                        status=AgentStatus.EVALUATING,
                        screening_policy_version=SCREENING_POLICY_VERSION,
                        created_at=now + timedelta(minutes=index),
                    ),
                    at=now,
                )
            )
            rows.append(_dataset(agent_id, seed=index))
        session.add_all(rows)

    async def claim(slot_id: str):
        async with maker() as session, session.begin():
            ticket = await issue_ticket(
                session,
                validator_hotkey="5ConcurrentCompletionFirst",
                slot_id=slot_id,
                now=now,
                ttl=timedelta(minutes=30),
                bench_version=_BENCH_VERSION,
                completion_first=True,
            )
            return ticket.agent_id if ticket is not None else None

    outcomes = await asyncio.gather(claim("slot-0"), claim("slot-1"))
    # A parked sibling -- the #449 regression -- shows up here as a ``None``.
    assert None not in outcomes
    # Two distinct rows: never a second lease on the head, and never the same
    # row twice. Which slot wins the head is a race, so compare as a set.
    assert set(outcomes) == {oldest, newer}

    async with maker() as session:
        # Reaching past the next FIFO candidate is the opposite failure, and it
        # is the one that would break the ordering the public queue preview
        # promises miners. ``newest`` must be untouched.
        newest_tickets = await session.scalar(
            select(func.count()).where(ValidatorTicket.agent_id == newest)
        )
        issued = (
            await session.execute(
                select(ValidatorTicket.agent_id, ValidatorTicket.slot_id).where(
                    ValidatorTicket.status == TicketStatus.ISSUED
                )
            )
        ).all()
    assert newest_tickets == 0
    assert len(issued) == 2
    assert {agent_id for agent_id, _ in issued} == {oldest, newer}
    # Each lease belongs to its own slot; one slot must not hold both.
    assert {slot_id for _, slot_id in issued} == {"slot-0", "slot-1"}
    await engine.dispose()


@pytest.mark.parametrize("completion_first", [False, True])
@pytest.mark.parametrize(
    "identity_mode", ["paid-coldkey", "mixed-legacy", "rotated-legacy-bridge"]
)
async def test_same_owner_partial_scores_converge_on_one_generation(
    completion_first: bool, identity_mode: str
) -> None:
    engine = create_db_engine()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC).replace(microsecond=0)
    agents = [uuid4(), uuid4()]
    async with maker() as session, session.begin():
        await session.execute(text("TRUNCATE TABLE agents CASCADE"))
        for index, agent_id in enumerate(agents):
            miner_hotkey = (
                "mixed-owner-hotkey"
                if identity_mode == "mixed-legacy"
                else f"rotated-owner-hotkey-{index}"
                if identity_mode == "rotated-legacy-bridge"
                else f"same-owner-partial-{index}"
            )
            session.add_all(
                [
                    _provisioned_agent(
                        Agent(
                            agent_id=agent_id,
                            miner_hotkey=miner_hotkey,
                            name=f"same-owner-partial-{index}",
                            sha256=f"{index + 1:x}" * 64,
                            status=AgentStatus.EVALUATING,
                            screening_policy_version=SCREENING_POLICY_VERSION,
                            created_at=now + timedelta(minutes=index),
                        ),
                        at=now,
                    ),
                    _dataset(agent_id, seed=index),
                    ValidatorTicket(
                        agent_id=agent_id,
                        validator_hotkey=f"prior-validator-{index}",
                        status=TicketStatus.SCORED,
                        issued_at=now,
                        deadline=now + timedelta(minutes=30),
                        bench_version=_BENCH_VERSION,
                        attempt_count=1,
                    ),
                    Score(
                        agent_id=agent_id,
                        validator_hotkey=f"prior-validator-{index}",
                        run_id=f"prior-run-{index}",
                        signature=None,
                        seed=index,
                        composite=0.5 + index / 10,
                        tool_mean=0.5,
                        memory_mean=0.5,
                        median_ms=100,
                        n=206,
                        details=None,
                        bench_version=_BENCH_VERSION,
                        generated_at=now,
                    ),
                ]
            )
            should_add_payment = (
                identity_mode == "paid-coldkey"
                or (identity_mode == "mixed-legacy" and index == 0)
                or (identity_mode == "rotated-legacy-bridge" and index == 1)
            )
            if should_add_payment:
                session.add(
                    EvaluationPayment(
                        block_hash=f"0xsame-owner-partial-{index}",
                        extrinsic_index=index,
                        agent_id=agent_id,
                        miner_hotkey=miner_hotkey,
                        miner_coldkey="same-owner-coldkey",
                        amount_rao=1,
                        tao_usd_rate=Decimal("1"),
                        dest_address="payment-destination",
                        timestamp=now,
                    )
                )
        if identity_mode == "rotated-legacy-bridge":
            bridge_id = uuid4()
            session.add_all(
                [
                    Agent(
                        agent_id=bridge_id,
                        miner_hotkey="rotated-owner-hotkey-0",
                        name="settled-owner-identity-bridge",
                        sha256="f" * 64,
                        status=AgentStatus.SCORED,
                        screening_policy_version=SCREENING_POLICY_VERSION,
                        created_at=now - timedelta(days=1),
                    ),
                    EvaluationPayment(
                        block_hash="0xsettled-owner-identity-bridge",
                        extrinsic_index=99,
                        agent_id=bridge_id,
                        miner_hotkey="rotated-owner-hotkey-0",
                        miner_coldkey="same-owner-coldkey",
                        amount_rao=1,
                        tao_usd_rate=Decimal("1"),
                        dest_address="payment-destination",
                        timestamp=now - timedelta(days=1),
                    ),
                ]
            )

    async def claim(validator_hotkey: str):
        async with maker() as session, session.begin():
            ticket = await issue_ticket(
                session,
                validator_hotkey=validator_hotkey,
                now=now,
                ttl=timedelta(minutes=30),
                bench_version=_BENCH_VERSION,
                completion_first=completion_first,
            )
            return ticket.agent_id if ticket is not None else None

    outcomes = await asyncio.gather(claim("recovery-a"), claim("recovery-b"))
    successful = [outcome for outcome in outcomes if outcome is not None]
    assert successful
    assert len(set(successful)) == 1

    # SKIP LOCKED may make one simultaneous poll yield while its sibling
    # transaction owns the selected Agent row. The next ordinary poll must
    # fill that free slot with the same generation rather than remain stuck or
    # open the other one.
    settled = [await claim("recovery-a"), await claim("recovery-b")]
    assert None not in settled
    assert len(set(settled)) == 1

    async with maker() as session:
        issued_agents = (
            await session.scalars(
                select(ValidatorTicket.agent_id).where(
                    ValidatorTicket.status == TicketStatus.ISSUED
                )
            )
        ).all()
    assert len(issued_agents) == 2
    assert len(set(issued_agents)) == 1
    await engine.dispose()
