"""The preview and the allocator must rank the queue identically.

Three miner-visible divergences landed in one evening because the public queue
preview restated the allocator's ordering in Python instead of sharing it. The
tests here are the standing guarantee that a fourth cannot: over generated
fixture sets, the row :func:`ditto.db.queries.queue_order.preview_queue_order`
puts first is the row :func:`ditto.db.queries.tickets.issue_ticket` actually
leases.

Each historical divergence also has its own named regression below, seeded so
that the specific confusion that caused it (a coldkey that differs from its
hotkey; a stranded pre-rollout backlog) is present in the data.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import ditto.db.queries.queue_order as queue_order_module
from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.api_models.ticket_status import TicketPurpose, TicketStatus
from ditto.db.models import (
    Agent,
    BenchmarkDataset,
    BenchmarkRollout,
    EvaluationPayment,
    Score,
    SubmissionRetirement,
    ValidatorTicket,
)
from ditto.db.queries.attestation import record_attestation
from ditto.db.queries.benchmark_admission import activated_rollout_for_version
from ditto.db.queries.queue_order import (
    OWNER_CONCURRENT_SUBMISSION_LIMIT_DEFAULT,
    QueuePreviewEntry,
    owner_capacity_gate,
    preview_artifact_mode,
    preview_queue_order,
    resolve_owner_linkage,
    resolve_owner_linkage_batch,
)
from ditto.db.queries.tickets import get_score_priority_floors, issue_ticket

pytestmark = pytest.mark.asyncio

_NOW = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)
_TTL = timedelta(minutes=90)
# The current scoreable era. This was v2 while v2 was still leasable; the
# bench-version floor now refuses to write a sub-7 ticket or score at all, and
# nothing here is about a particular benchmark -- the preview and the allocator
# have to agree on whatever era the fleet is on.
_BENCH = 7
_FRESH_VALIDATOR = "5FreshValidatorWithNoPriorTickets"


async def _seed_agent(
    session: AsyncSession,
    *,
    name: str,
    hotkey: str,
    coldkey: str | None,
    created_at: datetime,
    scores: tuple[float, ...] = (),
    live_lease_validator: str | None = None,
) -> UUID:
    """One waiting submission, with the ticket rows its scores imply.

    A recorded score always has an accepted ticket behind it in production --
    a validator cannot post one without holding a lease -- and the allocator's
    contender lane counts accepted tickets, so seeding scores alone would
    quietly disable the lane under test.

    The verified screened image and the dataset pin are here because ``_BENCH``
    is a post-v2 contract: ``queue_candidate_predicate`` filters out anything
    without them before ranking begins, so an agent seeded the v2 way is simply
    not in the queue and every ordering assertion below would pass vacuously.
    """
    agent_id = uuid4()
    async with session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=hotkey,
                name=name,
                sha256="ab" * 32,
                status=AgentStatus.EVALUATING,
                screening_policy_version=SCREENING_POLICY_VERSION,
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
        if coldkey is not None:
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
        for index, composite in enumerate(scores):
            scorer = f"5Scorer{index}"
            session.add(
                Score(
                    agent_id=agent_id,
                    bench_version=_BENCH,
                    validator_hotkey=scorer,
                    run_id=f"run-{index}",
                    signature=None,
                    seed=index,
                    composite=composite,
                    tool_mean=composite,
                    memory_mean=composite,
                    median_ms=100,
                    n=114,
                    details={"bench_version": _BENCH},
                    generated_at=created_at + timedelta(minutes=index),
                )
            )
            session.add(
                ValidatorTicket(
                    agent_id=agent_id,
                    bench_version=_BENCH,
                    validator_hotkey=scorer,
                    slot_id="slot-0",
                    status=TicketStatus.SCORED,
                    purpose=TicketPurpose.CANONICAL_QUORUM,
                    purpose_revision=1,
                    issued_at=created_at + timedelta(minutes=index),
                    deadline=created_at + timedelta(minutes=index) + _TTL,
                    attempt_count=1,
                )
            )
        if live_lease_validator is not None:
            session.add(
                ValidatorTicket(
                    agent_id=agent_id,
                    bench_version=_BENCH,
                    validator_hotkey=live_lease_validator,
                    slot_id="slot-0",
                    status=TicketStatus.ISSUED,
                    purpose=TicketPurpose.CANONICAL_QUORUM,
                    purpose_revision=1,
                    issued_at=_NOW - timedelta(minutes=5),
                    deadline=_NOW + _TTL,
                    attempt_count=1,
                )
            )
    return agent_id


async def _preview_entries(
    session: AsyncSession,
    *,
    agent_ids: list[UUID],
    previous_generation: set[UUID] | None = None,
) -> dict[UUID, QueuePreviewEntry]:
    """The preview's verdict on ``agent_ids``: rank plus gate."""
    continuation, provisional = await get_score_priority_floors(
        session, bench_version=_BENCH
    )
    return await preview_queue_order(
        session,
        bench_version=_BENCH,
        now=_NOW,
        agent_ids=agent_ids,
        score_continuation_floor=continuation,
        provisional_contender_floor=provisional,
        rollout=await activated_rollout_for_version(session, bench_version=_BENCH),
        previous_generation_agent_ids=previous_generation or set(),
    )


async def _preview(
    session: AsyncSession,
    *,
    agent_ids: list[UUID],
    previous_generation: set[UUID] | None = None,
) -> list[UUID]:
    """The preview's ranking of ``agent_ids``, best first."""
    entries = await _preview_entries(
        session, agent_ids=agent_ids, previous_generation=previous_generation
    )
    return [entry.agent_id for entry in sorted(entries.values(), key=lambda e: e.rank)]


async def _allocator_pick(
    session: AsyncSession, *, validator_hotkey: str = _FRESH_VALIDATOR
) -> UUID | None:
    """What the allocator hands a validator holding no tickets at all.

    A fresh validator is deliberate: the per-validator terms the shared
    ordering cannot carry (``had_prior_ticket``, ``already_mine``, retry
    cooldowns) are all inert for it, so any disagreement with the preview is a
    real divergence rather than a rule the preview never claimed to model.
    """
    # The preview's reads leave an implicit transaction open on this session.
    await session.rollback()
    async with session.begin():
        ticket = await issue_ticket(
            session,
            validator_hotkey=validator_hotkey,
            now=_NOW,
            ttl=_TTL,
            bench_version=_BENCH,
            artifact_mode=preview_artifact_mode(_BENCH),
        )
        return None if ticket is None else ticket.agent_id


class TestPreviewMatchesAllocator:
    """The head of the preview is the row the allocator leases."""

    @pytest.mark.integration
    @pytest.mark.parametrize("seed", range(12))
    async def test_generated_worlds_agree_on_the_next_submission(
        self, session: AsyncSession, seed: int
    ) -> None:
        """Property: over random queues, preview rank 1 == the allocator's pick.

        The generator deliberately produces the shapes the three incidents
        turned on: several hotkeys funded by one coldkey, submissions at every
        score count from zero to quorum-minus-one, live leases, and owners with
        multiple generations.

        The preview answers for the allocator's *ordinary* pass, so the property
        has two halves and the second one is where the owner relaxation lives:

        * While the preview has any leasable row, that row is what the allocator
          leases. This is the original property, unweakened -- the last-resort
          pass must never reorder the queue or serve one owner ahead of another.
        * When the preview has none, the allocator may still fill an otherwise
          idle slot from its last-resort pass, and what it picks must be a row
          the preview gated ``owner_serialized``. Never a ``not_leasable`` row
          and never a ``previous_generation`` one: relaxing the owner ceiling is
          the only rule the second pass is allowed to relax.
        """
        rng = random.Random(seed)
        coldkeys = [f"5Coldkey{index}" for index in range(3)]
        agent_ids: list[UUID] = []
        for index in range(rng.randint(4, 9)):
            coldkey = rng.choice(coldkeys)
            score_count = rng.randint(0, 2)
            agent_ids.append(
                await _seed_agent(
                    session,
                    name=f"agent-{index}",
                    # Several hotkeys per coldkey: the #435 shape.
                    hotkey=f"5Hotkey{index % 5}",
                    coldkey=coldkey,
                    created_at=_NOW - timedelta(hours=rng.randint(1, 200)),
                    scores=tuple(
                        round(rng.uniform(0.1, 0.99), 3) for _ in range(score_count)
                    ),
                    live_lease_validator=(
                        f"5Busy{index}" if rng.random() < 0.2 else None
                    ),
                )
            )

        ranked = await _preview(session, agent_ids=agent_ids)
        entries = await _preview_entries(session, agent_ids=agent_ids)
        leasable = [agent_id for agent_id in ranked if entries[agent_id].gate is None]
        picked = await _allocator_pick(session)

        if leasable:
            assert picked == leasable[0], (
                "the queue preview and the ticket allocator disagree about which "
                "submission is next; they are supposed to share one ordering"
            )
        elif picked is not None:
            assert entries[picked].gate == "owner_serialized", (
                "the allocator's last-resort pass leased a row the preview did "
                "not gate on owner capacity; it may only relax the owner "
                f"ceiling, but this row was gated {entries[picked].gate!r}"
            )

    @pytest.mark.integration
    async def test_owner_serialization_is_visible_rather_than_silent(
        self, session: AsyncSession
    ) -> None:
        """A miner's parked submission is ranked last *and* says why.

        ``issue_ticket`` pins an owner's capacity to whichever generation
        started progressing first, so the sibling never moves. The preview used
        to model none of this and ranked the sibling as though it were next --
        the single largest source of "why isn't mine moving".
        """
        progressing = await _seed_agent(
            session,
            name="progressing",
            hotkey="5OwnerHotkeyOne",
            coldkey="5SharedOwnerColdkey",
            created_at=_NOW - timedelta(hours=10),
            scores=(0.7,),
        )
        # Same owner, different hotkey: rotating hotkeys buys no second slot.
        parked = await _seed_agent(
            session,
            name="parked",
            hotkey="5OwnerHotkeyTwo",
            coldkey="5SharedOwnerColdkey",
            created_at=_NOW - timedelta(hours=9),
        )
        other = await _seed_agent(
            session,
            name="other-miner",
            hotkey="5UnrelatedHotkey",
            coldkey="5UnrelatedColdkey",
            created_at=_NOW - timedelta(hours=1),
        )

        entries = await _preview_entries(
            session, agent_ids=[progressing, parked, other]
        )
        assert entries[parked].gate == "owner_serialized"
        assert entries[other].gate is None
        assert entries[parked].rank > entries[other].rank
        assert await _allocator_pick(session) != parked

    @pytest.mark.integration
    async def test_owner_queries_reuse_aliases_across_preview_rows(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One preview does not rebuild ORM proxy metadata per owner row.

        A production CPU profile attributed most queue-preview time to
        SQLAlchemy adapting fresh aliases in the owner loop.  The aliases are
        immutable statement metadata, so constructing another one after module
        import is both unnecessary and a regression in this hot path.
        """
        agent_ids = [
            await _seed_agent(
                session,
                name=f"owner-{index}",
                hotkey=f"5OwnerHotkey{index}",
                coldkey=f"5OwnerColdkey{index}",
                created_at=_NOW - timedelta(hours=index + 1),
            )
            for index in range(3)
        ]

        def fail_on_fresh_alias(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("queue preview rebuilt a per-owner ORM alias")

        linkage = await resolve_owner_linkage_batch(session, agent_ids=agent_ids)
        monkeypatch.setattr(queue_order_module, "aliased", fail_on_fresh_alias)
        for agent_id in agent_ids:
            selected = await queue_order_module.selected_owner_agent_id(
                session,
                linkage=linkage[agent_id],
                bench_version=_BENCH,
                now=_NOW,
                provisional_contender_floor=None,
                rollout=None,
                capable_validator_hotkeys=(),
            )
            leases = await queue_order_module.owner_live_lease_agent_ids(
                session, linkage=linkage[agent_id], now=_NOW
            )
            assert selected is None
            assert leases == set()

    @pytest.mark.integration
    async def test_a_retired_submission_is_excluded_from_both(
        self, session: AsyncSession
    ) -> None:
        """Retirement is a real queue exclusion, not just a public label.

        The exclusion lives in ``queue_candidate_predicate`` rather than at
        ``issue_ticket``'s call site precisely so it cannot hold for one side
        and not the other: a row the allocator will never lease again must not
        be ranked by the preview as though it were next.
        """
        retired = await _seed_agent(
            session,
            name="retired",
            hotkey="5RetiredHotkey",
            coldkey="5RetiredColdkey",
            created_at=_NOW - timedelta(hours=10),
        )
        live = await _seed_agent(
            session,
            name="live",
            hotkey="5LiveHotkey",
            coldkey="5LiveColdkey",
            created_at=_NOW - timedelta(hours=1),
        )
        async with session.begin():
            session.add(
                SubmissionRetirement(
                    retirement_id=uuid4(),
                    agent_id=retired,
                    bench_version=_BENCH,
                    superseded_by_version=_BENCH + 1,
                    actor="operator",
                    reason="the generation this was queued against has closed",
                    expected_snapshot="waiting_validator",
                    score_count=0,
                    ticket_snapshot=[],
                )
            )

        entries = await _preview_entries(session, agent_ids=[retired, live])
        # The preview gates rather than drops, so the row still carries a
        # reason. ``/activity`` never even sends it here -- a retired row is not
        # in the waiting population -- but the two layers must agree if it does.
        assert entries[retired].gate == "not_leasable"
        assert entries[live].gate is None
        assert entries[retired].rank > entries[live].rank
        # The older row would otherwise be leased first; retirement is the only
        # reason the allocator skips it.
        assert await _allocator_pick(session) == live


class TestHistoricalDivergences:
    """One test per divergence that reached a miner."""

    @pytest.mark.integration
    async def test_contender_lane_is_one_slot_per_coldkey_not_per_hotkey(
        self, session: AsyncSession
    ) -> None:
        """#435: the preview deduped contenders by hotkey, the allocator by coldkey.

        The fixture is built so the two groupings produce *different* orders,
        which is the part the original review missed: when every hotkey has its
        own coldkey the two rules are indistinguishable, and the divergence
        shipped. Here one coldkey funds two hotkeys, and the owner's slot is
        pinned to the weaker of them (its progress started first), so the
        surviving row's lane is decided purely by how contenders are grouped.

        By coldkey -- the allocator's rule -- the owner already spent its one
        contender slot on ``strong``, so ``weaker`` drops to the ordinary queue
        and the independent miner's contender outranks it. By hotkey, ``weaker``
        would keep a contender slot of its own and jump the independent miner.
        """
        owner_coldkey = "5OneColdkeyTwoHotkeys"
        weaker = await _seed_agent(
            session,
            name="weaker",
            hotkey="5HotkeyB",
            coldkey=owner_coldkey,
            created_at=_NOW - timedelta(hours=9),
            scores=(0.85,),
        )
        strong = await _seed_agent(
            session,
            name="strong",
            hotkey="5HotkeyA",
            coldkey=owner_coldkey,
            created_at=_NOW - timedelta(hours=5),
            scores=(0.90,),
        )
        independent = await _seed_agent(
            session,
            name="independent",
            hotkey="5HotkeyC",
            coldkey="5IndependentColdkey",
            created_at=_NOW - timedelta(hours=3),
            scores=(0.50,),
        )

        entries = await _preview_entries(
            session, agent_ids=[strong, weaker, independent]
        )

        # The owner's slot is pinned to whichever generation started first.
        assert entries[strong].gate == "owner_serialized"
        assert entries[weaker].gate is None
        # ``strong`` took the coldkey's single contender slot, so ``weaker`` is
        # not a contender and the independent miner's 0.50 outranks its 0.85.
        assert entries[independent].rank < entries[weaker].rank
        assert await _allocator_pick(session) == independent

    @pytest.mark.integration
    async def test_previous_generation_never_holds_the_head_of_the_queue(
        self, session: AsyncSession
    ) -> None:
        """#448: stranded pre-rollout rows outranked every fresh submission.

        They are served only by the carryover and source-backfill lanes, which
        the operator policy holds strictly behind the whole desired era, so a
        preview that ranks them by arrival tells miners the opposite of what
        the fleet will do -- the report that named v6 work as being graded
        ahead of v7.
        """
        rollout_started = _NOW - timedelta(days=2)
        stranded = await _seed_agent(
            session,
            name="stranded",
            hotkey="5StrandedHotkey",
            coldkey="5ColdStranded",
            created_at=rollout_started - timedelta(days=5),
        )
        fresh = await _seed_agent(
            session,
            name="fresh",
            hotkey="5FreshHotkey",
            coldkey="5ColdFresh",
            created_at=rollout_started + timedelta(hours=6),
        )
        async with session.begin():
            session.add(
                BenchmarkRollout(
                    rollout_id=uuid4(),
                    from_version=_BENCH - 1,
                    desired_version=_BENCH,
                    status="collecting",
                    cohort_size=5,
                    created_at=rollout_started,
                )
            )

        entries = await _preview_entries(
            session,
            agent_ids=[stranded, fresh],
            previous_generation={stranded},
        )
        assert entries[fresh].rank == 1
        assert entries[stranded].rank == 2
        # And it must not be presentable as imminent at any rank.
        assert entries[stranded].gate == "previous_generation"
        assert entries[fresh].gate is None


class TestOwnerLinkage:
    """The batch resolver the preview uses must match the allocator's."""

    @pytest.mark.integration
    async def test_batch_linkage_matches_the_per_candidate_resolver(
        self, session: AsyncSession
    ) -> None:
        """Two hops, resolved two ways, over a deliberately tangled ledger."""
        agent_ids = [
            await _seed_agent(
                session,
                name="one",
                hotkey="5Rotating",
                coldkey="5ColdOne",
                created_at=_NOW - timedelta(hours=3),
            ),
            await _seed_agent(
                session,
                name="two",
                hotkey="5Rotating",
                coldkey="5ColdTwo",
                created_at=_NOW - timedelta(hours=2),
            ),
            await _seed_agent(
                session,
                name="three",
                hotkey="5Other",
                coldkey="5ColdTwo",
                created_at=_NOW - timedelta(hours=1),
            ),
            # A legacy row with no payment at all.
            await _seed_agent(
                session,
                name="legacy",
                hotkey="5LegacyOnly",
                coldkey=None,
                created_at=_NOW,
            ),
        ]

        batch = await resolve_owner_linkage_batch(session, agent_ids=agent_ids)
        for agent_id in agent_ids:
            assert batch[agent_id] == await resolve_owner_linkage(
                session, agent_id=agent_id
            )

    @pytest.mark.integration
    async def test_attested_hotkeys_share_one_capacity_owner(
        self, session: AsyncSession
    ) -> None:
        """A proved cross-coldkey owner link also serializes queue capacity."""
        hotkey_a = "5AttestedOwnerA"
        hotkey_b = "5AttestedOwnerB"
        agent_a = await _seed_agent(
            session,
            name="linked-a",
            hotkey=hotkey_a,
            coldkey="5LinkedColdA",
            created_at=_NOW - timedelta(hours=1),
        )
        agent_b = await _seed_agent(
            session,
            name="linked-b",
            hotkey=hotkey_b,
            coldkey="5LinkedColdB",
            created_at=_NOW,
        )
        async with session.begin():
            await record_attestation(
                session,
                netuid=118,
                hotkey_lo=min(hotkey_a, hotkey_b),
                hotkey_hi=max(hotkey_a, hotkey_b),
                nonce=uuid4(),
                issued_at=_NOW,
                lo_key_kind="hotkey",
                lo_signer=min(hotkey_a, hotkey_b),
                lo_signature="ab" * 64,
                hi_key_kind="hotkey",
                hi_signer=max(hotkey_a, hotkey_b),
                hi_signature="cd" * 64,
            )

        linkage_a = await resolve_owner_linkage(session, agent_id=agent_a)
        linkage_b = await resolve_owner_linkage(session, agent_id=agent_b)
        assert linkage_a.hotkeys == linkage_b.hotkeys == frozenset({hotkey_a, hotkey_b})
        assert (
            linkage_a.coldkeys
            == linkage_b.coldkeys
            == frozenset({"5LinkedColdA", "5LinkedColdB"})
        )
        batch = await resolve_owner_linkage_batch(session, agent_ids=(agent_a, agent_b))
        assert batch[agent_a] == linkage_a
        assert batch[agent_b] == linkage_b


class TestOwnerCapacityGate:
    """The one predicate the preview and the allocator both ask.

    Exercised directly because its inputs -- a pin, a live-lease set, a ceiling
    and which pass is running -- are cheap to state exactly here and expensive
    to arrange as fixtures, and because every one of these cases is a rule
    somebody could plausibly "simplify" away.
    """

    def _ids(self, count: int) -> list[UUID]:
        return [UUID(int=index + 1) for index in range(count)]

    async def test_a_lone_submission_is_never_gated(self) -> None:
        (agent,) = self._ids(1)
        assert (
            owner_capacity_gate(
                agent_id=agent, selected_agent_id=None, live_lease_agent_ids=()
            )
            is None
        )

    async def test_ordinary_pass_serializes_on_a_live_sibling(self) -> None:
        agent, sibling = self._ids(2)
        assert (
            owner_capacity_gate(
                agent_id=agent,
                selected_agent_id=None,
                live_lease_agent_ids=(sibling,),
            )
            == "owner_serialized"
        )

    async def test_ordinary_pass_serializes_on_the_pin_with_no_lease_at_all(
        self,
    ) -> None:
        """The pin holds the owner's slot across the gaps between leases."""
        agent, pinned = self._ids(2)
        assert (
            owner_capacity_gate(
                agent_id=agent, selected_agent_id=pinned, live_lease_agent_ids=()
            )
            == "owner_serialized"
        )

    async def test_last_resort_admits_a_second_submission(self) -> None:
        agent, sibling = self._ids(2)
        assert (
            owner_capacity_gate(
                agent_id=agent,
                selected_agent_id=sibling,
                live_lease_agent_ids=(sibling,),
                last_resort=True,
            )
            is None
        )

    async def test_last_resort_still_stops_at_the_ceiling(self) -> None:
        agent, first, second = self._ids(3)
        assert (
            owner_capacity_gate(
                agent_id=agent,
                selected_agent_id=first,
                live_lease_agent_ids=(first, second),
                last_resort=True,
            )
            == "owner_serialized"
        )

    async def test_a_ceiling_of_one_is_the_old_rule_exactly(self) -> None:
        """The knob's identity value: last resort becomes a no-op."""
        agent, sibling = self._ids(2)
        assert (
            owner_capacity_gate(
                agent_id=agent,
                selected_agent_id=sibling,
                live_lease_agent_ids=(sibling,),
                concurrent_submission_limit=1,
                last_resort=True,
            )
            == "owner_serialized"
        )

    async def test_the_pinned_generation_is_exempt_from_the_ceiling(self) -> None:
        """Otherwise a relaxed sibling locks out the row the pin protects."""
        pinned, sibling = self._ids(2)
        assert (
            owner_capacity_gate(
                agent_id=pinned,
                selected_agent_id=pinned,
                live_lease_agent_ids=(sibling, pinned),
            )
            is None
        )

    async def test_joining_a_submission_that_already_holds_a_lease_is_free(
        self,
    ) -> None:
        """Quorum, not the owner ceiling, bounds slots on ONE submission.

        The owner already occupies two submissions; adding a third validator to
        one of them does not make it three, so the ceiling must not refuse it.
        """
        agent, sibling = self._ids(2)
        assert (
            owner_capacity_gate(
                agent_id=agent,
                selected_agent_id=sibling,
                live_lease_agent_ids=(agent, sibling),
                concurrent_submission_limit=OWNER_CONCURRENT_SUBMISSION_LIMIT_DEFAULT,
                last_resort=True,
            )
            is None
        )
