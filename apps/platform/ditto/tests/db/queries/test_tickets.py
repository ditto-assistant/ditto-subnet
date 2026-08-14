"""Unit tests for :mod:`ditto.db.queries.tickets` against SQLite-in-memory."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.api_models.ticket_status import TicketPurpose, TicketStatus
from ditto.db.models import (
    Agent,
    BenchmarkDataset,
    BenchmarkRollout,
    BenchmarkRolloutMember,
    EvaluationPayment,
    Score,
    ValidatorHeartbeat,
    ValidatorLeaseAudit,
    ValidatorTicket,
)
from ditto.db.queries.audit import (
    EVENT_SCORE_RETEST_REQUESTED,
    append_audit_entry,
)
from ditto.db.queries.lease_liveness import (
    IDLE_EVIDENCE_MAX_AGE,
    LEASE_REPORTING_GRACE,
)
from ditto.db.queries.scores import SCORING_QUORUM
from ditto.db.queries.tickets import (
    EMISSION_CONTENDER_COUNT,
    MAX_ATTEMPTS_PER_VERSION,
    PROVISIONAL_CONTENDER_LANE_SIZE,
    expire_overdue_tickets,
    get_open_ticket,
    issue_confirmation_ticket,
    issue_ticket,
    mark_ticket_scored,
)
from ditto.tests.legacy_era import retired_era_writes_allowed

_NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC)
_TTL = timedelta(minutes=30)
_LATER = _NOW + timedelta(hours=1)
_AFTER_COOLDOWN = _NOW + timedelta(hours=7)
# Long enough after issuance that a heartbeat reporting an empty slot is real
# evidence of idleness rather than a run that has not announced itself yet.
_AFTER_REPORTING_GRACE = _NOW + LEASE_REPORTING_GRACE + timedelta(minutes=1)
# The era the fleet is actually leasing. Most of this file used v2 as an
# arbitrary version; the bench-version floor now refuses to write a ticket or a
# score below 7, so "some benchmark" has to be a benchmark that still exists.
_BENCH = 7
# The era it came from. Nothing at or below this may be leased again, so it only
# ever appears as a rollout's ``from_version`` or as history written through
# ``retired_era_writes_allowed``.
_RETIRED_BENCH = 6


async def _seed_heartbeat(
    session: AsyncSession,
    *,
    validator_hotkey: str,
    seen_at: datetime,
    active: tuple[dict, ...] = (),
    protocol_version: int = 10,
    state: str = "polling",
) -> None:
    """Store the signed snapshot that is the platform's only evidence of what a
    validator's slots are doing. Must be called inside a transaction."""
    session.add(
        ValidatorHeartbeat(
            validator_hotkey=validator_hotkey,
            software_version="1.0.0",
            protocol_version=protocol_version,
            code_digest="d" * 64,
            state=state,
            first_seen_at=seen_at,
            reported_at=seen_at,
            seen_at=seen_at,
            signature="ab" * 64,
            benchmark_capacity={
                "configured_slots": 1,
                "healthy_slots": ["slot-0"],
                "admission": "accepting",
                "active": list(active),
            },
        )
    )
    await session.flush()


def _mark_lease_reported(ticket: ValidatorTicket, *, at: datetime) -> None:
    """Say that this lease was observed running at least once.

    The liveness gate will not revoke a lease that has never reported: a slot
    that never announced itself is silent because it is still starting up, not
    because it is idle. So a test about reclaiming an *idle* lease has to first
    establish that the run began -- otherwise it is exercising the never-reported
    refusal instead of the case it was written for. Call inside the transaction
    that owns *ticket*.
    """
    ticket.first_reported_at = at


async def _seed_retired_era_lease(
    session: AsyncSession,
    *,
    agent_id: UUID,
    validator_hotkey: str,
    issued_at: datetime,
    deadline: datetime,
    reported_at: datetime | None = None,
    slot_id: str = "slot-0",
    status: TicketStatus = TicketStatus.ISSUED,
    purpose: TicketPurpose = TicketPurpose.CANONICAL_QUORUM,
    attempt_count: int = 1,
    retry_after: datetime | None = None,
) -> None:
    """One ticket left behind on the era the fleet has moved off.

    Everything below that needs *two* eras needs this, because there is only
    one era left that a validator may be handed work on. ``issue_ticket`` scopes
    both halves of its decision to the era it was asked for -- it resumes only a
    lease from that era, and it counts only that era's attempts against the
    retry budget -- so a same-era row proves nothing about either rule. The
    older row is what puts the poll on the incompatible-lease branch, and what
    makes a reset budget mean something.

    The allocator cannot mint one of these any more: the bench-version floor
    refuses a sub-7 ticket outright. It does not have to, either. Slots are
    holding retired-era leases in production right now, written before the floor
    and grandfathered by it, and that is exactly the state a validator is in
    when it polls for the era after them. ``retired_era_writes_allowed`` writes
    that history and puts the floor back, so what the poll sees is a row
    production actually has rather than one the allocator would be refused if it
    tried to create it today.
    """
    async with retired_era_writes_allowed(session), session.begin():
        session.add(
            ValidatorTicket(
                agent_id=agent_id,
                validator_hotkey=validator_hotkey,
                slot_id=slot_id,
                bench_version=_RETIRED_BENCH,
                status=status,
                purpose=purpose,
                purpose_revision=1,
                issued_at=issued_at,
                deadline=deadline,
                first_reported_at=reported_at,
                attempt_count=attempt_count,
                retry_after=retry_after,
            )
        )


# Exactly the shape of the live v7 fleet the owner-pin defect was found on:
# three version-capable validators against a quorum of three, so a single
# retry-exhausted validator already puts a submission out of reach.
_CAPABLE_FLEET = ("5CapableA", "5CapableB", "5CapableC")
_OWNER_COLDKEY = "5SharedOwnerColdkey"
_OWNER_ROLLOUT_STARTED = _NOW - timedelta(hours=6)


async def _seed_capable_heartbeat(
    session: AsyncSession, *, validator_hotkey: str, bench_version: int = _BENCH
) -> None:
    """A fresh signed heartbeat that advertises ``bench_version``.

    Only a validator the platform can see as capable counts toward the quorum
    ceiling, so reachability tests must publish a real capability payload
    rather than the bare slot snapshot :func:`_seed_heartbeat` stores.
    """
    revision = "a" * 40
    session.add(
        ValidatorHeartbeat(
            validator_hotkey=validator_hotkey,
            software_version="1.3.0",
            protocol_version=12,
            code_digest="d" * 64,
            state="polling",
            first_seen_at=_NOW,
            reported_at=_NOW,
            seen_at=_NOW,
            signature="ab" * 64,
            capabilities={
                "screened_images": True,
                "require_screened_image": False,
                "source_build_fallback": True,
                "full_stack_managed": False,
                "stack_updater": False,
                "sandbox_egress_restricted": True,
                "ticket_inference": False,
                "signed_score_quorum": False,
                "executor_isolation": "privileged_dind",
                "scorer_benchmarks": {
                    "status": "fresh_verified",
                    "supported_bench_versions": [_RETIRED_BENCH, bench_version],
                    "observed_at": int(_NOW.timestamp()),
                    "software_version": "1.3.0",
                    "source_revision": revision,
                },
            },
            stack={
                "mode": "source",
                "compose_schema": 1,
                "release_descriptor_digest": None,
                "components": {
                    name: {
                        "source_revision": (
                            revision if name == "dittobench_api" else "b" * 40
                        ),
                        "version": "1.3.0" if name == "dittobench_api" else "1.2.0",
                        "provenance": "committed_pin",
                    }
                    for name in (
                        "ditto_subnet",
                        "dittobench_api",
                        "sandbox_docker",
                        "model_relay",
                        "pylon",
                        "ollama",
                    )
                },
            },
        )
    )
    await session.flush()


def _owner_ticket(
    agent_id: UUID,
    validator_hotkey: str,
    *,
    status: TicketStatus,
    attempt_count: int = MAX_ATTEMPTS_PER_VERSION,
    retry_after: datetime | None = None,
    deadline: datetime | None = None,
    started_after: timedelta = timedelta(minutes=10),
) -> ValidatorTicket:
    """One validator's history on an owner generation.

    Defaults to a spent retry budget, the only state that permanently costs a
    submission a quorum slot. ``started_after`` sets ``issued_at``, which is
    what the pin orders generations by.
    """
    return ValidatorTicket(
        agent_id=agent_id,
        validator_hotkey=validator_hotkey,
        bench_version=_BENCH,
        status=status,
        issued_at=_OWNER_ROLLOUT_STARTED + started_after,
        deadline=deadline or (_NOW - timedelta(hours=1)),
        retry_after=retry_after,
        attempt_count=attempt_count,
    )


async def _seed_owner_generations(
    session: AsyncSession, *, older: str, newer: str
) -> tuple[UUID, UUID]:
    """Two admitted same-owner generations on an activated v3 era.

    Linked by a shared payment coldkey rather than a shared hotkey, matching
    the live case where one miner submitted from two hotkeys.
    """
    older_id = await _seed_evaluating(
        session,
        created_at=_OWNER_ROLLOUT_STARTED + timedelta(minutes=5),
        name=older,
        screened=True,
        dataset_version=None,
    )
    newer_id = await _seed_evaluating(
        session,
        created_at=_OWNER_ROLLOUT_STARTED + timedelta(hours=4),
        name=newer,
        screened=True,
        dataset_version=None,
    )
    async with session.begin():
        session.add(
            BenchmarkRollout(
                rollout_id=uuid4(),
                from_version=_RETIRED_BENCH,
                desired_version=_BENCH,
                status="activated",
                cohort_size=5,
                created_at=_OWNER_ROLLOUT_STARTED,
                activated_at=_OWNER_ROLLOUT_STARTED,
            )
        )
        for index, agent_id in enumerate((older_id, newer_id)):
            agent = await session.get(Agent, agent_id)
            assert agent is not None
            session.add_all(
                [
                    BenchmarkDataset(
                        agent_id=agent_id,
                        bench_version=_BENCH,
                        seed=321 + index,
                        sha256=f"{index + 7:02x}" * 32,
                        run_size="full",
                    ),
                    EvaluationPayment(
                        block_hash=f"0xowner-reach-{index}",
                        extrinsic_index=index,
                        agent_id=agent_id,
                        miner_hotkey=agent.miner_hotkey,
                        miner_coldkey=_OWNER_COLDKEY,
                        amount_rao=1,
                        tao_usd_rate=Decimal("1"),
                        dest_address="5Destination",
                        timestamp=_OWNER_ROLLOUT_STARTED,
                    ),
                ]
            )
    return older_id, newer_id


def _active_slot(
    agent_id: UUID,
    *,
    ticket_deadline: datetime,
    bench_version: int = _BENCH,
    slot_id: str = "slot-0",
) -> dict:
    """One in-flight benchmark as a validator advertises it."""
    return {
        "slot_id": slot_id,
        "agent_id": str(agent_id),
        "bench_version": bench_version,
        "progress": {
            "stage": "running_benchmark",
            "completed": 143,
            "total": 281,
            "ticket_deadline": ticket_deadline.isoformat(),
        },
    }


async def _seed_evaluating(
    session: AsyncSession,
    *,
    created_at: datetime = _NOW,
    name: str = "a",
    screened: bool = True,
    dataset_version: int | None = _BENCH,
) -> UUID:
    """One waiting submission that the live contract will actually consider.

    The verified screened image and the dataset pin are on by default because
    ``_BENCH`` is a post-v2 contract: ``queue_candidate_predicate`` drops any
    agent missing either one before the allocator ranks anything, so a
    submission seeded the v2 way is not in the queue at all and every
    "which row got leased" assertion below would pass vacuously against
    ``None``. Pass ``screened=False`` only when a source-only submission is the
    point, and ``dataset_version=None`` when the test pins the dataset itself.
    """
    aid = uuid4()
    async with session.begin():
        agent = Agent(
            agent_id=aid,
            miner_hotkey=f"5Miner-{name}",
            name=name,
            sha256="ab" * 32,
            status=AgentStatus.EVALUATING,
            screening_policy_version=SCREENING_POLICY_VERSION,
            created_at=created_at,
        )
        if screened:
            agent.screened_image_sha256 = "12" * 32
            agent.screened_image_size_bytes = 123
            agent.screened_image_id = "sha256:" + "34" * 32
            agent.screened_image_ref = f"ditto-screen/{aid}:latest"
            agent.screened_image_upload_id = uuid4()
            agent.screened_image_verified_at = _NOW
        session.add(agent)
        await session.flush()
        if dataset_version is not None:
            session.add(
                BenchmarkDataset(
                    agent_id=aid,
                    bench_version=dataset_version,
                    seed=42,
                    sha256="cd" * 32,
                    run_size="full",
                )
            )
    return aid


async def _seed_scored(session: AsyncSession) -> UUID:
    aid = await _seed_evaluating(session)
    async with session.begin():
        agent = await session.get(Agent, aid)
        assert agent is not None
        agent.status = AgentStatus.SCORED
        session.add(
            Score(
                agent_id=aid,
                validator_hotkey="5V1",
                run_id="prior",
                signature=None,
                seed=1,
                composite=0.8,
                tool_mean=0.8,
                memory_mean=0.8,
                median_ms=100,
                n=114,
                details={"bench_version": _BENCH},
                generated_at=_NOW,
            )
        )
    return aid


async def _seed_finalized_top_five(
    session: AsyncSession, *, fifth_place: float = 0.80
) -> None:
    """Establish five ranked miners with ``fifth_place`` as the live floor."""
    async with session.begin():
        for rank in range(EMISSION_CONTENDER_COUNT):
            agent_id = uuid4()
            composite = fifth_place + (EMISSION_CONTENDER_COUNT - rank - 1) * 0.01
            session.add(
                Agent(
                    agent_id=agent_id,
                    miner_hotkey=f"5Ranked-{rank}",
                    name=f"ranked-{rank}",
                    sha256=f"{rank + 100:064x}",
                    status=AgentStatus.SCORED,
                    screening_policy_version=SCREENING_POLICY_VERSION,
                    created_at=_NOW - timedelta(days=1, minutes=rank),
                )
            )
            for validator_index in range(SCORING_QUORUM):
                validator = f"5Ranked-{rank}-{validator_index}"
                session.add(
                    Score(
                        agent_id=agent_id,
                        validator_hotkey=validator,
                        run_id=f"ranked-{rank}-{validator_index}",
                        signature=None,
                        seed=123,
                        composite=composite,
                        tool_mean=composite,
                        memory_mean=composite,
                        median_ms=100,
                        n=114,
                        details=None,
                        generated_at=_NOW,
                    )
                )


async def _seed_finalized_top_ten(
    session: AsyncSession, *, tenth_place: float = 0.80
) -> None:
    """Establish ten ranked owners with ``tenth_place`` as the fast-lane floor."""
    async with session.begin():
        for rank in range(PROVISIONAL_CONTENDER_LANE_SIZE):
            agent_id = uuid4()
            composite = (
                tenth_place + (PROVISIONAL_CONTENDER_LANE_SIZE - rank - 1) * 0.01
            )
            session.add(
                Agent(
                    agent_id=agent_id,
                    miner_hotkey=f"5TopTen-{rank}",
                    name=f"top-ten-{rank}",
                    sha256=f"{rank + 200:064x}",
                    status=AgentStatus.SCORED,
                    screening_policy_version=SCREENING_POLICY_VERSION,
                    created_at=_NOW - timedelta(days=2, minutes=rank),
                )
            )
            for validator_index in range(SCORING_QUORUM):
                session.add(
                    Score(
                        agent_id=agent_id,
                        validator_hotkey=f"5TopTen-{rank}-{validator_index}",
                        run_id=f"top-ten-{rank}-{validator_index}",
                        signature=None,
                        seed=123,
                        composite=composite,
                        tool_mean=composite,
                        memory_mean=composite,
                        median_ms=100,
                        n=114,
                        details=None,
                        generated_at=_NOW,
                    )
                )


async def _seed_two_scores_below_floor(
    session: AsyncSession, *, bench_version: int = _BENCH
) -> UUID:
    """An ``evaluating`` agent whose best-case median cannot reach the floor."""
    aid = await _seed_evaluating(session)
    async with session.begin():
        for index, composite in enumerate((0.10, 0.20)):
            validator = f"5Scored-{index}"
            session.add(
                ValidatorTicket(
                    agent_id=aid,
                    validator_hotkey=validator,
                    status=TicketStatus.SCORED,
                    issued_at=_NOW,
                    deadline=_NOW + _TTL,
                    bench_version=bench_version,
                    attempt_count=1,
                )
            )
            session.add(
                Score(
                    agent_id=aid,
                    validator_hotkey=validator,
                    run_id=f"below-top-five-{index}",
                    signature=None,
                    seed=123,
                    composite=composite,
                    tool_mean=composite,
                    memory_mean=composite,
                    median_ms=100,
                    n=114,
                    details=None,
                    bench_version=bench_version,
                    generated_at=_NOW,
                )
            )
    return aid


class TestIssueTicket:
    @pytest.mark.parametrize(
        "purpose",
        [TicketPurpose.CONTINUAL_RETEST, TicketPurpose.LEGACY_UNCLASSIFIED],
    )
    async def test_does_not_resume_or_expire_noncanonical_live_lease(
        self, session: AsyncSession, purpose: TicketPurpose
    ) -> None:
        aid = await _seed_evaluating(session)
        async with session.begin():
            session.add(
                ValidatorTicket(
                    agent_id=aid,
                    validator_hotkey="5V1",
                    status=TicketStatus.ISSUED,
                    purpose=purpose,
                    purpose_revision=(
                        0 if purpose == TicketPurpose.LEGACY_UNCLASSIFIED else 1
                    ),
                    issued_at=_NOW,
                    deadline=_NOW + _TTL,
                    bench_version=_BENCH,
                )
            )

        async with session.begin():
            ticket = await issue_ticket(
                session,
                validator_hotkey="5V1",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )

        assert ticket is None
        stored = await session.get(ValidatorTicket, (aid, _BENCH, "5V1"))
        assert stored is not None
        assert stored.status == TicketStatus.ISSUED
        assert stored.purpose == purpose

    async def test_same_coldkey_finishes_one_generation_before_next(
        self, session: AsyncSession
    ) -> None:
        first = await _seed_evaluating(session, created_at=_NOW, name="owner-first")
        second = await _seed_evaluating(
            session,
            created_at=_NOW + timedelta(minutes=1),
            name="owner-second",
        )
        async with session.begin():
            for index, agent_id in enumerate((first, second)):
                agent = await session.get(Agent, agent_id)
                assert agent is not None
                session.add(
                    EvaluationPayment(
                        block_hash=f"0xowner-{index}",
                        extrinsic_index=index,
                        agent_id=agent_id,
                        miner_hotkey=agent.miner_hotkey,
                        miner_coldkey="5SharedColdkey",
                        amount_rao=1,
                        tao_usd_rate=Decimal("1"),
                        dest_address="5Destination",
                        timestamp=_NOW,
                    )
                )

        claimed: list[UUID] = []
        async with session.begin():
            for index in range(SCORING_QUORUM):
                ticket = await issue_ticket(
                    session,
                    validator_hotkey=f"5OwnerValidator-{index}",
                    now=_NOW,
                    ttl=_TTL,
                    bench_version=_BENCH,
                )
                assert ticket is not None
                claimed.append(ticket.agent_id)
            last_resort = await issue_ticket(
                session,
                validator_hotkey="5OwnerValidator-blocked",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )

        assert claimed == [first] * SCORING_QUORUM
        # Every quorum slot on ``first`` is taken and this world contains no
        # other owner, so a fourth validator's only alternative to the owner's
        # second generation is an idle slot. The ordinary pass still refused it
        # -- that is what the three claims above prove -- and it is leased here
        # only because the pass came back empty. See
        # ``test_second_generation_waits_while_another_owner_has_work`` for the
        # same fixture with a competitor in it.
        assert last_resort is not None
        assert last_resort.agent_id == second

        async with session.begin():
            first_agent = await session.get(Agent, first)
            assert first_agent is not None
            first_agent.status = AgentStatus.SCORED
            for index in range(SCORING_QUORUM):
                completed = await session.get(
                    ValidatorTicket,
                    (first, _BENCH, f"5OwnerValidator-{index}"),
                )
                assert completed is not None
                completed.status = TicketStatus.SCORED
            next_ticket = await issue_ticket(
                session,
                validator_hotkey="5OwnerValidator-next",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )

        assert next_ticket is not None
        assert next_ticket.agent_id == second

    async def _seed_owner_pair_and_rival(
        self, session: AsyncSession, *, coldkey: str = "5RelaxColdkey"
    ) -> tuple[UUID, UUID, UUID]:
        """One owner with two generations, plus an unrelated owner's submission.

        The rival is a legacy (unpaid) row, so it falls back to its own hotkey
        and is a genuinely separate owner from the shared-coldkey pair.
        """
        first = await _seed_evaluating(session, created_at=_NOW, name="relax-first")
        second = await _seed_evaluating(
            session, created_at=_NOW + timedelta(minutes=1), name="relax-second"
        )
        rival = await _seed_evaluating(
            session, created_at=_NOW + timedelta(minutes=2), name="relax-rival"
        )
        async with session.begin():
            for index, agent_id in enumerate((first, second)):
                agent = await session.get(Agent, agent_id)
                assert agent is not None
                session.add(
                    EvaluationPayment(
                        block_hash=f"0xrelax-{index}",
                        extrinsic_index=index,
                        agent_id=agent_id,
                        miner_hotkey=agent.miner_hotkey,
                        miner_coldkey=coldkey,
                        amount_rao=1,
                        tao_usd_rate=Decimal("1"),
                        dest_address="5Destination",
                        timestamp=_NOW,
                    )
                )
        return first, second, rival

    async def test_second_generation_waits_while_another_owner_has_work(
        self, session: AsyncSession
    ) -> None:
        """The load-bearing negative: last resort is last, not merely idle-aware.

        ``first`` holds every quorum slot, so the owner's ``second`` is the only
        thing that could use a further slot -- except that an unrelated owner is
        also waiting. The relaxation must not fire, because "a slot is idle" is
        not the predicate; "no other owner has eligible work" is.
        """
        first, second, rival = await self._seed_owner_pair_and_rival(session)
        async with session.begin():
            for index in range(SCORING_QUORUM):
                session.add(
                    ValidatorTicket(
                        agent_id=first,
                        validator_hotkey=f"5Holder-{index}",
                        status=TicketStatus.ISSUED,
                        issued_at=_NOW,
                        deadline=_NOW + _TTL,
                        bench_version=_BENCH,
                        attempt_count=1,
                    )
                )

        async with session.begin():
            served = await issue_ticket(
                session,
                validator_hotkey="5Spare",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )

        assert served is not None
        assert served.agent_id == rival, (
            "an owner's second submission was served while another owner still "
            "had eligible work; the relaxation must be last-resort only"
        )
        async with session.begin():
            assert (
                await session.scalar(
                    select(func.count()).where(ValidatorTicket.agent_id == second)
                )
            ) == 0

    async def test_owner_ceiling_bounds_the_last_resort_pass(
        self, session: AsyncSession
    ) -> None:
        """Even with the fleet otherwise idle, one owner does not take every slot.

        Three generations, one owner, nothing else in the world. The ceiling of
        two lets the second generation start and refuses the third, so headroom
        survives for a submission that becomes eligible a moment later.

        The unit is *submissions*, not leases: once ``second`` is open, further
        spare validators may join it -- that is how it reaches quorum and how
        the owner stops holding two slots -- but no poll may ever open
        ``third``. Draining the whole fleet at it is what proves the bound.
        """
        first, second, rival = await self._seed_owner_pair_and_rival(session)
        third = await _seed_evaluating(
            session, created_at=_NOW + timedelta(minutes=3), name="relax-third"
        )
        async with session.begin():
            agent = await session.get(Agent, third)
            assert agent is not None
            session.add(
                EvaluationPayment(
                    block_hash="0xrelax-3",
                    extrinsic_index=3,
                    agent_id=third,
                    miner_hotkey=agent.miner_hotkey,
                    miner_coldkey="5RelaxColdkey",
                    amount_rao=1,
                    tao_usd_rate=Decimal("1"),
                    dest_address="5Destination",
                    timestamp=_NOW,
                )
            )
            # Retire the rival so this owner is genuinely the only work left.
            rival_agent = await session.get(Agent, rival)
            assert rival_agent is not None
            rival_agent.status = AgentStatus.SCORED
            for index in range(SCORING_QUORUM):
                session.add(
                    ValidatorTicket(
                        agent_id=first,
                        validator_hotkey=f"5Holder-{index}",
                        status=TicketStatus.ISSUED,
                        issued_at=_NOW,
                        deadline=_NOW + _TTL,
                        bench_version=_BENCH,
                        attempt_count=1,
                    )
                )

        async with session.begin():
            opened_second = await issue_ticket(
                session,
                validator_hotkey="5Spare-a",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )
        assert opened_second is not None
        assert opened_second.agent_id == second

        # Drain the rest of the fleet at it. Every one of these polls has the
        # same choice the one above had, and none of them may take ``third``.
        for index in range(SCORING_QUORUM + 2):
            async with session.begin():
                spare = await issue_ticket(
                    session,
                    validator_hotkey=f"5Spare-drain-{index}",
                    now=_NOW,
                    ttl=_TTL,
                    bench_version=_BENCH,
                )
            assert spare is None or spare.agent_id in {first, second}, (
                "the per-owner ceiling did not hold: a third concurrent "
                "submission from one owner was leased even though the limit is "
                "two"
            )

        async with session.begin():
            assert (
                await session.scalar(
                    select(func.count()).where(ValidatorTicket.agent_id == third)
                )
            ) == 0
            live_submissions = (
                await session.scalars(
                    select(ValidatorTicket.agent_id)
                    .where(
                        ValidatorTicket.status == TicketStatus.ISSUED,
                        ValidatorTicket.deadline > _NOW,
                    )
                    .distinct()
                )
            ).all()
        assert set(live_submissions) == {first, second}

    async def test_last_resort_never_gives_one_validator_two_slots_on_one_agent(
        self, session: AsyncSession
    ) -> None:
        """Quorum is untouched: the score PK still bounds a validator to one slot.

        The relaxation is across an owner's *different* submissions. A validator
        that already holds a lease on the owner's first generation may take the
        second, but it must never come back for a second slot on either -- the
        ticket and score primary keys are (agent, version, validator).
        """
        first, second, rival = await self._seed_owner_pair_and_rival(session)
        async with session.begin():
            rival_agent = await session.get(Agent, rival)
            assert rival_agent is not None
            rival_agent.status = AgentStatus.SCORED

        async with session.begin():
            mine = await issue_ticket(
                session,
                validator_hotkey="5Solo",
                now=_NOW,
                ttl=_TTL,
                slot_id="slot-0",
                bench_version=_BENCH,
            )
        assert mine is not None
        assert mine.agent_id == first

        async with session.begin():
            sibling = await issue_ticket(
                session,
                validator_hotkey="5Solo",
                now=_NOW,
                ttl=_TTL,
                slot_id="slot-1",
                bench_version=_BENCH,
            )
        assert sibling is not None
        assert sibling.agent_id == second

        async with session.begin():
            again = await issue_ticket(
                session,
                validator_hotkey="5Solo",
                now=_NOW,
                ttl=_TTL,
                slot_id="slot-2",
                bench_version=_BENCH,
            )

        assert again is None
        async with session.begin():
            for agent_id in (first, second):
                assert (
                    await session.scalar(
                        select(func.count()).where(
                            ValidatorTicket.agent_id == agent_id,
                            ValidatorTicket.validator_hotkey == "5Solo",
                        )
                    )
                ) == 1

    async def test_same_coldkey_legacy_partial_scores_do_not_deadlock(
        self, session: AsyncSession
    ) -> None:
        first = await _seed_evaluating(
            session, created_at=_NOW, name="owner-partial-first"
        )
        second = await _seed_evaluating(
            session,
            created_at=_NOW + timedelta(minutes=1),
            name="owner-partial-second",
        )
        async with session.begin():
            for index, agent_id in enumerate((first, second)):
                agent = await session.get(Agent, agent_id)
                assert agent is not None
                session.add_all(
                    [
                        EvaluationPayment(
                            block_hash=f"0xowner-partial-{index}",
                            extrinsic_index=index,
                            agent_id=agent_id,
                            miner_hotkey=agent.miner_hotkey,
                            miner_coldkey="5SharedPartialColdkey",
                            amount_rao=1,
                            tao_usd_rate=Decimal("1"),
                            dest_address="5Destination",
                            timestamp=_NOW,
                        ),
                        ValidatorTicket(
                            agent_id=agent_id,
                            validator_hotkey=f"5Prior-{index}",
                            status=TicketStatus.SCORED,
                            issued_at=_NOW,
                            deadline=_NOW + _TTL,
                            bench_version=_BENCH,
                            attempt_count=1,
                        ),
                    ]
                )

        async with session.begin():
            first_recovery = await issue_ticket(
                session,
                validator_hotkey="5Recovery-1",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )

        assert first_recovery is not None
        assert first_recovery.agent_id == first

        async with session.begin():
            stored_recovery = await session.get(
                ValidatorTicket, (first, _BENCH, "5Recovery-1")
            )
            assert stored_recovery is not None
            stored_recovery.status = TicketStatus.SCORED

        async with session.begin():
            ineligible_recovery = await issue_ticket(
                session,
                validator_hotkey="5Prior-0",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )
            eligible_recovery = await issue_ticket(
                session,
                validator_hotkey="5Recovery-2",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )

        # ``5Prior-0`` already scored ``first`` and can never contribute to it
        # again -- the score PK is (agent, version, validator). With no other
        # owner in this world its slot would otherwise idle, so the last-resort
        # pass hands it the owner's second generation instead. It still did not
        # get a second slot on ``first``, which is the deadlock this guards.
        assert ineligible_recovery is not None
        assert ineligible_recovery.agent_id == second
        assert eligible_recovery is not None
        assert eligible_recovery.agent_id == first

    async def test_legacy_candidate_serializes_against_paid_same_hotkey(
        self, session: AsyncSession
    ) -> None:
        paid = await _seed_evaluating(session, created_at=_NOW, name="paid-owner")
        legacy = await _seed_evaluating(
            session,
            created_at=_NOW + timedelta(minutes=1),
            name="legacy-owner",
        )
        async with session.begin():
            paid_agent = await session.get(Agent, paid)
            legacy_agent = await session.get(Agent, legacy)
            assert paid_agent is not None
            assert legacy_agent is not None
            legacy_agent.miner_hotkey = paid_agent.miner_hotkey
            session.add(
                EvaluationPayment(
                    block_hash="0xpaid-owner",
                    extrinsic_index=0,
                    agent_id=paid,
                    miner_hotkey=paid_agent.miner_hotkey,
                    miner_coldkey="5SharedColdkey",
                    amount_rao=1,
                    tao_usd_rate=Decimal("1"),
                    dest_address="5Destination",
                    timestamp=_NOW,
                )
            )

        async with session.begin():
            first_claim = await issue_ticket(
                session,
                validator_hotkey="5PaidClaim",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )
            second_claim = await issue_ticket(
                session,
                validator_hotkey="5LegacyClaim",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )

        assert first_claim is not None
        assert first_claim.agent_id == paid
        assert second_claim is not None
        assert second_claim.agent_id == paid

    async def test_live_sibling_blocks_across_status_and_benchmark_version(
        self, session: AsyncSession
    ) -> None:
        """A live sibling is found across era and status, and costs the owner
        its turn while any other owner has work.

        The linkage under test is the awkward one: the sibling holding the lease
        is in ``SCREENING`` and its ticket is for a different benchmark version,
        so neither the candidate filter nor the version-scoped queries would
        surface it. A rival owner is present so that "candidate was not leased"
        means the rail refused it, rather than merely that the fixture had
        nothing else to offer.
        """
        live = await _seed_evaluating(session, created_at=_NOW, name="live-owner")
        candidate = await _seed_evaluating(
            session,
            created_at=_NOW + timedelta(minutes=1),
            name="candidate-owner",
        )
        rival = await _seed_evaluating(
            session,
            created_at=_NOW + timedelta(minutes=2),
            name="rival-owner",
        )
        async with session.begin():
            for index, agent_id in enumerate((live, candidate)):
                agent = await session.get(Agent, agent_id)
                assert agent is not None
                session.add(
                    EvaluationPayment(
                        block_hash=f"0xcross-version-owner-{index}",
                        extrinsic_index=index,
                        agent_id=agent_id,
                        miner_hotkey=agent.miner_hotkey,
                        miner_coldkey="5CrossVersionColdkey",
                        amount_rao=1,
                        tao_usd_rate=Decimal("1"),
                        dest_address="5Destination",
                        timestamp=_NOW,
                    )
                )
            live_agent = await session.get(Agent, live)
            assert live_agent is not None
            live_agent.status = AgentStatus.SCREENING
        # The sibling's lease belongs to the era the fleet came off, which is
        # the half of this shape that made the defect invisible: it is neither
        # a candidate the current-era queries return nor a ticket they count.
        await _seed_retired_era_lease(
            session,
            agent_id=live,
            validator_hotkey="5LiveOtherEra",
            issued_at=_NOW,
            deadline=_NOW + _TTL,
        )

        async with session.begin():
            served = await issue_ticket(
                session,
                validator_hotkey="5CurrentEra",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )

        # ``candidate`` outranks ``rival`` on FIFO, so the allocator reached it
        # first and passed over it: the owner's cross-era, cross-status live
        # lease still costs them their turn while anyone else is waiting.
        assert served is not None
        assert served.agent_id == rival
        assert (
            await session.get(ValidatorTicket, (candidate, _BENCH, "5CurrentEra"))
            is None
        )

    async def test_fresh_lane_excludes_pre_rollout_backlog(
        self, session: AsyncSession
    ) -> None:
        rollout_started = _NOW - timedelta(minutes=5)
        old = await _seed_evaluating(
            session,
            created_at=rollout_started - timedelta(days=1),
            name="old",
            screened=True,
            dataset_version=None,
        )
        fresh = await _seed_evaluating(
            session,
            created_at=rollout_started + timedelta(minutes=1),
            name="fresh",
            screened=True,
            dataset_version=None,
        )
        async with session.begin():
            for agent_id in (old, fresh):
                session.add(
                    BenchmarkDataset(
                        agent_id=agent_id,
                        bench_version=_BENCH,
                        seed=123,
                        sha256="cd" * 32,
                        run_size="full",
                    )
                )

        async with session.begin():
            ticket = await issue_ticket(
                session,
                validator_hotkey="5Fresh",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
                submitted_at_or_after=rollout_started,
                fifo_start_at=rollout_started,
            )

        assert ticket is not None
        assert ticket.agent_id == fresh
        assert ticket.agent_id != old

    async def test_new_benchmark_resets_fifo_age_to_rollout_start(
        self, session: AsyncSession
    ) -> None:
        rollout_started = _NOW - timedelta(minutes=5)
        lower_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        higher_id = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
        async with session.begin():
            rollout_id = uuid4()
            session.add(
                BenchmarkRollout(
                    rollout_id=rollout_id,
                    from_version=_RETIRED_BENCH,
                    desired_version=_BENCH,
                    status="activated",
                    cohort_size=5,
                    created_at=rollout_started,
                    activated_at=rollout_started,
                )
            )
            for agent_id, created_at in (
                (higher_id, rollout_started - timedelta(days=2)),
                (lower_id, rollout_started - timedelta(days=1)),
            ):
                session.add(
                    Agent(
                        agent_id=agent_id,
                        miner_hotkey=f"5Miner-{agent_id}",
                        name=str(agent_id),
                        sha256=f"{agent_id.int:064x}",
                        status=AgentStatus.EVALUATING,
                        screening_policy_version=9,
                        screened_image_sha256="12" * 32,
                        screened_image_size_bytes=123,
                        screened_image_id="sha256:" + "34" * 32,
                        screened_image_ref=f"ditto-screen/{agent_id}:latest",
                        screened_image_upload_id=uuid4(),
                        screened_image_verified_at=_NOW,
                        created_at=created_at,
                    )
                )
                session.add(
                    BenchmarkDataset(
                        agent_id=agent_id,
                        bench_version=_BENCH,
                        seed=123,
                        sha256="ef" * 32,
                        run_size="full",
                    )
                )
                session.add(
                    BenchmarkRolloutMember(
                        rollout_id=rollout_id,
                        agent_id=agent_id,
                        position=1 if agent_id == higher_id else 2,
                        frozen_miner_hotkey=f"5Miner-{agent_id}",
                        frozen_composite=0.5,
                    )
                )

        async with session.begin():
            ticket = await issue_ticket(
                session,
                validator_hotkey="5EraFIFO",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )

        assert ticket is not None
        assert ticket.agent_id == lower_id

    async def test_activated_era_skips_old_nonmember_with_backfilled_dataset(
        self, session: AsyncSession
    ) -> None:
        rollout_started = _NOW - timedelta(minutes=5)
        old = await _seed_evaluating(
            session,
            created_at=rollout_started - timedelta(days=1),
            name="old-nonmember",
            screened=True,
            dataset_version=None,
        )
        fresh = await _seed_evaluating(
            session,
            created_at=rollout_started + timedelta(minutes=1),
            name="fresh",
            screened=True,
            dataset_version=None,
        )
        async with session.begin():
            session.add(
                BenchmarkRollout(
                    rollout_id=uuid4(),
                    from_version=_RETIRED_BENCH,
                    desired_version=_BENCH,
                    status="activated",
                    cohort_size=5,
                    created_at=rollout_started,
                    activated_at=rollout_started,
                )
            )
            for agent_id in (old, fresh):
                session.add(
                    BenchmarkDataset(
                        agent_id=agent_id,
                        bench_version=_BENCH,
                        seed=123,
                        sha256="ef" * 32,
                        run_size="full",
                    )
                )
            session.add(
                ValidatorTicket(
                    agent_id=old,
                    validator_hotkey="5HistoricalRecovery",
                    bench_version=_BENCH,
                    status=TicketStatus.EXPIRED,
                    issued_at=rollout_started - timedelta(hours=2),
                    deadline=rollout_started - timedelta(hours=1),
                    retry_after=rollout_started,
                    attempt_count=2,
                    manual_retry_grants=1,
                )
            )

        async with session.begin():
            ticket = await issue_ticket(
                session,
                validator_hotkey="5EraAdmission",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )

        assert ticket is not None
        assert ticket.agent_id == fresh
        assert ticket.agent_id != old

    async def test_inadmissible_owner_history_does_not_pin_fresh_sibling(
        self, session: AsyncSession
    ) -> None:
        rollout_started = _NOW - timedelta(minutes=5)
        old = await _seed_evaluating(
            session,
            created_at=rollout_started - timedelta(days=1),
            name="old-owner-generation",
            screened=True,
            dataset_version=None,
        )
        fresh = await _seed_evaluating(
            session,
            created_at=rollout_started + timedelta(minutes=1),
            name="fresh-owner-generation",
            screened=True,
            dataset_version=None,
        )
        async with session.begin():
            session.add(
                BenchmarkRollout(
                    rollout_id=uuid4(),
                    from_version=_RETIRED_BENCH,
                    desired_version=_BENCH,
                    status="activated",
                    cohort_size=5,
                    created_at=rollout_started,
                    activated_at=rollout_started,
                )
            )
            for index, agent_id in enumerate((old, fresh)):
                agent = await session.get(Agent, agent_id)
                assert agent is not None
                session.add_all(
                    [
                        BenchmarkDataset(
                            agent_id=agent_id,
                            bench_version=_BENCH,
                            seed=123 + index,
                            sha256=f"{index + 1:02x}" * 32,
                            run_size="full",
                        ),
                        EvaluationPayment(
                            block_hash=f"0xera-owner-{index}",
                            extrinsic_index=index,
                            agent_id=agent_id,
                            miner_hotkey=agent.miner_hotkey,
                            miner_coldkey="5SharedEraColdkey",
                            amount_rao=1,
                            tao_usd_rate=Decimal("1"),
                            dest_address="5Destination",
                            timestamp=_NOW,
                        ),
                    ]
                )
            session.add(
                ValidatorTicket(
                    agent_id=old,
                    validator_hotkey="5HistoricalOwnerScore",
                    bench_version=_BENCH,
                    status=TicketStatus.SCORED,
                    issued_at=rollout_started - timedelta(hours=1),
                    deadline=rollout_started,
                    attempt_count=1,
                    manual_retry_grants=1,
                )
            )

        async with session.begin():
            ticket = await issue_ticket(
                session,
                validator_hotkey="5FreshOwnerValidator",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )

        assert ticket is not None
        assert ticket.agent_id == fresh

    async def test_unreachable_pinned_generation_falls_through_to_sibling(
        self, session: AsyncSession
    ) -> None:
        """The pin asks "can this still finish?", not "did it start first?".

        The live incident: an owner's oldest generation had burned every
        capable validator's retry budget, so it could never collect
        ``SCORING_QUORUM`` scores -- yet its first-started progress kept it
        pinned, and the owner's healthy newer submission was never leased while
        fleet slots sat idle.
        """
        dead, healthy = await _seed_owner_generations(
            session, older="dead-generation", newer="healthy-generation"
        )
        async with session.begin():
            for hotkey in _CAPABLE_FLEET:
                await _seed_capable_heartbeat(session, validator_hotkey=hotkey)
            # One score banked, then both remaining capable validators spent
            # their whole budget on it. Ceiling is 1 of a required 3.
            session.add(
                _owner_ticket(dead, _CAPABLE_FLEET[0], status=TicketStatus.SCORED)
            )
            for hotkey in _CAPABLE_FLEET[1:]:
                session.add(_owner_ticket(dead, hotkey, status=TicketStatus.EXPIRED))
            # The sibling started progress later, so it loses the pin on
            # ``min(issued_at)`` and can only win it by being the reachable
            # one. Ceiling is 3 of a required 3.
            session.add(
                _owner_ticket(
                    healthy,
                    _CAPABLE_FLEET[0],
                    status=TicketStatus.SCORED,
                    started_after=timedelta(hours=3),
                )
            )

        async with session.begin():
            ticket = await issue_ticket(
                session,
                validator_hotkey=_CAPABLE_FLEET[1],
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )

        assert ticket is not None
        assert ticket.agent_id == healthy

    async def test_reachable_pinned_generation_still_pins_sibling(
        self, session: AsyncSession
    ) -> None:
        """No regression on quorum completion: only *provably* dead rows fall through.

        A recorded score, a live lease and a retry cooldown all still lead to a
        score, so none of them may cost a generation its pin. This is why the
        term counts spent retry budgets rather than reusing
        ``desired_era_work_outstanding``'s "could a validator take this right
        now" -- that question answers "no" here, and unpinning mid-quorum is
        exactly the diversion pinning exists to prevent.
        """
        pinned, sibling = await _seed_owner_generations(
            session, older="pinned-generation", newer="younger-sibling"
        )
        async with session.begin():
            for hotkey in _CAPABLE_FLEET:
                await _seed_capable_heartbeat(session, validator_hotkey=hotkey)
            session.add(
                _owner_ticket(pinned, _CAPABLE_FLEET[0], status=TicketStatus.SCORED)
            )
            # Cooling down, not exhausted: this validator comes back, so the
            # ceiling is still 3 of a required 3 and the pin must survive.
            session.add(
                _owner_ticket(
                    pinned,
                    _CAPABLE_FLEET[2],
                    status=TicketStatus.EXPIRED,
                    attempt_count=1,
                    retry_after=_NOW + timedelta(hours=1),
                )
            )
            # Reachable and pin-selectable, so it takes the owner's slot the
            # moment the older generation is wrongly judged dead.
            session.add(
                _owner_ticket(
                    sibling,
                    _CAPABLE_FLEET[0],
                    status=TicketStatus.SCORED,
                    started_after=timedelta(hours=3),
                )
            )

        async with session.begin():
            ticket = await issue_ticket(
                session,
                validator_hotkey=_CAPABLE_FLEET[1],
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )

        assert ticket is not None
        assert ticket.agent_id == pinned
        assert ticket.agent_id != sibling

    async def test_live_lease_still_bounds_owner_even_when_unreachable(
        self, session: AsyncSession
    ) -> None:
        """One live lease per owner outranks the new fall-through.

        With nine concurrent fleet slots, the live-sibling rail is the only
        thing stopping one owner monopolising the fleet, so a generation losing
        its pin must never let a sibling start on the ordinary pass while a
        lease is still out on it. Here the older generation is genuinely
        unreachable *and* still holds a live lease.

        Pinned to the ceiling rather than to an outcome, because the two answers
        are the whole point of the relaxation: at ``1`` the rail is absolute and
        the sibling stays shut out, exactly as before. At the shipped ceiling
        the sibling is leased only after the allocator has proven it has nothing
        else to do -- this fixture contains no other owner -- and one live lease
        per owner still governs every poll that does.
        """
        leased, sibling = await _seed_owner_generations(
            session, older="leased-generation", newer="waiting-sibling"
        )
        async with session.begin():
            for hotkey in _CAPABLE_FLEET:
                await _seed_capable_heartbeat(session, validator_hotkey=hotkey)
            session.add(
                _owner_ticket(leased, _CAPABLE_FLEET[0], status=TicketStatus.SCORED)
            )
            session.add(
                _owner_ticket(leased, _CAPABLE_FLEET[1], status=TicketStatus.EXPIRED)
            )
            # Ceiling is 2 of a required 3, so the pin is gone -- but this
            # lease is still live and the owner's slot is still occupied.
            session.add(
                _owner_ticket(
                    leased,
                    _CAPABLE_FLEET[2],
                    status=TicketStatus.ISSUED,
                    deadline=_NOW + timedelta(minutes=20),
                )
            )

        async with session.begin():
            strict = await issue_ticket(
                session,
                validator_hotkey=_CAPABLE_FLEET[1],
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
                owner_concurrent_submission_limit=1,
            )

        assert strict is None
        async with session.begin():
            assert (
                await session.scalar(
                    select(func.count()).where(ValidatorTicket.agent_id == sibling)
                )
            ) == 0

        async with session.begin():
            relaxed = await issue_ticket(
                session,
                validator_hotkey=_CAPABLE_FLEET[1],
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )

        assert relaxed is not None
        assert relaxed.agent_id == sibling

    async def test_pinned_generation_outranks_its_own_relaxed_sibling(
        self, session: AsyncSession
    ) -> None:
        """The relaxation must not resurrect mid-quorum diversion through the back door.

        Once a sibling may hold a lease, the owner's live-lease set is no longer
        a single row -- and the naive rail would then gate the *pinned*
        generation behind the sibling it just let start. That is precisely the
        diversion #467's pin exists to prevent, arriving from the other side:
        the fleet would abandon a 1-of-3 generation to finish a newer one.

        So the pinned generation is exempt from the ceiling. A capable validator
        that can still score it gets it, sibling lease or not.

        A rival owner is in the fixture on purpose: without one the allocator
        reaches ``pinned`` again on its own last-resort pass and the bug is
        invisible. With one, losing the exemption means the fleet walks past a
        1-of-3 generation to serve somebody else -- which is the starvation.
        """
        pinned, sibling = await _seed_owner_generations(
            session, older="pinned-progress", newer="relaxed-sibling"
        )
        rival = await _seed_evaluating(
            session,
            created_at=_OWNER_ROLLOUT_STARTED + timedelta(hours=5),
            name="rival-generation",
            screened=True,
            dataset_version=None,
        )
        async with session.begin():
            session.add(
                BenchmarkDataset(
                    agent_id=rival,
                    bench_version=_BENCH,
                    seed=999,
                    sha256="99" * 32,
                    run_size="full",
                )
            )
        async with session.begin():
            for hotkey in _CAPABLE_FLEET:
                await _seed_capable_heartbeat(session, validator_hotkey=hotkey)
            # One accepted score: progress started, and with two capable
            # validators left the generation is still reachable, so it pins.
            session.add(
                _owner_ticket(pinned, _CAPABLE_FLEET[0], status=TicketStatus.SCORED)
            )
            # The sibling holds the lease the last-resort pass would have given
            # it, which is what makes the owner's live-lease set non-trivial.
            session.add(
                _owner_ticket(
                    sibling,
                    _CAPABLE_FLEET[2],
                    status=TicketStatus.ISSUED,
                    deadline=_NOW + timedelta(minutes=20),
                    started_after=timedelta(hours=3),
                )
            )

        async with session.begin():
            ticket = await issue_ticket(
                session,
                validator_hotkey=_CAPABLE_FLEET[1],
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )

        assert ticket is not None
        assert ticket.agent_id == pinned, (
            "a relaxed sibling lease locked out the generation the owner is "
            "pinned to; the fleet walked past it to serve "
            f"{'the rival owner' if ticket.agent_id == rival else 'other work'} "
            "and would abandon it mid-quorum"
        )

    async def test_below_top_ten_owner_history_does_not_pin_fresh_sibling(
        self, session: AsyncSession
    ) -> None:
        await _seed_finalized_top_ten(session, tenth_place=0.80)
        old = await _seed_evaluating(
            session,
            created_at=_NOW - timedelta(hours=2),
            name="weak-old-generation",
        )
        fresh = await _seed_evaluating(
            session,
            created_at=_NOW - timedelta(hours=1),
            name="fresh-generation",
        )
        async with session.begin():
            for index, agent_id in enumerate((old, fresh)):
                agent = await session.get(Agent, agent_id)
                assert agent is not None
                session.add(
                    EvaluationPayment(
                        block_hash=f"0xweak-owner-{index}",
                        extrinsic_index=index,
                        agent_id=agent_id,
                        miner_hotkey=agent.miner_hotkey,
                        miner_coldkey="5" + "C" * 47,
                        amount_rao=1,
                        tao_usd_rate=Decimal("1"),
                        dest_address="5Destination",
                        timestamp=_NOW,
                    )
                )
            for index, composite in enumerate((0.10, 0.20)):
                validator = f"5WeakOwnerScore-{index}"
                session.add_all(
                    [
                        ValidatorTicket(
                            agent_id=old,
                            validator_hotkey=validator,
                            status=TicketStatus.SCORED,
                            issued_at=_NOW - timedelta(hours=1),
                            deadline=_NOW,
                            bench_version=_BENCH,
                            attempt_count=1,
                        ),
                        Score(
                            agent_id=old,
                            validator_hotkey=validator,
                            run_id=f"weak-owner-{index}",
                            signature=None,
                            seed=123,
                            composite=composite,
                            tool_mean=composite,
                            memory_mean=composite,
                            median_ms=100,
                            n=114,
                            details=None,
                            generated_at=_NOW - timedelta(hours=1),
                        ),
                    ]
                )

        async with session.begin():
            ticket = await issue_ticket(
                session,
                validator_hotkey="5FreshOwnerValidator",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )

        assert ticket is not None
        assert ticket.agent_id == fresh

    async def test_activated_era_expires_idle_old_nonmember_lease(
        self, session: AsyncSession
    ) -> None:
        rollout_started = _NOW - timedelta(minutes=5)
        old = await _seed_evaluating(
            session,
            created_at=rollout_started - timedelta(days=1),
            name="leased-old-nonmember",
            screened=True,
            dataset_version=None,
        )
        async with session.begin():
            session.add(
                BenchmarkRollout(
                    rollout_id=uuid4(),
                    from_version=_RETIRED_BENCH,
                    desired_version=_BENCH,
                    status="activated",
                    cohort_size=5,
                    created_at=rollout_started,
                    activated_at=rollout_started,
                )
            )
            session.add(
                BenchmarkDataset(
                    agent_id=old,
                    bench_version=_BENCH,
                    seed=123,
                    sha256="ef" * 32,
                    run_size="full",
                )
            )
            session.add(
                ValidatorTicket(
                    agent_id=old,
                    validator_hotkey="5EraAdmission",
                    bench_version=_BENCH,
                    slot_id="slot-0",
                    status=TicketStatus.ISSUED,
                    issued_at=_NOW - timedelta(minutes=1),
                    deadline=_AFTER_REPORTING_GRACE + _TTL,
                    first_reported_at=_NOW,
                )
            )
            # The validator is heartbeating right now and advertising no work on
            # the slot: real, fresh, post-issuance evidence that it is idle.
            await _seed_heartbeat(
                session,
                validator_hotkey="5EraAdmission",
                seen_at=_AFTER_REPORTING_GRACE - timedelta(seconds=30),
            )

        async with session.begin():
            ticket = await issue_ticket(
                session,
                validator_hotkey="5EraAdmission",
                now=_AFTER_REPORTING_GRACE,
                ttl=_TTL,
                bench_version=_BENCH,
                validator_running_benchmark=False,
            )

        assert ticket is None
        expired = await session.get(ValidatorTicket, (old, _BENCH, "5EraAdmission"))
        assert expired is not None
        assert expired.status == TicketStatus.EXPIRED
        assert expired.deadline.replace(tzinfo=UTC) == _AFTER_REPORTING_GRACE

    async def test_screened_only_skips_source_only_agent(
        self, session: AsyncSession
    ) -> None:
        """The older source-only row is passed over for the screened one.

        Under a contract that requires a verified image the exclusion is now
        doubly enforced -- the artifact mode asks for it and the contract
        insists on it -- so this reads as a guard against either one being
        dropped, not as the artifact mode carrying the rule alone.
        """
        source_id = await _seed_evaluating(session, created_at=_NOW, screened=False)
        image_id = await _seed_evaluating(
            session, created_at=_NOW + timedelta(seconds=1), screened=True
        )
        async with session.begin():
            ticket = await issue_ticket(
                session,
                validator_hotkey="5ImageOnly",
                now=_NOW,
                ttl=_TTL,
                artifact_mode="screened_only",
                bench_version=_BENCH,
            )
        assert ticket is not None
        assert ticket.agent_id == image_id
        assert ticket.agent_id != source_id

    async def test_prefer_screened_no_longer_falls_back_to_source(
        self, session: AsyncSession
    ) -> None:
        """``prefer_screened`` has no source lane left to fall back to.

        This asserted the opposite while v2 was leasable: v2 was the one
        contract with ``requires_screened_image`` false, so a validator that
        merely preferred screened images would still be handed a source-only
        submission rather than sit idle. The bench-version floor retired v2,
        and every contract at or above it requires a verified image, so the
        fallback branch in ``queue_candidate_predicate`` can no longer be
        reached by any leasable era. Inverted rather than deleted because the
        branch is still in the code: if a future contract reopens the source
        lane, that is a decision, and it should have to change this test.
        """
        await _seed_evaluating(session, screened=False)
        async with session.begin():
            ticket = await issue_ticket(
                session,
                validator_hotkey="5Prefer",
                now=_NOW,
                ttl=_TTL,
                artifact_mode="prefer_screened",
                bench_version=_BENCH,
            )
        assert ticket is None

    async def test_prefer_screened_prioritizes_complete_verified_tuple(
        self, session: AsyncSession
    ) -> None:
        await _seed_evaluating(session, created_at=_NOW, screened=False)
        image_id = await _seed_evaluating(
            session, created_at=_NOW + timedelta(seconds=1), screened=True
        )
        async with session.begin():
            ticket = await issue_ticket(
                session,
                validator_hotkey="5Prefer",
                now=_NOW,
                ttl=_TTL,
                artifact_mode="prefer_screened",
                bench_version=_BENCH,
            )
        assert ticket is not None
        assert ticket.agent_id == image_id

    async def test_screened_only_releases_idle_incompatible_lease(
        self, session: AsyncSession
    ) -> None:
        """A retired-era lease on an idle slot is released, not held to term.

        The incompatibility used to be an artifact one: a source-build lease
        against a validator that had flipped to ``screened_only``. Every era at
        or above the bench-version floor requires a verified screened image, so
        the artifact contract can no longer differ between two live polls and
        the era is what differs instead -- which is the same branch, and the one
        that actually fires in production during a benchmark transition.
        """
        source_id = await _seed_evaluating(
            session, screened=False, dataset_version=_RETIRED_BENCH
        )
        await _seed_retired_era_lease(
            session,
            agent_id=source_id,
            validator_hotkey="5Transition",
            issued_at=_NOW,
            deadline=_NOW + timedelta(hours=2),
            # The lease started and was seen running; what makes it releasable
            # is that the validator now reports the slot empty, not that time
            # passed.
            reported_at=_NOW + timedelta(minutes=1),
        )
        async with session.begin():
            await _seed_heartbeat(
                session,
                validator_hotkey="5Transition",
                seen_at=_AFTER_REPORTING_GRACE - timedelta(seconds=30),
            )

        async with session.begin():
            replacement = await issue_ticket(
                session,
                validator_hotkey="5Transition",
                now=_AFTER_REPORTING_GRACE,
                ttl=_TTL,
                artifact_mode="screened_only",
                validator_running_benchmark=False,
                bench_version=_BENCH,
            )
        assert replacement is None
        async with session.begin():
            released = await session.get(
                ValidatorTicket, (source_id, _RETIRED_BENCH, "5Transition")
            )
            assert released is not None
            assert released.status == TicketStatus.EXPIRED

    async def test_screened_only_preserves_actively_running_incompatible_lease(
        self, session: AsyncSession
    ) -> None:
        source_id = await _seed_evaluating(
            session, screened=False, dataset_version=_RETIRED_BENCH
        )
        await _seed_retired_era_lease(
            session,
            agent_id=source_id,
            validator_hotkey="5Running",
            issued_at=_NOW,
            deadline=_NOW + _TTL,
        )
        async with session.begin():
            replacement = await issue_ticket(
                session,
                validator_hotkey="5Running",
                now=_NOW + timedelta(seconds=1),
                ttl=_TTL,
                artifact_mode="screened_only",
                validator_running_benchmark=True,
                bench_version=_BENCH,
            )
        assert replacement is None
        async with session.begin():
            preserved = await session.get(
                ValidatorTicket, (source_id, _RETIRED_BENCH, "5Running")
            )
            assert preserved is not None
            assert preserved.status == TicketStatus.ISSUED

    async def test_stale_heartbeat_never_revokes_a_live_lease(
        self, session: AsyncSession
    ) -> None:
        """The v7 run-loss bug: heartbeat ingest breaks, the stored capacity blob
        freezes with the slot absent, and the next job claim destroys a healthy
        19-minute benchmark. Staleness is not evidence of idleness."""
        source_id = await _seed_evaluating(
            session, screened=False, dataset_version=_RETIRED_BENCH
        )
        await _seed_retired_era_lease(
            session,
            agent_id=source_id,
            validator_hotkey="5Frozen",
            issued_at=_NOW,
            deadline=_NOW + timedelta(hours=2),
            # It was running before the silence began, so that silence is real
            # evidence rather than a run that never announced itself.
            reported_at=_NOW + timedelta(minutes=1),
        )
        async with session.begin():
            # Ingest died 19 minutes ago; the blob it left behind predates the
            # run and shows no active slot. Exactly the frozen row that read as
            # "slot free" while the validator logged `scoring continues`.
            await _seed_heartbeat(
                session,
                validator_hotkey="5Frozen",
                seen_at=_NOW - timedelta(seconds=30),
            )

        claimed_at = _NOW + timedelta(minutes=19)
        async with session.begin():
            replacement = await issue_ticket(
                session,
                validator_hotkey="5Frozen",
                now=claimed_at,
                ttl=_TTL,
                artifact_mode="screened_only",
                validator_running_benchmark=False,
                bench_version=_BENCH,
            )

        assert replacement is None
        async with session.begin():
            live = await session.get(
                ValidatorTicket, (source_id, _RETIRED_BENCH, "5Frozen")
            )
            assert live is not None
            assert live.status == TicketStatus.ISSUED
            assert live.deadline.replace(tzinfo=UTC) == _NOW + timedelta(hours=2)
            assert (
                await session.scalar(
                    select(func.count()).select_from(ValidatorLeaseAudit)
                )
                == 0
            )

    async def test_fresh_heartbeat_reporting_the_slot_active_keeps_the_lease(
        self, session: AsyncSession
    ) -> None:
        """Ingest is healthy and the blob names the slot: unambiguously alive."""
        source_id = await _seed_evaluating(
            session, screened=False, dataset_version=_RETIRED_BENCH
        )
        await _seed_retired_era_lease(
            session,
            agent_id=source_id,
            validator_hotkey="5Busy",
            issued_at=_NOW,
            deadline=_NOW + timedelta(hours=2),
        )
        async with session.begin():
            await _seed_heartbeat(
                session,
                validator_hotkey="5Busy",
                seen_at=_AFTER_REPORTING_GRACE - timedelta(seconds=10),
                state="running_benchmark",
                active=(
                    _active_slot(
                        source_id,
                        ticket_deadline=_NOW + timedelta(hours=2),
                        bench_version=_RETIRED_BENCH,
                    ),
                ),
            )

        async with session.begin():
            replacement = await issue_ticket(
                session,
                validator_hotkey="5Busy",
                now=_AFTER_REPORTING_GRACE,
                ttl=_TTL,
                artifact_mode="screened_only",
                validator_running_benchmark=False,
                bench_version=_BENCH,
            )

        assert replacement is None
        async with session.begin():
            live = await session.get(
                ValidatorTicket, (source_id, _RETIRED_BENCH, "5Busy")
            )
            assert live is not None
            assert live.status == TicketStatus.ISSUED

    async def test_idle_blob_predating_the_lease_cannot_revoke_it(
        self, session: AsyncSession
    ) -> None:
        """A validator that just claimed work has not had time to advertise the
        slot, so "not active" describes the moment before the run, not the run."""
        source_id = await _seed_evaluating(
            session, screened=False, dataset_version=_RETIRED_BENCH
        )
        await _seed_retired_era_lease(
            session,
            agent_id=source_id,
            validator_hotkey="5Starting",
            issued_at=_NOW,
            deadline=_NOW + timedelta(hours=2),
        )
        async with session.begin():
            await _seed_heartbeat(
                session,
                validator_hotkey="5Starting",
                seen_at=_NOW + timedelta(minutes=1),
            )

        async with session.begin():
            replacement = await issue_ticket(
                session,
                validator_hotkey="5Starting",
                now=_NOW + timedelta(minutes=1, seconds=30),
                ttl=_TTL,
                artifact_mode="screened_only",
                validator_running_benchmark=False,
                bench_version=_BENCH,
            )

        assert replacement is None
        async with session.begin():
            live = await session.get(
                ValidatorTicket, (source_id, _RETIRED_BENCH, "5Starting")
            )
            assert live is not None
            assert live.status == TicketStatus.ISSUED

    async def test_abandoned_lease_is_still_reclaimed_and_audited(
        self, session: AsyncSession
    ) -> None:
        """The reclaim path must not regress into leaking slots forever: a
        validator that restarted keeps heartbeating, so it proves its own slot
        idle and gets it back on the next claim, with an audit row explaining
        why."""
        source_id = await _seed_evaluating(
            session, screened=False, dataset_version=_RETIRED_BENCH
        )
        await _seed_retired_era_lease(
            session,
            agent_id=source_id,
            validator_hotkey="5Restarted",
            issued_at=_NOW,
            deadline=_NOW + timedelta(hours=2),
            # It was running before the silence began, so that silence is real
            # evidence rather than a run that never announced itself.
            reported_at=_NOW + timedelta(minutes=1),
        )
        async with session.begin():
            await _seed_heartbeat(
                session,
                validator_hotkey="5Restarted",
                seen_at=_AFTER_REPORTING_GRACE - timedelta(seconds=30),
            )

        async with session.begin():
            replacement = await issue_ticket(
                session,
                validator_hotkey="5Restarted",
                now=_AFTER_REPORTING_GRACE,
                ttl=_TTL,
                artifact_mode="screened_only",
                validator_running_benchmark=False,
                bench_version=_BENCH,
            )

        assert replacement is None
        async with session.begin():
            reclaimed = await session.get(
                ValidatorTicket, (source_id, _RETIRED_BENCH, "5Restarted")
            )
            assert reclaimed is not None
            assert reclaimed.status == TicketStatus.EXPIRED
            assert reclaimed.deadline.replace(tzinfo=UTC) == _AFTER_REPORTING_GRACE
            audit = (await session.scalars(select(ValidatorLeaseAudit))).all()
        assert len(audit) == 1
        entry = audit[0]
        assert entry.agent_id == source_id
        assert entry.validator_hotkey == "5Restarted"
        assert entry.action == "force_expired"
        assert entry.reason == "idle_capacity_reports_slot_free"
        assert entry.context == "issue_ticket"
        assert entry.evidence["heartbeat_age_seconds"] == 30.0
        assert (
            entry.evidence["lease_age_seconds"]
            == LEASE_REPORTING_GRACE.total_seconds() + 60
        )
        assert entry.evidence["active_slot_ids"] == []

    async def test_heartbeat_just_past_the_freshness_window_is_not_evidence(
        self, session: AsyncSession
    ) -> None:
        """The boundary that decides whether a run lives: one second past the
        window and the blob stops counting as proof."""
        source_id = await _seed_evaluating(
            session, screened=False, dataset_version=_RETIRED_BENCH
        )
        await _seed_retired_era_lease(
            session,
            agent_id=source_id,
            validator_hotkey="5Boundary",
            issued_at=_NOW,
            deadline=_NOW + timedelta(hours=2),
        )
        claimed_at = _AFTER_REPORTING_GRACE
        async with session.begin():
            await _seed_heartbeat(
                session,
                validator_hotkey="5Boundary",
                seen_at=claimed_at - IDLE_EVIDENCE_MAX_AGE - timedelta(seconds=1),
            )

        async with session.begin():
            replacement = await issue_ticket(
                session,
                validator_hotkey="5Boundary",
                now=claimed_at,
                ttl=_TTL,
                artifact_mode="screened_only",
                validator_running_benchmark=False,
                bench_version=_BENCH,
            )

        assert replacement is None
        async with session.begin():
            live = await session.get(
                ValidatorTicket, (source_id, _RETIRED_BENCH, "5Boundary")
            )
            assert live is not None
            assert live.status == TicketStatus.ISSUED

    @pytest.mark.parametrize(
        "status",
        (
            AgentStatus.ATH_PENDING_REVIEW,
            AgentStatus.QUARANTINED,
            AgentStatus.REJECTED,
        ),
    )
    async def test_terminal_review_states_do_not_receive_new_tickets(
        self, session: AsyncSession, status: AgentStatus
    ) -> None:
        aid = await _seed_evaluating(session)
        async with session.begin():
            agent = await session.get(Agent, aid)
            assert agent is not None
            agent.status = status

        async with session.begin():
            ticket = await issue_ticket(
                session,
                validator_hotkey="5V1",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )

        assert ticket is None

    async def test_skips_agent_that_needs_rescreening(
        self, session: AsyncSession
    ) -> None:
        aid = await _seed_evaluating(session)
        async with session.begin():
            agent = await session.get(Agent, aid)
            assert agent is not None
            agent.screening_policy_version = 0
        async with session.begin():
            ticket = await issue_ticket(
                session,
                validator_hotkey="5V1",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )
        assert ticket is None

    async def test_seats_ticket_for_evaluating_agent(
        self, session: AsyncSession
    ) -> None:
        aid = await _seed_evaluating(session)
        async with session.begin():
            t = await issue_ticket(
                session,
                validator_hotkey="5V1",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )
        assert t is not None
        assert t.agent_id == aid
        assert t.status == TicketStatus.ISSUED
        assert t.deadline == _NOW + _TTL

    async def test_low_first_score_still_receives_a_second_ticket(
        self, session: AsyncSession
    ) -> None:
        await _seed_finalized_top_five(session)
        aid = await _seed_evaluating(session)
        async with session.begin():
            session.add(
                ValidatorTicket(
                    agent_id=aid,
                    validator_hotkey="5Scored",
                    status=TicketStatus.SCORED,
                    issued_at=_NOW,
                    deadline=_NOW + _TTL,
                    bench_version=_BENCH,
                    attempt_count=1,
                )
            )
            session.add(
                Score(
                    agent_id=aid,
                    validator_hotkey="5Scored",
                    run_id="below-floor",
                    signature=None,
                    seed=123,
                    composite=0.10,
                    tool_mean=0.10,
                    memory_mean=0.10,
                    median_ms=100,
                    n=114,
                    details=None,
                    generated_at=_NOW,
                )
            )

        async with session.begin():
            ticket = await issue_ticket(
                session,
                validator_hotkey="5Next",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )

        assert ticket is not None
        assert ticket.agent_id == aid

    async def test_two_scores_below_top_five_bound_defer_behind_other_work(
        self, session: AsyncSession
    ) -> None:
        """An eliminated 2-of-3 submission yields to every other candidate."""
        await _seed_finalized_top_five(session, fifth_place=0.80)
        below_floor = await _seed_two_scores_below_floor(session)
        # Newer than the eliminated submission, so arrival order alone would
        # still hand the eliminated one out first.
        fresh = await _seed_evaluating(
            session, created_at=_NOW + timedelta(minutes=5), name="fresh"
        )

        async with session.begin():
            ticket = await issue_ticket(
                session,
                validator_hotkey="5Next",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )

        assert ticket is not None
        assert ticket.agent_id == fresh
        assert ticket.agent_id != below_floor

    async def test_two_scores_below_top_five_bound_finalize_once_queue_drains(
        self, session: AsyncSession
    ) -> None:
        """Deferred, not withheld: the third score still lands eventually."""
        await _seed_finalized_top_five(session, fifth_place=0.80)
        below_floor = await _seed_two_scores_below_floor(session)

        async with session.begin():
            ticket = await issue_ticket(
                session,
                validator_hotkey="5Next",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )

        assert ticket is not None
        assert ticket.agent_id == below_floor

    async def test_score_floor_does_not_cross_benchmark_eras(
        self, session: AsyncSession
    ) -> None:
        """A v2 fifth place must not eliminate a v4 two-score submission.

        Composites only compare within one benchmark version, so a new era with
        fewer than five ranked agents has no floor at all and nothing in it can
        be pre-emptively eliminated.
        """
        await _seed_finalized_top_five(session, fifth_place=0.80)
        aid = await _seed_evaluating(session, screened=True, dataset_version=None)
        async with session.begin():
            session.add(
                BenchmarkDataset(
                    agent_id=aid,
                    bench_version=_BENCH,
                    seed=42,
                    sha256="cd" * 32,
                    run_size="full",
                )
            )
            # Two v4 scores that would sit below the v2-era floor of 0.80.
            for index, composite in enumerate((0.10, 0.20)):
                validator = f"5V4-{index}"
                session.add(
                    ValidatorTicket(
                        agent_id=aid,
                        validator_hotkey=validator,
                        status=TicketStatus.SCORED,
                        issued_at=_NOW,
                        deadline=_NOW + _TTL,
                        bench_version=_BENCH,
                        attempt_count=1,
                    )
                )
                session.add(
                    Score(
                        agent_id=aid,
                        validator_hotkey=validator,
                        run_id=f"v4-below-v2-floor-{index}",
                        signature=None,
                        seed=42,
                        composite=composite,
                        tool_mean=composite,
                        memory_mean=composite,
                        median_ms=100,
                        n=119,
                        details=None,
                        bench_version=_BENCH,
                        generated_at=_NOW,
                    )
                )

        async with session.begin():
            ticket = await issue_ticket(
                session,
                validator_hotkey="5NextV4",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )

        assert ticket is not None
        assert ticket.agent_id == aid
        assert ticket.bench_version == _BENCH

    async def test_high_variance_two_score_candidate_can_still_reach_top_five(
        self, session: AsyncSession
    ) -> None:
        await _seed_finalized_top_five(session, fifth_place=0.80)
        aid = await _seed_evaluating(session)
        async with session.begin():
            for validator, composite in (("5First", 0.10), ("5Second", 0.90)):
                session.add(
                    Score(
                        agent_id=aid,
                        validator_hotkey=validator,
                        run_id=f"run-{validator}",
                        signature=None,
                        seed=123,
                        composite=composite,
                        tool_mean=composite,
                        memory_mean=composite,
                        median_ms=100,
                        n=114,
                        details=None,
                        generated_at=_NOW,
                    )
                )

        async with session.begin():
            ticket = await issue_ticket(
                session,
                validator_hotkey="5Third",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )

        assert ticket is not None
        assert ticket.agent_id == aid

    async def test_exact_top_five_bound_continues_for_oldest_first_tie_break(
        self, session: AsyncSession
    ) -> None:
        await _seed_finalized_top_five(session, fifth_place=0.80)
        aid = await _seed_evaluating(session)
        async with session.begin():
            for validator, composite in (("5First", 0.20), ("5Second", 0.80)):
                session.add(
                    Score(
                        agent_id=aid,
                        validator_hotkey=validator,
                        run_id=f"run-{validator}",
                        signature=None,
                        seed=123,
                        composite=composite,
                        tool_mean=composite,
                        memory_mean=composite,
                        median_ms=100,
                        n=114,
                        details=None,
                        generated_at=_NOW,
                    )
                )

        async with session.begin():
            ticket = await issue_ticket(
                session,
                validator_hotkey="5Third",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )

        assert ticket is not None
        assert ticket.agent_id == aid

    async def test_no_evaluating_agent_returns_none(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            t = await issue_ticket(
                session,
                validator_hotkey="5V1",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )
        assert t is None

    async def test_caps_at_quorum(self, session: AsyncSession) -> None:
        aid = await _seed_evaluating(session)
        async with session.begin():
            for i in range(SCORING_QUORUM):
                t = await issue_ticket(
                    session,
                    validator_hotkey=f"5V{i}",
                    now=_NOW,
                    ttl=_TTL,
                    bench_version=_BENCH,
                )
                assert t is not None and t.agent_id == aid
            # Quorum reached: a further distinct validator gets no job.
            extra = await issue_ticket(
                session,
                validator_hotkey="5Vx",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )
        assert extra is None

    async def test_same_validator_resumes_its_live_ticket(
        self, session: AsyncSession
    ) -> None:
        await _seed_evaluating(session)
        async with session.begin():
            t1 = await issue_ticket(
                session,
                validator_hotkey="5V1",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )
            t2 = await issue_ticket(
                session,
                validator_hotkey="5V1",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )
        assert t1 is not None
        assert t2 is not None
        assert t2.agent_id == t1.agent_id
        assert t2.deadline == t1.deadline

    async def test_distinct_slots_receive_distinct_agents(
        self, session: AsyncSession
    ) -> None:
        first = await _seed_evaluating(session, name="parallel-a", created_at=_NOW)
        second = await _seed_evaluating(
            session, name="parallel-b", created_at=_NOW + timedelta(seconds=1)
        )
        async with session.begin():
            slot0 = await issue_ticket(
                session,
                validator_hotkey="5Parallel",
                slot_id="slot-0",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )
            slot1 = await issue_ticket(
                session,
                validator_hotkey="5Parallel",
                slot_id="slot-1",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )
        assert slot0 is not None and slot1 is not None
        assert {slot0.agent_id, slot1.agent_id} == {first, second}
        assert slot0.slot_id == "slot-0"
        assert slot1.slot_id == "slot-1"

    async def test_second_slot_never_duplicates_same_agent(
        self, session: AsyncSession
    ) -> None:
        await _seed_evaluating(session, name="only-agent")
        async with session.begin():
            first = await issue_ticket(
                session,
                validator_hotkey="5Parallel",
                slot_id="slot-0",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )
            duplicate = await issue_ticket(
                session,
                validator_hotkey="5Parallel",
                slot_id="slot-1",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )
        assert first is not None
        assert duplicate is None

    async def test_new_benchmark_era_expires_idle_legacy_ticket(
        self, session: AsyncSession
    ) -> None:
        legacy_agent = await _seed_evaluating(
            session,
            name="legacy",
            created_at=_NOW - timedelta(minutes=1),
            screened=False,
            dataset_version=_RETIRED_BENCH,
        )
        current_agent = await _seed_evaluating(session, name="current", created_at=_NOW)
        # The stranded lease belongs to the era the fleet has moved off, which
        # the allocator can no longer mint -- the floor refuses a sub-7 ticket
        # outright -- but which slots are genuinely still holding.
        await _seed_retired_era_lease(
            session,
            agent_id=legacy_agent,
            validator_hotkey="5V1",
            issued_at=_NOW,
            deadline=_NOW + timedelta(hours=2),
            # It announced itself once, so the later silence is evidence the
            # slot is free rather than a run that has not started yet.
            reported_at=_NOW + timedelta(minutes=1),
        )
        async with session.begin():
            await _seed_heartbeat(
                session,
                validator_hotkey="5V1",
                seen_at=_AFTER_REPORTING_GRACE - timedelta(seconds=30),
            )

        async with session.begin():
            current = await issue_ticket(
                session,
                validator_hotkey="5V1",
                now=_AFTER_REPORTING_GRACE,
                ttl=_TTL,
                bench_version=_BENCH,
                artifact_mode="screened_only",
            )

        assert current is not None
        assert current.agent_id == current_agent
        assert current.bench_version == _BENCH
        legacy = await session.get(
            ValidatorTicket, (legacy_agent, _RETIRED_BENCH, "5V1")
        )
        assert legacy is not None
        assert legacy.status == TicketStatus.EXPIRED

    async def test_validator_cannot_hold_live_tickets_for_distinct_agents(
        self, session: AsyncSession
    ) -> None:
        a1 = await _seed_evaluating(session, created_at=_NOW, name="old")
        a2 = await _seed_evaluating(
            session, created_at=_NOW + timedelta(minutes=1), name="new"
        )
        async with session.begin():
            t1 = await issue_ticket(
                session,
                validator_hotkey="5V1",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )
            t2 = await issue_ticket(
                session,
                validator_hotkey="5V1",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )
        assert t1 is not None and t2 is not None
        assert t1.agent_id == a1  # oldest first
        assert t2.agent_id == a1
        assert t2.agent_id != a2

    async def test_weak_first_score_does_not_precede_uncovered_work(
        self, session: AsyncSession
    ) -> None:
        await _seed_finalized_top_ten(session, tenth_place=0.80)
        zero_scores = await _seed_evaluating(
            session, created_at=_NOW - timedelta(hours=1), name="zero-scores"
        )
        one_score = await _seed_evaluating(session, created_at=_NOW, name="one-score")
        async with session.begin():
            session.add_all(
                [
                    ValidatorTicket(
                        agent_id=one_score,
                        validator_hotkey="5Weak",
                        status=TicketStatus.SCORED,
                        issued_at=_NOW,
                        deadline=_NOW + _TTL,
                        bench_version=_BENCH,
                        attempt_count=1,
                    ),
                    Score(
                        agent_id=one_score,
                        validator_hotkey="5Weak",
                        run_id="weak-first-score",
                        signature=None,
                        seed=123,
                        composite=0.05,
                        tool_mean=0.05,
                        memory_mean=0.05,
                        median_ms=100,
                        n=114,
                        details=None,
                        generated_at=_NOW,
                    ),
                ]
            )

        async with session.begin():
            ticket = await issue_ticket(
                session,
                validator_hotkey="5New",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )

        assert ticket is not None
        assert ticket.agent_id == zero_scores

    async def test_first_score_above_top_ten_floor_precedes_uncovered_work(
        self, session: AsyncSession
    ) -> None:
        await _seed_finalized_top_ten(session, tenth_place=0.80)
        zero_scores = await _seed_evaluating(
            session, created_at=_NOW - timedelta(hours=1), name="zero-scores"
        )
        contender = await _seed_evaluating(session, created_at=_NOW, name="contender")
        async with session.begin():
            session.add_all(
                [
                    ValidatorTicket(
                        agent_id=contender,
                        validator_hotkey="5Strong",
                        status=TicketStatus.SCORED,
                        issued_at=_NOW,
                        deadline=_NOW + _TTL,
                        bench_version=_BENCH,
                        attempt_count=1,
                    ),
                    Score(
                        agent_id=contender,
                        validator_hotkey="5Strong",
                        run_id="strong-first-score",
                        signature=None,
                        seed=123,
                        composite=0.90,
                        tool_mean=0.90,
                        memory_mean=0.90,
                        median_ms=100,
                        n=114,
                        details=None,
                        generated_at=_NOW,
                    ),
                ]
            )

        async with session.begin():
            ticket = await issue_ticket(
                session,
                validator_hotkey="5New",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )

        assert ticket is not None
        assert ticket.agent_id == contender
        assert ticket.agent_id != zero_scores

    async def test_completion_lane_prioritizes_highest_provisional_score(
        self, session: AsyncSession
    ) -> None:
        low = await _seed_evaluating(
            session, created_at=_NOW - timedelta(hours=2), name="low"
        )
        high = await _seed_evaluating(
            session, created_at=_NOW - timedelta(hours=1), name="high"
        )
        medium = await _seed_evaluating(session, created_at=_NOW, name="medium")
        async with session.begin():
            for agent_id, composites in (
                (low, (0.20, 0.30)),
                (high, (0.90, 0.80)),
                (medium, (0.60, 0.70)),
            ):
                for index, composite in enumerate(composites):
                    validator = f"5Scored-{agent_id}-{index}"
                    session.add(
                        ValidatorTicket(
                            agent_id=agent_id,
                            validator_hotkey=validator,
                            status=TicketStatus.SCORED,
                            issued_at=_NOW,
                            deadline=_NOW + _TTL,
                            bench_version=_BENCH,
                            attempt_count=1,
                        )
                    )
                    session.add(
                        Score(
                            agent_id=agent_id,
                            validator_hotkey=validator,
                            run_id=f"run-{agent_id}-{index}",
                            signature=None,
                            seed=123,
                            composite=composite,
                            tool_mean=composite,
                            memory_mean=composite,
                            median_ms=100,
                            n=114,
                            details=None,
                            generated_at=_NOW,
                        )
                    )

        async with session.begin():
            ticket = await issue_ticket(
                session,
                validator_hotkey="5Completion",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )

        assert ticket is not None
        assert ticket.agent_id == high

    async def test_one_score_round_prioritizes_highest_provisional_score(
        self, session: AsyncSession
    ) -> None:
        low = await _seed_evaluating(
            session, created_at=_NOW - timedelta(hours=1), name="low"
        )
        high = await _seed_evaluating(session, created_at=_NOW, name="high")
        async with session.begin():
            for agent_id, composite in ((low, 0.40), (high, 0.80)):
                validator = f"5Scored-{agent_id}"
                session.add(
                    ValidatorTicket(
                        agent_id=agent_id,
                        validator_hotkey=validator,
                        status=TicketStatus.SCORED,
                        issued_at=_NOW,
                        deadline=_NOW + _TTL,
                        bench_version=_BENCH,
                        attempt_count=1,
                    )
                )
                session.add(
                    Score(
                        agent_id=agent_id,
                        validator_hotkey=validator,
                        run_id=f"run-{agent_id}",
                        signature=None,
                        seed=123,
                        composite=composite,
                        tool_mean=composite,
                        memory_mean=composite,
                        median_ms=100,
                        n=114,
                        details=None,
                        generated_at=_NOW,
                    )
                )

        async with session.begin():
            ticket = await issue_ticket(
                session,
                validator_hotkey="5Next",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )

        assert ticket is not None
        assert ticket.agent_id == high

    async def test_promising_one_score_jumps_weaker_completion_candidate(
        self, session: AsyncSession
    ) -> None:
        one_score = await _seed_evaluating(
            session, created_at=_NOW, name="promising-one-score"
        )
        two_scores = await _seed_evaluating(
            session,
            created_at=_NOW - timedelta(hours=1),
            name="weaker-two-scores",
        )
        async with session.begin():
            for agent_id, composites in (
                (one_score, (0.90,)),
                (two_scores, (0.60, 0.70)),
            ):
                for index, composite in enumerate(composites):
                    validator = f"5Scored-{agent_id}-{index}"
                    session.add(
                        ValidatorTicket(
                            agent_id=agent_id,
                            validator_hotkey=validator,
                            status=TicketStatus.SCORED,
                            issued_at=_NOW,
                            deadline=_NOW + _TTL,
                            bench_version=_BENCH,
                            attempt_count=1,
                        )
                    )
                    session.add(
                        Score(
                            agent_id=agent_id,
                            validator_hotkey=validator,
                            run_id=f"run-{agent_id}-{index}",
                            signature=None,
                            seed=123,
                            composite=composite,
                            tool_mean=composite,
                            memory_mean=composite,
                            median_ms=100,
                            n=114,
                            details=None,
                            generated_at=_NOW,
                        )
                    )

        async with session.begin():
            ticket = await issue_ticket(
                session,
                validator_hotkey="5Next",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )

        assert ticket is not None
        assert ticket.agent_id == one_score

    async def test_top_provisional_contender_precedes_uncovered_work(
        self, session: AsyncSession
    ) -> None:
        uncovered = await _seed_evaluating(
            session, created_at=_NOW - timedelta(hours=2), name="uncovered"
        )
        contender = await _seed_evaluating(session, created_at=_NOW, name="contender")
        async with session.begin():
            for index, composite in enumerate((0.80, 0.90)):
                validator = f"5Contender-{index}"
                session.add(
                    ValidatorTicket(
                        agent_id=contender,
                        validator_hotkey=validator,
                        status=TicketStatus.SCORED,
                        issued_at=_NOW,
                        deadline=_NOW + _TTL,
                        bench_version=_BENCH,
                        attempt_count=1,
                    )
                )
                session.add(
                    Score(
                        agent_id=contender,
                        validator_hotkey=validator,
                        run_id=f"run-contender-{index}",
                        signature=None,
                        seed=123,
                        composite=composite,
                        tool_mean=composite,
                        memory_mean=composite,
                        median_ms=100,
                        n=114,
                        details=None,
                        generated_at=_NOW,
                    )
                )

        async with session.begin():
            ticket = await issue_ticket(
                session,
                validator_hotkey="5Completion",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )

        assert ticket is not None
        assert ticket.agent_id == contender
        assert ticket.agent_id != uncovered

    async def test_contender_lane_is_bounded(self, session: AsyncSession) -> None:
        uncovered = await _seed_evaluating(
            session, created_at=_NOW - timedelta(hours=2), name="uncovered"
        )
        async with session.begin():
            for rank in range(PROVISIONAL_CONTENDER_LANE_SIZE + 1):
                contender = Agent(
                    agent_id=uuid4(),
                    miner_hotkey=f"5Miner-{rank}",
                    name=f"contender-{rank}",
                    sha256=f"{rank + 1:064x}",
                    status=AgentStatus.EVALUATING,
                    screening_policy_version=SCREENING_POLICY_VERSION,
                    created_at=_NOW + timedelta(minutes=rank),
                )
                session.add(contender)
                for index in range(2):
                    validator = f"5Scored-{rank}-{index}"
                    composite = 1.0 - rank / 100
                    session.add(
                        ValidatorTicket(
                            agent_id=contender.agent_id,
                            validator_hotkey=validator,
                            status=TicketStatus.SCORED,
                            issued_at=_NOW,
                            deadline=_NOW + _TTL,
                            bench_version=_BENCH,
                            attempt_count=1,
                        )
                    )
                    session.add(
                        Score(
                            agent_id=contender.agent_id,
                            validator_hotkey=validator,
                            run_id=f"run-{rank}-{index}",
                            signature=None,
                            seed=123,
                            composite=composite,
                            tool_mean=composite,
                            memory_mean=composite,
                            median_ms=100,
                            n=114,
                            details=None,
                            generated_at=_NOW,
                        )
                    )
                if rank < PROVISIONAL_CONTENDER_LANE_SIZE:
                    session.add(
                        ValidatorTicket(
                            agent_id=contender.agent_id,
                            validator_hotkey="5Completion",
                            status=TicketStatus.EXPIRED,
                            issued_at=_NOW - _TTL,
                            deadline=_NOW,
                            bench_version=_BENCH,
                            attempt_count=MAX_ATTEMPTS_PER_VERSION,
                            retry_after=_NOW + timedelta(days=1),
                        )
                    )

        async with session.begin():
            ticket = await issue_ticket(
                session,
                validator_hotkey="5Completion",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )

        assert ticket is not None
        assert ticket.agent_id == uncovered

    async def test_round_robins_live_assignments_across_zero_score_agents(
        self, session: AsyncSession
    ) -> None:
        agents = [
            await _seed_evaluating(
                session,
                created_at=_NOW + timedelta(minutes=index),
                name=f"agent-{index}",
            )
            for index in range(3)
        ]

        claimed: list[UUID] = []
        async with session.begin():
            for index in range(3):
                ticket = await issue_ticket(
                    session,
                    validator_hotkey=f"5V{index}",
                    now=_NOW,
                    ttl=_TTL,
                    bench_version=_BENCH,
                )
                assert ticket is not None
                claimed.append(ticket.agent_id)

        assert claimed == agents

    async def test_completion_first_finishes_oldest_before_opening_next(
        self, session: AsyncSession
    ) -> None:
        oldest = await _seed_evaluating(session, created_at=_NOW, name="oldest")
        newer = await _seed_evaluating(
            session,
            created_at=_NOW + timedelta(minutes=1),
            name="newer",
        )

        claimed: list[UUID] = []
        async with session.begin():
            for index in range(SCORING_QUORUM):
                ticket = await issue_ticket(
                    session,
                    validator_hotkey=f"5Finish-{index}",
                    now=_NOW,
                    ttl=_TTL,
                    completion_first=True,
                    bench_version=_BENCH,
                )
                assert ticket is not None
                claimed.append(ticket.agent_id)
            next_ticket = await issue_ticket(
                session,
                validator_hotkey="5Finish-next",
                now=_NOW,
                ttl=_TTL,
                completion_first=True,
                bench_version=_BENCH,
            )

        assert claimed == [oldest] * SCORING_QUORUM
        assert next_ticket is not None
        assert next_ticket.agent_id == newer

    async def test_completion_first_does_not_demote_oldest_below_floor(
        self, session: AsyncSession
    ) -> None:
        await _seed_finalized_top_five(session, fifth_place=0.80)
        oldest = await _seed_two_scores_below_floor(session)
        newer = await _seed_evaluating(
            session,
            created_at=_NOW + timedelta(minutes=1),
            name="newer",
        )

        async with session.begin():
            ticket = await issue_ticket(
                session,
                validator_hotkey="5Finish-oldest",
                now=_NOW,
                ttl=_TTL,
                completion_first=True,
                bench_version=_BENCH,
            )

        assert ticket is not None
        assert ticket.agent_id == oldest
        assert ticket.agent_id != newer

    async def test_completion_first_second_slot_advances_past_own_live_head(
        self, session: AsyncSession
    ) -> None:
        """A live lease on the head must not idle the same validator's siblings.

        One ticket per (agent, version, validator) is the composite primary key,
        so the head's remaining quorum slots belong to other validators no
        matter what this slot does. Parking the sibling behind the head bought
        the head nothing and cost the fleet a slot for the whole lease.
        """
        oldest = await _seed_evaluating(session, created_at=_NOW, name="oldest")
        newer = await _seed_evaluating(
            session,
            created_at=_NOW + timedelta(minutes=1),
            name="newer",
        )

        async with session.begin():
            slot0 = await issue_ticket(
                session,
                validator_hotkey="5ParallelFinish",
                slot_id="slot-0",
                now=_NOW,
                ttl=_TTL,
                completion_first=True,
                bench_version=_BENCH,
            )
            slot1 = await issue_ticket(
                session,
                validator_hotkey="5ParallelFinish",
                slot_id="slot-1",
                now=_NOW,
                ttl=_TTL,
                completion_first=True,
                bench_version=_BENCH,
            )

        assert slot0 is not None
        assert slot0.agent_id == oldest
        assert slot1 is not None
        # Strictly the next FIFO candidate, never a second lease on the head.
        assert slot1.agent_id == newer
        assert slot1.slot_id == "slot-1"

    async def test_completion_first_second_slot_stops_when_fifo_is_exhausted(
        self, session: AsyncSession
    ) -> None:
        """Advancing past the head is not permission to invent work."""
        oldest = await _seed_evaluating(session, created_at=_NOW, name="oldest")

        async with session.begin():
            slot0 = await issue_ticket(
                session,
                validator_hotkey="5OnlyOneAgent",
                slot_id="slot-0",
                now=_NOW,
                ttl=_TTL,
                completion_first=True,
                bench_version=_BENCH,
            )
            slot1 = await issue_ticket(
                session,
                validator_hotkey="5OnlyOneAgent",
                slot_id="slot-1",
                now=_NOW,
                ttl=_TTL,
                completion_first=True,
                bench_version=_BENCH,
            )

        assert slot0 is not None
        assert slot0.agent_id == oldest
        assert slot1 is None

    async def test_completion_first_advances_past_head_validator_already_scored(
        self, session: AsyncSession
    ) -> None:
        oldest = await _seed_evaluating(session, created_at=_NOW, name="oldest")
        newer = await _seed_evaluating(
            session,
            created_at=_NOW + timedelta(minutes=1),
            name="newer",
        )
        async with session.begin():
            session.add(
                ValidatorTicket(
                    agent_id=oldest,
                    validator_hotkey="5AlreadyScored",
                    status=TicketStatus.SCORED,
                    issued_at=_NOW,
                    deadline=_NOW + _TTL,
                    bench_version=_BENCH,
                    attempt_count=1,
                )
            )

        async with session.begin():
            ticket = await issue_ticket(
                session,
                validator_hotkey="5AlreadyScored",
                now=_NOW,
                ttl=_TTL,
                completion_first=True,
                bench_version=_BENCH,
            )

        assert ticket is not None
        assert ticket.agent_id == newer

    async def test_completion_first_advances_past_exhausted_head_retry(
        self, session: AsyncSession
    ) -> None:
        oldest = await _seed_evaluating(session, created_at=_NOW, name="oldest")
        newer = await _seed_evaluating(
            session,
            created_at=_NOW + timedelta(minutes=1),
            name="newer",
        )
        async with session.begin():
            session.add(
                ValidatorTicket(
                    agent_id=oldest,
                    validator_hotkey="5Exhausted",
                    status=TicketStatus.EXPIRED,
                    issued_at=_NOW - timedelta(hours=2),
                    deadline=_NOW - timedelta(hours=1),
                    bench_version=_BENCH,
                    attempt_count=MAX_ATTEMPTS_PER_VERSION,
                    retry_after=None,
                )
            )

        async with session.begin():
            ticket = await issue_ticket(
                session,
                validator_hotkey="5Exhausted",
                now=_NOW,
                ttl=_TTL,
                completion_first=True,
                bench_version=_BENCH,
            )

        assert ticket is not None
        assert ticket.agent_id == newer

    async def test_completion_first_mixed_fleet_keeps_making_progress(
        self, session: AsyncSession
    ) -> None:
        """One unclaimable FIFO head must not idle the whole validator fleet."""
        oldest = await _seed_evaluating(session, created_at=_NOW, name="oldest")
        newer = await _seed_evaluating(
            session,
            created_at=_NOW + timedelta(minutes=1),
            name="newer",
        )
        async with session.begin():
            session.add_all(
                [
                    ValidatorTicket(
                        agent_id=oldest,
                        validator_hotkey="5AlreadyScored",
                        status=TicketStatus.SCORED,
                        issued_at=_NOW,
                        deadline=_NOW + _TTL,
                        bench_version=_BENCH,
                        attempt_count=1,
                    ),
                    ValidatorTicket(
                        agent_id=oldest,
                        validator_hotkey="5CoolingDown",
                        status=TicketStatus.EXPIRED,
                        issued_at=_NOW - timedelta(hours=2),
                        deadline=_NOW - timedelta(hours=1),
                        bench_version=_BENCH,
                        attempt_count=1,
                        retry_after=_NOW + timedelta(hours=1),
                    ),
                    ValidatorTicket(
                        agent_id=oldest,
                        validator_hotkey="5Exhausted",
                        status=TicketStatus.EXPIRED,
                        issued_at=_NOW - timedelta(hours=2),
                        deadline=_NOW - timedelta(hours=1),
                        bench_version=_BENCH,
                        attempt_count=MAX_ATTEMPTS_PER_VERSION,
                        retry_after=None,
                    ),
                ]
            )

        claims: dict[str, UUID] = {}
        async with session.begin():
            for validator in (
                "5AlreadyScored",
                "5CoolingDown",
                "5Exhausted",
                "5Eligible",
            ):
                ticket = await issue_ticket(
                    session,
                    validator_hotkey=validator,
                    now=_NOW,
                    ttl=_TTL,
                    completion_first=True,
                    bench_version=_BENCH,
                )
                assert ticket is not None
                claims[validator] = ticket.agent_id

        assert claims == {
            "5AlreadyScored": newer,
            "5CoolingDown": newer,
            "5Exhausted": newer,
            "5Eligible": oldest,
        }

    async def test_completion_first_advances_past_head_at_full_quorum(
        self, session: AsyncSession
    ) -> None:
        """A saturated FIFO head does not hide later claimable work."""
        oldest = await _seed_evaluating(session, created_at=_NOW, name="oldest")
        newer = await _seed_evaluating(
            session,
            created_at=_NOW + timedelta(minutes=1),
            name="newer",
        )
        async with session.begin():
            for index in range(SCORING_QUORUM):
                session.add(
                    ValidatorTicket(
                        agent_id=oldest,
                        validator_hotkey=f"5Head-{index}",
                        status=TicketStatus.ISSUED,
                        issued_at=_NOW,
                        deadline=_NOW + _TTL,
                        bench_version=_BENCH,
                        attempt_count=1,
                    )
                )

        async with session.begin():
            ticket = await issue_ticket(
                session,
                validator_hotkey="5Next",
                now=_NOW,
                ttl=_TTL,
                completion_first=True,
                bench_version=_BENCH,
            )

        assert ticket is not None
        assert ticket.agent_id == newer

    async def test_uncovered_work_follows_noncontender_with_live_assignment(
        self, session: AsyncSession
    ) -> None:
        one_score = await _seed_evaluating(session, name="one-score")
        zero_scores = await _seed_evaluating(
            session,
            created_at=_NOW + timedelta(minutes=1),
            name="zero-scores",
        )
        async with session.begin():
            session.add(
                ValidatorTicket(
                    agent_id=one_score,
                    validator_hotkey="5Scored",
                    status=TicketStatus.SCORED,
                    issued_at=_NOW,
                    deadline=_NOW + _TTL,
                    bench_version=_BENCH,
                    attempt_count=1,
                )
            )

        async with session.begin():
            first = await issue_ticket(
                session,
                validator_hotkey="5NewA",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )
        async with session.begin():
            second = await issue_ticket(
                session,
                validator_hotkey="5NewB",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )

        assert first is not None and first.agent_id == one_score
        assert second is not None and second.agent_id == zero_scores
        assert first.agent_id != zero_scores
        assert second.agent_id != one_score


class TestExpiry:
    async def test_deadline_instant_is_expired(self, session: AsyncSession) -> None:
        await _seed_evaluating(session)
        async with session.begin():
            await issue_ticket(
                session,
                validator_hotkey="5V1",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )
        deadline = _NOW + _TTL
        async with session.begin():
            assert await expire_overdue_tickets(session, now=deadline) == 1

    async def test_expired_ticket_frees_slot(self, session: AsyncSession) -> None:
        aid = await _seed_evaluating(session)
        async with session.begin():
            for i in range(SCORING_QUORUM):
                await issue_ticket(
                    session,
                    validator_hotkey=f"5V{i}",
                    now=_NOW,
                    ttl=_TTL,
                    bench_version=_BENCH,
                )
        # After the deadline the three lapse, so a new validator can seat.
        async with session.begin():
            t = await issue_ticket(
                session,
                validator_hotkey="5Vnew",
                now=_LATER,
                ttl=_TTL,
                bench_version=_BENCH,
            )
        assert t is not None and t.agent_id == aid

    async def test_expired_ticket_cools_down_and_next_agent_moves_ahead(
        self, session: AsyncSession
    ) -> None:
        slow = await _seed_evaluating(session, name="slow")
        next_agent = await _seed_evaluating(
            session,
            created_at=_NOW + timedelta(minutes=1),
            name="next",
        )
        async with session.begin():
            first = await issue_ticket(
                session,
                validator_hotkey="5V1",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )
        assert first is not None and first.agent_id == slow

        async with session.begin():
            claimed = await issue_ticket(
                session,
                validator_hotkey="5V1",
                now=_LATER,
                ttl=_TTL,
                bench_version=_BENCH,
            )

        assert claimed is not None
        assert claimed.agent_id == next_agent

    async def test_expired_ticket_does_not_retry_without_a_grant(
        self, session: AsyncSession
    ) -> None:
        aid = await _seed_evaluating(session)
        async with session.begin():
            await issue_ticket(
                session,
                validator_hotkey="5V1",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )
        async with session.begin():
            retried = await issue_ticket(
                session,
                validator_hotkey="5V1",
                now=_AFTER_COOLDOWN,
                ttl=_TTL,
                bench_version=_BENCH,
            )

        assert retried is None
        async with session.begin():
            ticket = await session.get(ValidatorTicket, (aid, _BENCH, "5V1"))
        assert ticket is not None
        assert ticket.status == TicketStatus.EXPIRED
        assert ticket.attempt_count == 1

    async def test_never_attempted_agent_precedes_eligible_retry(
        self, session: AsyncSession
    ) -> None:
        slow = await _seed_evaluating(session, name="slow")
        untouched = await _seed_evaluating(
            session,
            created_at=_NOW + timedelta(minutes=1),
            name="untouched",
        )
        async with session.begin():
            first = await issue_ticket(
                session,
                validator_hotkey="5V1",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )
        assert first is not None and first.agent_id == slow

        async with session.begin():
            claimed = await issue_ticket(
                session,
                validator_hotkey="5V1",
                now=_AFTER_COOLDOWN,
                ttl=_TTL,
                bench_version=_BENCH,
            )

        assert claimed is not None
        assert claimed.agent_id == untouched

    async def test_first_expiry_exhausts_same_version_retry_budget(
        self, session: AsyncSession
    ) -> None:
        aid = await _seed_evaluating(session)
        async with session.begin():
            await issue_ticket(
                session,
                validator_hotkey="5V1",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )
        async with session.begin():
            retry = await issue_ticket(
                session,
                validator_hotkey="5V1",
                now=_AFTER_COOLDOWN,
                ttl=_TTL,
                bench_version=_BENCH,
            )

        assert retry is None
        async with session.begin():
            ticket = await session.get(ValidatorTicket, (aid, _BENCH, "5V1"))
        assert ticket is not None
        assert ticket.status == TicketStatus.EXPIRED
        assert ticket.attempt_count == 1

    async def test_benchmark_version_change_resets_retry_budget(
        self, session: AsyncSession
    ) -> None:
        """A spent budget on the era before does not follow the submission.

        The exhausted lease has to belong to a *different* era for the reset to
        mean anything, and the only era below the live one is a retired one, so
        the history is written through the floor rather than issued: the
        allocator is refused a sub-7 ticket now, but the rows it wrote before
        the floor are still on the slot and still carry a spent budget.
        """
        aid = await _seed_evaluating(session)
        await _seed_retired_era_lease(
            session,
            agent_id=aid,
            validator_hotkey="5V1",
            issued_at=_NOW,
            deadline=_NOW + _TTL,
            status=TicketStatus.EXPIRED,
            attempt_count=MAX_ATTEMPTS_PER_VERSION,
            retry_after=_NOW + timedelta(days=1),
        )
        async with session.begin():
            reset = await issue_ticket(
                session,
                validator_hotkey="5V1",
                now=_LATER,
                ttl=_TTL,
                bench_version=_BENCH,
            )

        assert reset is not None
        assert reset.bench_version == _BENCH
        assert reset.attempt_count == 1
        assert reset.retry_after is None

    async def test_image_requirement_has_no_source_fallback_left(
        self, session: AsyncSession
    ) -> None:
        """A pinned dataset is not enough: no leasable era waives the image.

        This was ``test_v3_requires_image_while_v2_keeps_source_fallback`` and
        asserted the split -- v3 refusing a source-only submission while v2
        still leased it, which is what let the fleet roll forward one validator
        at a time. v2 was the only contract with ``requires_screened_image``
        false and the bench-version floor has retired it, so the second half is
        not merely untested now, it is unreachable: there is no version at or
        above the floor for a source-only submission to fall back to. Inverted
        rather than dropped so that reopening a source lane has to come back
        through this test.
        """
        aid = await _seed_evaluating(session, screened=False)
        async with session.begin():
            assert (
                await issue_ticket(
                    session,
                    validator_hotkey="5NoImage",
                    now=_NOW,
                    ttl=_TTL,
                    bench_version=_BENCH,
                )
                is None
            )
            assert (
                await issue_ticket(
                    session,
                    validator_hotkey="5Fallback",
                    now=_NOW,
                    ttl=_TTL,
                    artifact_mode="prefer_screened",
                    bench_version=_BENCH,
                )
                is None
            )
        # The submission is otherwise perfectly queueable: give it the image and
        # the same poll leases it, so the refusal above is the artifact contract
        # and not some other filter.
        async with session.begin():
            agent = await session.get(Agent, aid)
            assert agent is not None
            agent.screened_image_sha256 = "12" * 32
            agent.screened_image_size_bytes = 123
            agent.screened_image_id = "sha256:" + "34" * 32
            agent.screened_image_ref = f"ditto-screen/{aid}:latest"
            agent.screened_image_upload_id = uuid4()
            agent.screened_image_verified_at = _NOW
        async with session.begin():
            leased = await issue_ticket(
                session,
                validator_hotkey="5NoImage",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )
        assert leased is not None
        assert leased.agent_id == aid

    async def test_prior_scored_version_does_not_block_new_version(
        self, session: AsyncSession
    ) -> None:
        """Having scored a submission on the era before is not "already mine".

        The prior lease is written as retired-era history rather than issued:
        the floor refuses to mint a sub-7 ticket, but a validator that scored
        this submission on the era the fleet just left still has that row, and
        it must not cost the validator the new era's slot.
        """
        aid = await _seed_evaluating(session)
        await _seed_retired_era_lease(
            session,
            agent_id=aid,
            validator_hotkey="5V1",
            issued_at=_NOW,
            deadline=_NOW + _TTL,
            status=TicketStatus.SCORED,
        )

        async with session.begin():
            current = await issue_ticket(
                session,
                validator_hotkey="5V1",
                now=_LATER,
                ttl=_TTL,
                bench_version=_BENCH,
            )

        assert current is not None
        assert current.agent_id == aid
        assert current.bench_version == _BENCH
        assert current.attempt_count == 1

    async def test_expire_overdue_returns_count(self, session: AsyncSession) -> None:
        await _seed_evaluating(session)
        async with session.begin():
            await issue_ticket(
                session,
                validator_hotkey="5V1",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )
        async with session.begin():
            n = await expire_overdue_tickets(session, now=_LATER)
        assert n == 1


class TestIssueConfirmationTicket:
    @pytest.mark.parametrize(
        "first_reported_at",
        [None, _NOW + timedelta(minutes=1)],
        ids=["before-progress", "after-progress"],
    )
    async def test_refuses_duplicate_live_confirmation_claim(
        self, session: AsyncSession, first_reported_at: datetime | None
    ) -> None:
        """A second task must not rotate the grant under an active scorer."""
        aid = await _seed_scored(session)
        original_deadline = _NOW + _TTL
        async with session.begin():
            session.add(
                ValidatorTicket(
                    agent_id=aid,
                    validator_hotkey="5V1",
                    slot_id="slot-0",
                    status=TicketStatus.ISSUED,
                    purpose=TicketPurpose.CONTINUAL_RETEST,
                    purpose_revision=4,
                    issued_at=_NOW,
                    deadline=original_deadline,
                    bench_version=_BENCH,
                    seed=123,
                    dataset_sha256="ab" * 32,
                    attempt_count=7,
                    infra_retry_grants=2,
                    first_reported_at=first_reported_at,
                )
            )

        async with session.begin():
            ticket = await issue_confirmation_ticket(
                session,
                agent_id=aid,
                validator_hotkey="5V1",
                now=_NOW + timedelta(minutes=10),
                ttl=_TTL,
                bench_version=_BENCH,
                seed=123,
                dataset_sha256="ab" * 32,
                slot_id="slot-1",
            )

        assert ticket is None
        stored = await session.get(ValidatorTicket, (aid, _BENCH, "5V1"))
        assert stored is not None
        assert stored.deadline == original_deadline
        assert stored.slot_id == "slot-0"
        assert stored.attempt_count == 7
        assert stored.infra_retry_grants == 2
        assert stored.first_reported_at == first_reported_at

    async def test_reissues_scored_validator_slot_with_fresh_lease(
        self, session: AsyncSession
    ) -> None:
        aid = await _seed_scored(session)
        async with session.begin():
            session.add(
                ValidatorTicket(
                    agent_id=aid,
                    validator_hotkey="5V1",
                    status=TicketStatus.SCORED,
                    issued_at=_NOW - _TTL,
                    deadline=_NOW,
                    bench_version=_BENCH,
                    attempt_count=1,
                    manual_retry_grants=0,
                    retry_after=None,
                )
            )
        async with session.begin():
            ticket = await issue_confirmation_ticket(
                session,
                agent_id=aid,
                validator_hotkey="5V1",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )

        assert ticket is not None
        assert ticket.status == TicketStatus.ISSUED
        assert ticket.purpose == TicketPurpose.CONTINUAL_RETEST
        assert ticket.purpose_revision == 2
        assert ticket.deadline == _NOW + _TTL
        assert ticket.attempt_count == 2

    async def test_does_not_resume_a_canonical_live_lease_as_confirmation(
        self, session: AsyncSession
    ) -> None:
        aid = await _seed_scored(session)
        async with session.begin():
            session.add(
                ValidatorTicket(
                    agent_id=aid,
                    validator_hotkey="5V1",
                    status=TicketStatus.ISSUED,
                    purpose=TicketPurpose.CANONICAL_QUORUM,
                    issued_at=_NOW,
                    deadline=_NOW + _TTL,
                    bench_version=_BENCH,
                    attempt_count=1,
                )
            )
        async with session.begin():
            ticket = await issue_confirmation_ticket(
                session,
                agent_id=aid,
                validator_hotkey="5V1",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )

        assert ticket is None

    async def test_does_not_resume_old_version_continual_lease(
        self, session: AsyncSession
    ) -> None:
        aid = await _seed_scored(session)
        # A continual-retest lease left behind on the era the fleet moved off.
        # It has to be written as history: the floor refuses to mint a sub-7
        # ticket, and a lease on the era being asked for would be resumable,
        # which is the opposite of the case under test.
        await _seed_retired_era_lease(
            session,
            agent_id=aid,
            validator_hotkey="5V1",
            issued_at=_NOW,
            deadline=_NOW + _TTL,
            purpose=TicketPurpose.CONTINUAL_RETEST,
        )
        async with session.begin():
            ticket = await issue_confirmation_ticket(
                session,
                agent_id=aid,
                validator_hotkey="5V1",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )

        assert ticket is None

    async def test_does_not_take_over_expired_operator_replacement(
        self, session: AsyncSession
    ) -> None:
        aid = await _seed_scored(session)
        async with session.begin():
            session.add(
                ValidatorTicket(
                    agent_id=aid,
                    validator_hotkey="5V1",
                    status=TicketStatus.ISSUED,
                    purpose=TicketPurpose.CANONICAL_QUORUM,
                    issued_at=_NOW - _TTL,
                    deadline=_NOW,
                    bench_version=_BENCH,
                    attempt_count=2,
                )
            )
            await append_audit_entry(
                session,
                agent_id=aid,
                validator_hotkey="5V1",
                event=EVENT_SCORE_RETEST_REQUESTED,
                payload={"bench_version": _BENCH, "run_id": "accepted-5V1"},
                recorded_at=_NOW - _TTL,
            )
        async with session.begin():
            ticket = await issue_confirmation_ticket(
                session,
                agent_id=aid,
                validator_hotkey="5V1",
                now=_NOW + timedelta(seconds=1),
                ttl=_TTL,
                bench_version=_BENCH,
            )

        assert ticket is None
        stored = await session.get(ValidatorTicket, (aid, _BENCH, "5V1"))
        assert stored is not None
        assert stored.status == TicketStatus.EXPIRED
        assert stored.purpose == TicketPurpose.CANONICAL_QUORUM

    async def test_does_not_interrupt_another_live_assignment(
        self, session: AsyncSession
    ) -> None:
        target = await _seed_scored(session)
        other = await _seed_evaluating(session, name="other")
        async with session.begin():
            session.add(
                ValidatorTicket(
                    agent_id=other,
                    validator_hotkey="5V1",
                    status=TicketStatus.ISSUED,
                    issued_at=_NOW,
                    deadline=_NOW + _TTL,
                    bench_version=_BENCH,
                    attempt_count=1,
                    manual_retry_grants=0,
                    retry_after=None,
                )
            )
        async with session.begin():
            ticket = await issue_confirmation_ticket(
                session,
                agent_id=target,
                validator_hotkey="5V1",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )

        assert ticket is None


class TestTicketLifecycle:
    async def test_get_open_ticket_live(self, session: AsyncSession) -> None:
        aid = await _seed_evaluating(session)
        async with session.begin():
            await issue_ticket(
                session,
                validator_hotkey="5V1",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )
        async with session.begin():
            t = await get_open_ticket(
                session,
                agent_id=aid,
                validator_hotkey="5V1",
                now=_NOW,
                deadline=_NOW + _TTL,
                bench_version=_BENCH,
            )
        assert t is not None

    async def test_get_open_ticket_expired_is_none(self, session: AsyncSession) -> None:
        aid = await _seed_evaluating(session)
        async with session.begin():
            await issue_ticket(
                session,
                validator_hotkey="5V1",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )
        async with session.begin():
            t = await get_open_ticket(
                session,
                agent_id=aid,
                validator_hotkey="5V1",
                now=_LATER,
                deadline=_NOW + _TTL,
                bench_version=_BENCH,
            )
        assert t is None

    async def test_get_open_ticket_at_exact_deadline_is_none(
        self, session: AsyncSession
    ) -> None:
        aid = await _seed_evaluating(session)
        deadline = _NOW + _TTL
        async with session.begin():
            await issue_ticket(
                session,
                validator_hotkey="5V1",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )
        async with session.begin():
            ticket = await get_open_ticket(
                session,
                agent_id=aid,
                validator_hotkey="5V1",
                now=deadline,
                deadline=deadline,
                bench_version=_BENCH,
            )
        assert ticket is None

    async def test_get_open_ticket_absent_is_none(self, session: AsyncSession) -> None:
        aid = await _seed_evaluating(session)
        async with session.begin():
            t = await get_open_ticket(
                session,
                agent_id=aid,
                validator_hotkey="5Vx",
                now=_NOW,
                deadline=_NOW + _TTL,
                bench_version=_BENCH,
            )
        assert t is None

    async def test_mark_scored_makes_ticket_not_open(
        self, session: AsyncSession
    ) -> None:
        aid = await _seed_evaluating(session)
        async with session.begin():
            await issue_ticket(
                session,
                validator_hotkey="5V1",
                now=_NOW,
                ttl=_TTL,
                bench_version=_BENCH,
            )
        async with session.begin():
            await mark_ticket_scored(
                session,
                agent_id=aid,
                validator_hotkey="5V1",
                bench_version=_BENCH,
            )
        async with session.begin():
            t = await get_open_ticket(
                session,
                agent_id=aid,
                validator_hotkey="5V1",
                now=_NOW,
                deadline=_NOW + _TTL,
                bench_version=_BENCH,
            )
        assert t is None  # spent, no longer open

    async def test_open_ticket_selects_explicit_version_with_dual_rows(
        self, session: AsyncSession
    ) -> None:
        """Two rows for one validator, and the asked-for era is the one served.

        The spent row is retired-era history rather than a second current-era
        ticket: the two rows have to differ in ``bench_version`` for the lookup
        to be under test at all, and the only era below the live one is one the
        floor will not let the allocator write.
        """
        aid = await _seed_evaluating(session)
        await _seed_retired_era_lease(
            session,
            agent_id=aid,
            validator_hotkey="5V1",
            issued_at=_NOW,
            deadline=_NOW + _TTL,
            status=TicketStatus.SCORED,
        )
        async with session.begin():
            session.add(
                ValidatorTicket(
                    agent_id=aid,
                    bench_version=_BENCH,
                    validator_hotkey="5V1",
                    status=TicketStatus.ISSUED,
                    issued_at=_NOW,
                    deadline=_NOW + _TTL,
                )
            )
        async with session.begin():
            ticket = await get_open_ticket(
                session,
                agent_id=aid,
                validator_hotkey="5V1",
                now=_NOW,
                deadline=_NOW + _TTL,
                bench_version=_BENCH,
            )
        assert ticket is not None
        assert ticket.bench_version == _BENCH

    async def test_open_ticket_selects_signed_lease_across_versions(
        self, session: AsyncSession
    ) -> None:
        aid = await _seed_evaluating(session)
        live_deadline = _NOW + _TTL + timedelta(minutes=1)
        await _seed_retired_era_lease(
            session,
            agent_id=aid,
            validator_hotkey="5V1",
            issued_at=_NOW,
            deadline=_NOW + _TTL,
            status=TicketStatus.SCORED,
        )
        async with session.begin():
            session.add(
                ValidatorTicket(
                    agent_id=aid,
                    bench_version=_BENCH,
                    validator_hotkey="5V1",
                    status=TicketStatus.ISSUED,
                    issued_at=_NOW,
                    deadline=live_deadline,
                )
            )
        async with session.begin():
            ticket = await get_open_ticket(
                session,
                agent_id=aid,
                validator_hotkey="5V1",
                now=_NOW,
                deadline=live_deadline,
                bench_version=None,
            )
        assert ticket is not None
        assert ticket.bench_version == _BENCH
