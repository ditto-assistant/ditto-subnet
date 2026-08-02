"""Near-identical submissions share a budget; distinct miners do not.

The end-to-end guarantee, exercised through the two callers that must never
disagree: :func:`~ditto.db.queries.tickets.issue_ticket`, which actually leases,
and :func:`~ditto.db.queries.queue_order.preview_queue_order`, which tells a
miner why they are waiting. The reason lives in one function precisely so a
future relaxation cannot relax it for only one of them, and the last test here
is what would notice if someone added a second copy.

The similarity fixtures are the same real production sketches the grouping tests
use: four submissions from one family under four different payment coldkeys, and
four independent miners. The owner gate sees the family as four unrelated owners
and lets all four run; this gate does not.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.api_models.ticket_status import TicketPurpose, TicketStatus
from ditto.db.models import (
    Agent,
    BenchmarkDataset,
    EvaluationPayment,
    ValidatorTicket,
)
from ditto.db.queries.benchmark_rollout import MIN_SCOREABLE_BENCH_VERSION
from ditto.db.queries.queue_order import (
    owner_capacity_gate,
    preview_queue_order,
)
from ditto.db.queries.similarity_grouping import SimilarityBudgetPolicy
from ditto.db.queries.tickets import issue_ticket

_NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=UTC)
_TTL = timedelta(minutes=90)
_BENCH = MIN_SCOREABLE_BENCH_VERSION

# Every quorum slot on the held submission, so a polling validator cannot join
# it and the only question left is the similarity one.
_HOLDERS = ("5ValidatorHolderA", "5ValidatorHolderB", "5ValidatorHolderC")

_FIXTURES = json.loads(
    (
        Path(__file__).parent / "similarity_fixtures" / "production_sketches.json"
    ).read_text()
)


def _fingerprint(name: str) -> dict:
    return _FIXTURES[name]["fingerprint"]


async def _seed(
    session: AsyncSession,
    *,
    fingerprint: dict | None,
    coldkey: str,
    created_at: datetime,
    lease_validators: tuple[str, ...] = (),
) -> UUID:
    """One waiting submission, optionally already leased.

    ``lease_validators`` names every validator holding a live ticket on it.
    Tests that need a submission to be *unavailable* to the fleet name all
    three quorum slots, so a poller cannot simply join it -- filling a
    submission's own quorum is not similarity contention and must not be
    confused with it.
    """
    agent_id = uuid4()
    hotkey = f"5Hotkey{agent_id.hex[:10]}"
    async with session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=hotkey,
                name=f"agent-{agent_id.hex[:6]}",
                sha256="ab" * 32,
                status=AgentStatus.EVALUATING,
                screening_policy_version=SCREENING_POLICY_VERSION,
                content_fingerprint=fingerprint,
                created_at=created_at,
                screened_image_sha256="12" * 32,
                screened_image_size_bytes=123,
                screened_image_id="sha256:" + "34" * 32,
                screened_image_ref=f"ditto-screen/{agent_id}:latest",
                screened_image_upload_id=uuid4(),
                screened_image_verified_at=created_at,
            )
        )
        await session.flush()
        session.add(
            BenchmarkDataset(
                agent_id=agent_id,
                bench_version=_BENCH,
                seed=1,
                sha256="cd" * 32,
                run_size="full",
            )
        )
        session.add(
            EvaluationPayment(
                block_hash=f"0x{uuid4().hex}",
                extrinsic_index=0,
                agent_id=agent_id,
                miner_hotkey=hotkey,
                miner_coldkey=coldkey,
                amount_rao=1,
                tao_usd_rate=Decimal("1"),
                dest_address="5Destination",
                timestamp=created_at,
            )
        )
        for holder in lease_validators:
            session.add(
                ValidatorTicket(
                    agent_id=agent_id,
                    bench_version=_BENCH,
                    validator_hotkey=holder,
                    slot_id="slot-0",
                    status=TicketStatus.ISSUED,
                    purpose=TicketPurpose.CANONICAL_QUORUM,
                    purpose_revision=1,
                    issued_at=created_at,
                    deadline=_NOW + _TTL,
                    attempt_count=1,
                )
            )
    return agent_id


async def _lease(
    session: AsyncSession,
    *,
    validator: str,
    policy: SimilarityBudgetPolicy | None,
    limit: int = 1,
) -> UUID | None:
    await session.rollback()
    async with session.begin():
        ticket = await issue_ticket(
            session,
            validator_hotkey=validator,
            now=_NOW,
            ttl=_TTL,
            bench_version=_BENCH,
            similarity_policy=policy,
            similarity_concurrent_submission_limit=limit,
        )
    return ticket.agent_id if ticket is not None else None


async def _distinct_submissions_leased(
    session: AsyncSession,
    *,
    validators: int,
    policy: SimilarityBudgetPolicy | None,
) -> set[UUID]:
    """How many distinct submissions the fleet gets running.

    Distinct *submissions*, not tickets, because filling one submission's own
    quorum is exactly what the fleet is for -- three validators scoring the same
    row is a completed evaluation, not a monopoly. What the similarity rail
    bounds is how many near-identical submissions run at once, so that is what
    the assertions count.
    """
    leased = {
        await _lease(session, validator=f"5Validator{slot}", policy=policy)
        for slot in range(validators)
    }
    return {agent_id for agent_id in leased if agent_id is not None}


# ---------------------------------------------------------------------------
# The gate, as a pure decision
# ---------------------------------------------------------------------------


def test_a_near_twin_holding_a_lease_serializes_the_candidate() -> None:
    candidate, twin = uuid4(), uuid4()

    assert (
        owner_capacity_gate(
            agent_id=candidate,
            selected_agent_id=None,
            live_lease_agent_ids=(),
            similar_lease_agent_ids=[twin],
        )
        == "similarity_serialized"
    )


def test_no_twins_means_no_similarity_refusal() -> None:
    assert (
        owner_capacity_gate(
            agent_id=uuid4(),
            selected_agent_id=None,
            live_lease_agent_ids=(),
            similar_lease_agent_ids=(),
        )
        is None
    )


def test_the_candidates_own_lease_is_not_a_twin() -> None:
    """A row already leased by another validator must reach its own quorum."""
    candidate = uuid4()

    assert (
        owner_capacity_gate(
            agent_id=candidate,
            selected_agent_id=None,
            live_lease_agent_ids=(),
            similar_lease_agent_ids=[candidate],
        )
        is None
    )


def test_the_owner_reason_wins_when_both_rails_apply() -> None:
    """The pre-existing, narrower reason is the one an operator sees."""
    candidate, sibling = uuid4(), uuid4()

    assert (
        owner_capacity_gate(
            agent_id=candidate,
            selected_agent_id=None,
            live_lease_agent_ids=[sibling],
            similar_lease_agent_ids=[sibling],
        )
        == "owner_serialized"
    )


def test_the_similarity_limit_is_operator_configurable() -> None:
    candidate, twin = uuid4(), uuid4()
    kwargs = {
        "agent_id": candidate,
        "selected_agent_id": None,
        "live_lease_agent_ids": (),
        "similar_lease_agent_ids": [twin],
    }

    assert owner_capacity_gate(**kwargs) == "similarity_serialized"  # type: ignore[arg-type]
    assert (
        owner_capacity_gate(
            **kwargs,  # type: ignore[arg-type]
            similarity_concurrent_submission_limit=2,
        )
        is None
    )


def test_the_similarity_rail_applies_on_the_last_resort_pass_too() -> None:
    """Unlike the owner ceiling, which relaxes when nothing else is leasable.

    That asymmetry is the point: one family filling an otherwise-idle fleet is
    exactly the monopoly this rail exists to break.
    """
    candidate, twin = uuid4(), uuid4()

    assert (
        owner_capacity_gate(
            agent_id=candidate,
            selected_agent_id=None,
            live_lease_agent_ids=(),
            similar_lease_agent_ids=[twin],
            last_resort=True,
        )
        == "similarity_serialized"
    )


def test_a_started_generation_is_never_refused_on_similarity() -> None:
    """Refusing a mid-quorum submission would strand the fleet work spent on it."""
    candidate, twin = uuid4(), uuid4()

    assert (
        owner_capacity_gate(
            agent_id=candidate,
            selected_agent_id=candidate,
            live_lease_agent_ids=(),
            similar_lease_agent_ids=[twin],
        )
        is None
    )


def test_supplying_no_similarity_input_leaves_the_owner_rails_untouched() -> None:
    """The default is off, so nothing changes until an operator turns it on."""
    candidate, sibling = uuid4(), uuid4()

    assert (
        owner_capacity_gate(
            agent_id=candidate,
            selected_agent_id=None,
            live_lease_agent_ids=[sibling],
        )
        == "owner_serialized"
    )
    assert (
        owner_capacity_gate(
            agent_id=candidate,
            selected_agent_id=None,
            live_lease_agent_ids=(),
        )
        is None
    )


# ---------------------------------------------------------------------------
# End to end, through the allocator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_near_identical_submissions_across_coldkeys_share_one_budget(
    session: AsyncSession,
) -> None:
    """The incident, reproduced and then closed.

    Four submissions from one family, four different payment coldkeys, four
    idle validators. The owner gate is satisfied by every one of them.
    """
    for index, name in enumerate(("family_a", "family_b", "family_c", "family_d")):
        await _seed(
            session,
            fingerprint=_fingerprint(name),
            coldkey=f"5Coldkey{index}",
            created_at=_NOW - timedelta(hours=4 - index),
        )
    running = await _distinct_submissions_leased(
        session, validators=8, policy=SimilarityBudgetPolicy()
    )

    assert len(running) == 1


@pytest.mark.asyncio
async def test_the_same_four_all_lease_without_the_rail(
    session: AsyncSession,
) -> None:
    """The control: this is what production does today, and why."""
    for index, name in enumerate(("family_a", "family_b", "family_c", "family_d")):
        await _seed(
            session,
            fingerprint=_fingerprint(name),
            coldkey=f"5Coldkey{index}",
            created_at=_NOW - timedelta(hours=4 - index),
        )

    running = await _distinct_submissions_leased(session, validators=8, policy=None)

    assert len(running) == 4


@pytest.mark.asyncio
async def test_distinct_miners_keep_their_own_budgets(
    session: AsyncSession,
) -> None:
    """The expensive failure: four honest miners must all still run.

    All four build on the same public starter kit and share byte-identical
    scaffold files. A metric that measured raw source would collapse them into
    one budget and quietly halve every one of their throughputs.
    """
    for index, name in enumerate(
        ("miner_gkat", "miner_kabaw", "miner_lihai", "miner_oraclemind")
    ):
        await _seed(
            session,
            fingerprint=_fingerprint(name),
            coldkey=f"5Coldkey{index}",
            created_at=_NOW - timedelta(hours=4 - index),
        )
    running = await _distinct_submissions_leased(
        session, validators=8, policy=SimilarityBudgetPolicy()
    )

    assert len(running) == 4


@pytest.mark.asyncio
async def test_a_submission_without_a_fingerprint_is_never_grouped(
    session: AsyncSession,
) -> None:
    """No evidence means its own budget, not a guess."""
    await _seed(
        session,
        fingerprint=None,
        coldkey="5ColdkeyA",
        created_at=_NOW - timedelta(hours=2),
    )
    await _seed(
        session,
        fingerprint=None,
        coldkey="5ColdkeyB",
        created_at=_NOW - timedelta(hours=1),
    )
    running = await _distinct_submissions_leased(
        session, validators=8, policy=SimilarityBudgetPolicy()
    )

    assert len(running) == 2


@pytest.mark.asyncio
async def test_the_budget_frees_when_the_twins_lease_ends(
    session: AsyncSession,
) -> None:
    """``similarity_serialized`` is a wait, not a rejection."""
    held = await _seed(
        session,
        fingerprint=_fingerprint("family_a"),
        coldkey="5ColdkeyA",
        created_at=_NOW - timedelta(hours=2),
        lease_validators=_HOLDERS,
    )
    waiting = await _seed(
        session,
        fingerprint=_fingerprint("family_b"),
        coldkey="5ColdkeyB",
        created_at=_NOW - timedelta(hours=1),
    )
    policy = SimilarityBudgetPolicy()

    async def gate_on_waiting() -> str | None:
        await session.rollback()
        preview = await preview_queue_order(
            session,
            bench_version=_BENCH,
            now=_NOW,
            agent_ids=[waiting],
            score_continuation_floor=None,
            provisional_contender_floor=None,
            rollout=None,
            similarity_policy=policy,
        )
        return preview[waiting].gate

    assert await gate_on_waiting() == "similarity_serialized"

    await session.rollback()
    async with session.begin():
        for holder in _HOLDERS:
            ticket = await session.get(ValidatorTicket, (held, _BENCH, holder))
            assert ticket is not None
            ticket.status = TicketStatus.EXPIRED

    assert await gate_on_waiting() is None


# ---------------------------------------------------------------------------
# The operator's view, from the same one expression
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_preview_names_the_submission_and_the_evidence(
    session: AsyncSession,
) -> None:
    """A miner waiting on someone else's row can only act on a named reason."""
    held = await _seed(
        session,
        fingerprint=_fingerprint("family_a"),
        coldkey="5ColdkeyA",
        created_at=_NOW - timedelta(hours=2),
        lease_validators=_HOLDERS,
    )
    waiting = await _seed(
        session,
        fingerprint=_fingerprint("family_b"),
        coldkey="5ColdkeyB",
        created_at=_NOW - timedelta(hours=1),
    )

    preview = await preview_queue_order(
        session,
        bench_version=_BENCH,
        now=_NOW,
        agent_ids=[waiting],
        score_continuation_floor=None,
        provisional_contender_floor=None,
        rollout=None,
        similarity_policy=SimilarityBudgetPolicy(),
    )

    entry = preview[waiting]
    assert entry.gate == "similarity_serialized"
    assert entry.leasable is False
    assert entry.gate_detail is not None
    assert str(held) in entry.gate_detail
    assert "jaccard" in entry.gate_detail
    # A capacity statement, never an accusation.
    assert "concurrency budget" in entry.gate_detail


@pytest.mark.asyncio
async def test_the_preview_says_nothing_when_the_rail_is_off(
    session: AsyncSession,
) -> None:
    await _seed(
        session,
        fingerprint=_fingerprint("family_a"),
        coldkey="5ColdkeyA",
        created_at=_NOW - timedelta(hours=2),
        lease_validators=_HOLDERS,
    )
    waiting = await _seed(
        session,
        fingerprint=_fingerprint("family_b"),
        coldkey="5ColdkeyB",
        created_at=_NOW - timedelta(hours=1),
    )

    preview = await preview_queue_order(
        session,
        bench_version=_BENCH,
        now=_NOW,
        agent_ids=[waiting],
        score_continuation_floor=None,
        provisional_contender_floor=None,
        rollout=None,
    )

    assert preview[waiting].gate is None
    assert preview[waiting].gate_detail is None


@pytest.mark.asyncio
async def test_the_preview_and_the_allocator_agree_on_the_refusal(
    session: AsyncSession,
) -> None:
    """One expression, two callers -- the invariant #463 exists to protect."""
    await _seed(
        session,
        fingerprint=_fingerprint("family_a"),
        coldkey="5ColdkeyA",
        created_at=_NOW - timedelta(hours=2),
        lease_validators=_HOLDERS,
    )
    waiting = await _seed(
        session,
        fingerprint=_fingerprint("family_b"),
        coldkey="5ColdkeyB",
        created_at=_NOW - timedelta(hours=1),
    )
    policy = SimilarityBudgetPolicy()

    preview = await preview_queue_order(
        session,
        bench_version=_BENCH,
        now=_NOW,
        agent_ids=[waiting],
        score_continuation_floor=None,
        provisional_contender_floor=None,
        rollout=None,
        similarity_policy=policy,
    )
    leased = await _lease(session, validator="5ValidatorB", policy=policy)

    assert preview[waiting].gate == "similarity_serialized"
    assert leased is None

    # Relaxing the one knob relaxes BOTH callers, because there is only one
    # place to relax. If a second copy of the rule ever appears, this fails.
    relaxed_preview = await preview_queue_order(
        session,
        bench_version=_BENCH,
        now=_NOW,
        agent_ids=[waiting],
        score_continuation_floor=None,
        provisional_contender_floor=None,
        rollout=None,
        similarity_policy=policy,
        similarity_concurrent_submission_limit=2,
    )
    relaxed_leased = await _lease(
        session, validator="5ValidatorB", policy=policy, limit=2
    )

    assert relaxed_preview[waiting].gate is None
    assert relaxed_leased == waiting
