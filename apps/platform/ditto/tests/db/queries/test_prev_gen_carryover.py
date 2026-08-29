"""Admission, selection and leasing of stranded previous-generation work.

Every case here fails without the carryover change. The ones that matter most
are the *negative* ones: the shipped default must admit nothing, and a bare
desired-version dataset must never admit on its own.

The three legs this exercises are one contract, and each was independently
capable of producing an outage when moved alone:

1. ``benchmark_admission_predicate`` -- eligibility;
2. the desired-version ``BenchmarkDataset`` that ``issue_ticket`` hard-requires;
3. a ticket-issuing path, because every existing desired-version issuance
   filters on ``Agent.created_at >= rollout.created_at`` and so can never reach a
   previous-generation agent however admitted.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.queue_policy_settings import PrevGenCarryoverSettings
from ditto.api_models.ticket_status import TicketStatus
from ditto.db.models import (
    Agent,
    BenchmarkDataset,
    BenchmarkRollout,
    BenchmarkRolloutCarryover,
    BenchmarkRolloutMember,
    EvaluationPayment,
    Score,
    ValidatorQueueWithdrawal,
    ValidatorTicket,
)
from ditto.db.queries.benchmark_admission import (
    agent_is_admitted,
    benchmark_admission_predicate,
)
from ditto.db.queries.benchmark_carryover import (
    adopt_carryover_agent,
    carryover_agent_ids,
    stranded_prev_gen_candidates,
)
from ditto.db.queries.benchmark_rollout import DatasetPin, arrival_bench_version
from ditto.db.queries.tickets import MAX_ATTEMPTS_PER_VERSION, issue_ticket
from ditto.tests.legacy_era import (
    grandfather_active_era,
    retired_era_writes_allowed,
)
from ditto_screening_protocol import SCREENING_FLOOR_POLICY_VERSION

# Every use of SCREENING_POLICY_VERSION in this module means "the version the
# platform REQUIRES," which — with no scheduled activation written — is the
# floor, not the newest text the deployed build implements. The runtime query
# builders read the effective snapshot, which defaults to the floor.
SCREENING_POLICY_VERSION = SCREENING_FLOOR_POLICY_VERSION


_ROLLOUT_START = datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)
_NOW = _ROLLOUT_START + timedelta(days=2)
_TTL = timedelta(minutes=90)
# The generation the stranded work belongs to, and the one it is carried over
# into. Unlike most benchmark versions in the suite these two are NOT arbitrary
# and cannot be renumbered upward: carryover only exists during a rollout, the
# newest shipped contract is v7 (``benchmark_contract`` fails closed above it,
# so no ticket can issue for v8), and a rollout must move forward. That pins
# the pair at 6 -> 7 -- which means the source generation is a RETIRED one, and
# the stranded ``scores`` and burnt ``validator_tickets`` this file is about are
# rows the floor refuses to write. Production holds them because the floor is
# NOT VALID and grandfathered them; a fresh test database has to be put into the
# same state on purpose, which is what ``_seeding_the_retired_era`` does.
_FROM_VERSION = 6
_DESIRED_VERSION = 7

# The shipped default. Named so every "no-op" assertion below is visibly made
# against the object an un-revisioned deployment actually resolves.
_SHIPPED_DEFAULT = PrevGenCarryoverSettings()
_ENABLED = PrevGenCarryoverSettings(enabled=True)


@asynccontextmanager
async def _seeding_the_retired_era(session: AsyncSession) -> AsyncIterator[None]:
    """Open the seeding transaction with the retired-era floor lifted.

    Everything written inside is history: a v6 submission that never reached
    quorum, the two validator scores it did collect, the ticket whose attempts
    it burnt. Those are precisely the rows ``scores_bench_version_floor`` and
    the ``validator_tickets`` trigger exist to stop anything from creating
    *now*, and they are also the only thing carryover has to work on.

    The floor is restored -- NOT VALID, exactly as the migration declares it --
    before the assertions run, so the seeded rows are grandfathered the same way
    production's are and every write the code under test attempts afterwards
    still meets the live floor. Nothing here weakens the guarantee; it
    reproduces the database production is actually in.
    """
    async with retired_era_writes_allowed(session), session.begin():
        yield


async def _seed_rollout(
    session: AsyncSession, *, status: str = "collecting", cohort_size: int = 10
) -> BenchmarkRollout:
    rollout = BenchmarkRollout(
        rollout_id=uuid4(),
        from_version=_FROM_VERSION,
        desired_version=_DESIRED_VERSION,
        status=status,
        cohort_size=cohort_size,
        created_at=_ROLLOUT_START,
        activated_at=_ROLLOUT_START if status == "activated" else None,
        rescore_cohort_target=10,
        priority_cohort_target=5,
    )
    session.add(rollout)
    await session.flush()
    return rollout


async def _seed_activated_source_era(session: AsyncSession) -> BenchmarkRollout:
    """The activation that made ``_FROM_VERSION`` the era in force."""
    return await grandfather_active_era(
        session, version=_FROM_VERSION, now=_ROLLOUT_START - timedelta(days=30)
    )


async def _seed_stranded(
    session: AsyncSession,
    *,
    name: str,
    score_count: int = 2,
    age_days: int = 5,
    coldkey: str | None = None,
    hotkey: str | None = None,
    screening_policy_version: int = SCREENING_POLICY_VERSION,
    status: AgentStatus = AgentStatus.EVALUATING,
    created_at: datetime | None = None,
) -> UUID:
    """One previous-generation submission below quorum on the source version."""
    agent_id = uuid4()
    miner_hotkey = hotkey or f"5Miner-{name}"
    agent = Agent(
        agent_id=agent_id,
        miner_hotkey=miner_hotkey,
        name=name,
        sha256=f"{abs(hash(name)) % (16**64):064x}",
        status=status,
        screening_policy_version=screening_policy_version,
        created_at=created_at or (_ROLLOUT_START - timedelta(days=age_days)),
    )
    # The desired era requires a complete screened image; without one the agent
    # is filtered by artifact_mode long before admission is consulted, which
    # would make an admission assertion vacuous.
    agent.screened_image_sha256 = "12" * 32
    agent.screened_image_size_bytes = 123
    agent.screened_image_id = "sha256:" + "34" * 32
    agent.screened_image_ref = f"ditto-screen/{agent_id}:latest"
    agent.screened_image_upload_id = uuid4()
    agent.screened_image_verified_at = _ROLLOUT_START
    session.add(agent)
    await session.flush()
    if coldkey is not None:
        session.add(
            EvaluationPayment(
                block_hash=f"0x{name}",
                extrinsic_index=0,
                agent_id=agent_id,
                miner_hotkey=miner_hotkey,
                miner_coldkey=coldkey,
                amount_rao=1,
                tao_usd_rate=Decimal("1"),
                dest_address="5Destination",
                timestamp=_ROLLOUT_START,
            )
        )
    for index in range(score_count):
        session.add(
            Score(
                agent_id=agent_id,
                bench_version=_FROM_VERSION,
                validator_hotkey=f"5Validator-{index}",
                run_id=f"{name}-{index}",
                signature=None,
                seed=7,
                composite=0.5,
                tool_mean=0.5,
                memory_mean=0.5,
                median_ms=1,
                n=114,
                details={"bench_version": _FROM_VERSION},
                generated_at=_ROLLOUT_START - timedelta(days=age_days),
            )
        )
    await session.flush()
    return agent_id


async def _adopt(
    session: AsyncSession, *, rollout: BenchmarkRollout, agent_id: UUID
) -> bool:
    """Run the real adoption writer, so the row/dataset coupling is exercised."""
    candidates = await stranded_prev_gen_candidates(
        session, rollout=rollout, settings=_ENABLED, now=_NOW
    )
    candidate = next(c for c in candidates if c.agent_id == agent_id)
    return await adopt_carryover_agent(
        session,
        rollout=rollout,
        candidate=candidate,
        dataset=DatasetPin(seed=7, sha256="ab" * 32, run_size="full"),
        now=_NOW,
    )


async def _admitted_ids(
    session: AsyncSession, *, rollout: BenchmarkRollout
) -> set[UUID]:
    return set(
        await session.scalars(
            select(Agent.agent_id).where(
                benchmark_admission_predicate(
                    rollout=rollout, bench_version=_DESIRED_VERSION
                )
            )
        )
    )


class TestShippedDefaultIsATotalNoOp:
    """Requirement one: merging this changes nothing until an operator acts."""

    async def test_disabled_admits_nothing_and_generates_nothing(
        self, session: AsyncSession
    ) -> None:
        async with _seeding_the_retired_era(session):
            rollout = await _seed_rollout(session)
            agent_id = await _seed_stranded(session, name="stranded")

        async with session.begin():
            # No settings revision is written at all: this is what a deployment
            # that merely merges the change actually resolves.
            assert _SHIPPED_DEFAULT.enabled is False
            selected = await stranded_prev_gen_candidates(
                session, rollout=rollout, settings=_ENABLED, now=_NOW
            )
            # The agent IS stranded -- the default's no-op is a policy decision,
            # not an accident of the data.
            assert [c.agent_id for c in selected] == [agent_id]

            # Leg 1: not admitted.
            assert agent_id not in await _admitted_ids(session, rollout=rollout)
            # Leg 2: no desired-version dataset.
            assert (
                await session.get(BenchmarkDataset, (agent_id, _DESIRED_VERSION))
            ) is None
            # Leg 3: not leasable by the carryover path either.
            assert await carryover_agent_ids(session, rollout=rollout) == []

        async with session.begin():
            ticket = await issue_ticket(
                session,
                validator_hotkey="5Validator-new",
                now=_NOW,
                ttl=_TTL,
                bench_version=_DESIRED_VERSION,
                artifact_mode="screened_only",
                only_agent_ids=[agent_id],
            )
        assert ticket is None


class TestThreeLegsMoveTogether:
    async def test_adoption_pins_dataset_admits_and_leases(
        self, session: AsyncSession
    ) -> None:
        async with _seeding_the_retired_era(session):
            rollout = await _seed_rollout(session)
            agent_id = await _seed_stranded(session, name="stranded")

        async with session.begin():
            assert await _adopt(session, rollout=rollout, agent_id=agent_id)

        async with session.begin():
            # The row and the dataset exist together, and the row records the
            # progress and owner it was adopted under.
            row = await session.get(
                BenchmarkRolloutCarryover, (rollout.rollout_id, agent_id)
            )
            assert row is not None
            assert row.position == 1
            assert row.frozen_score_count == 2
            assert row.frozen_owner_key == "hotkey:5Miner-stranded"
            dataset = await session.get(BenchmarkDataset, (agent_id, _DESIRED_VERSION))
            assert dataset is not None
            assert dataset.sha256 == "ab" * 32

            assert agent_id in await _admitted_ids(session, rollout=rollout)
            assert await carryover_agent_ids(session, rollout=rollout) == [agent_id]

        async with session.begin():
            ticket = await issue_ticket(
                session,
                validator_hotkey="5Validator-new",
                now=_NOW,
                ttl=_TTL,
                bench_version=_DESIRED_VERSION,
                artifact_mode="screened_only",
                only_agent_ids=await carryover_agent_ids(session, rollout=rollout),
            )
        assert ticket is not None
        assert ticket.agent_id == agent_id
        assert ticket.bench_version == _DESIRED_VERSION

    async def test_only_agent_ids_actually_narrows_the_candidate_set(
        self, session: AsyncSession
    ) -> None:
        """The named set must decide the lease, not merely fail to exclude it.

        Two adopted agents that are identical to every ordering term, so the
        ordinary queue falls through to ``agent_id ASC``. Asking for the HIGHER
        id proves the restriction is doing the selecting: an implementation that
        accepted the argument and ignored it would return the lower id.
        """
        async with _seeding_the_retired_era(session):
            rollout = await _seed_rollout(session)
            first = await _seed_stranded(
                session, name="twin-a", hotkey="5HotkeyA", age_days=5
            )
            second = await _seed_stranded(
                session, name="twin-b", hotkey="5HotkeyB", age_days=5
            )
        async with session.begin():
            assert await _adopt(session, rollout=rollout, agent_id=first)
            assert await _adopt(session, rollout=rollout, agent_id=second)

        unpreferred = max(first, second)
        preferred = min(first, second)
        async with session.begin():
            ticket = await issue_ticket(
                session,
                validator_hotkey="5Validator-new",
                now=_NOW,
                ttl=_TTL,
                bench_version=_DESIRED_VERSION,
                artifact_mode="screened_only",
                only_agent_ids=[unpreferred],
            )
        assert ticket is not None
        assert ticket.agent_id == unpreferred
        assert ticket.agent_id != preferred

    async def test_admission_survives_activation(self, session: AsyncSession) -> None:
        """After activation the ordinary queue must reach the adopted agent.

        The carryover ticket path only runs while the rollout is open. Once the
        rollout activates, ``issue_ticket`` applies the admission predicate
        instead, with no arrival filter -- so the disjunct is what keeps an
        adopted agent leasable rather than stranding it a second time.
        """
        async with _seeding_the_retired_era(session):
            rollout = await _seed_rollout(session)
            agent_id = await _seed_stranded(session, name="stranded")
        async with session.begin():
            assert await _adopt(session, rollout=rollout, agent_id=agent_id)
        async with session.begin():
            rollout.status = "activated"
            rollout.activated_at = _NOW
            session.add(rollout)

        async with session.begin():
            assert await agent_is_admitted(
                session, bench_version=_DESIRED_VERSION, agent_id=agent_id
            )
            ticket = await issue_ticket(
                session,
                validator_hotkey="5Validator-new",
                now=_NOW,
                ttl=_TTL,
                bench_version=_DESIRED_VERSION,
                artifact_mode="screened_only",
            )
        assert ticket is not None
        assert ticket.agent_id == agent_id

    async def test_arrival_version_follows_the_adoption(
        self, session: AsyncSession
    ) -> None:
        """A later policy rescreen must regenerate in the NEW era, not the old."""

        async def arrival(agent_id: UUID) -> int:
            # Reload from the DB so the agent's and the rollout's timestamps
            # agree on tz-awareness; the SQLite test engine round-trips naive.
            session.expunge_all()
            agent = await session.get(Agent, agent_id)
            assert agent is not None
            return await arrival_bench_version(session, agent=agent)

        async with _seeding_the_retired_era(session):
            # The un-adopted answer has to be the SOURCE era, so the source era
            # has to be the one in force. With no activation on record the
            # ledger answers the floor -- which is ``_DESIRED_VERSION`` -- and
            # the first assertion below would be 7 != 7 before adoption ever
            # happened. v6 held authority through an activated rollout that now
            # sits beneath the floor; this is that grandfathered row.
            await _seed_activated_source_era(session)
            rollout = await _seed_rollout(session)
            agent_id = await _seed_stranded(session, name="stranded")

        async with session.begin():
            assert await arrival(agent_id) == _FROM_VERSION
        async with session.begin():
            rollout = (
                await session.scalars(
                    select(BenchmarkRollout).where(
                        BenchmarkRollout.desired_version == _DESIRED_VERSION
                    )
                )
            ).one()
            assert await _adopt(session, rollout=rollout, agent_id=agent_id)
        async with session.begin():
            assert await arrival(agent_id) == _DESIRED_VERSION


class TestAdmissionCannotOutrunGeneration:
    async def test_bare_dataset_does_not_admit(self, session: AsyncSession) -> None:
        """The rescreen-cannot-self-admit invariant, restated for carryover.

        A routine policy rescreen can regenerate a desired-version dataset for a
        historical submission. If admission keyed off dataset existence, that
        rescreen would silently admit it. Admission keys off the carryover ROW,
        which only ``adopt_carryover_agent`` writes.
        """
        async with _seeding_the_retired_era(session):
            rollout = await _seed_rollout(session)
            agent_id = await _seed_stranded(session, name="rescreened")
            session.add(
                BenchmarkDataset(
                    agent_id=agent_id,
                    bench_version=_DESIRED_VERSION,
                    seed=7,
                    sha256="cd" * 32,
                    run_size="full",
                    created_at=_NOW,
                )
            )

        async with session.begin():
            assert (
                await session.get(BenchmarkDataset, (agent_id, _DESIRED_VERSION))
            ) is not None
            assert (
                await session.get(
                    BenchmarkRolloutCarryover, (rollout.rollout_id, agent_id)
                )
            ) is None
            assert agent_id not in await _admitted_ids(session, rollout=rollout)

    async def test_adoption_never_writes_a_row_without_a_dataset(
        self, session: AsyncSession
    ) -> None:
        """Both halves land in one transaction, or neither does."""
        async with _seeding_the_retired_era(session):
            rollout = await _seed_rollout(session)
            agent_id = await _seed_stranded(session, name="stranded")
        async with session.begin():
            assert await _adopt(session, rollout=rollout, agent_id=agent_id)

        async with session.begin():
            rows = list(
                await session.scalars(select(BenchmarkRolloutCarryover.agent_id))
            )
            datasets = set(
                await session.scalars(
                    select(BenchmarkDataset.agent_id).where(
                        BenchmarkDataset.bench_version == _DESIRED_VERSION
                    )
                )
            )
        assert set(rows) <= datasets

    async def test_second_adoption_is_idempotent(self, session: AsyncSession) -> None:
        async with _seeding_the_retired_era(session):
            rollout = await _seed_rollout(session)
            agent_id = await _seed_stranded(session, name="stranded")
        async with session.begin():
            assert await _adopt(session, rollout=rollout, agent_id=agent_id)
        async with session.begin():
            # A concurrent refresh re-selecting the same agent must not double
            # up: the agent is no longer even a candidate.
            assert (
                await stranded_prev_gen_candidates(
                    session, rollout=rollout, settings=_ENABLED, now=_NOW
                )
                == []
            )


class TestStrandedSelection:
    async def test_min_score_count_two_excludes_never_ticketed(
        self, session: AsyncSession
    ) -> None:
        async with _seeding_the_retired_era(session):
            rollout = await _seed_rollout(session)
            two_of_three = await _seed_stranded(
                session, name="progressed", score_count=2
            )
            zero_of_three = await _seed_stranded(
                session, name="untouched", score_count=0
            )

        async with session.begin():
            strict = await stranded_prev_gen_candidates(
                session,
                rollout=rollout,
                settings=PrevGenCarryoverSettings(enabled=True, min_score_count=2),
                now=_NOW,
            )
            permissive = await stranded_prev_gen_candidates(
                session,
                rollout=rollout,
                settings=PrevGenCarryoverSettings(enabled=True, min_score_count=0),
                now=_NOW,
            )
        assert [c.agent_id for c in strict] == [two_of_three]
        assert {c.agent_id for c in permissive} == {two_of_three, zero_of_three}

    async def test_finalized_and_new_era_submissions_are_not_stranded(
        self, session: AsyncSession
    ) -> None:
        async with _seeding_the_retired_era(session):
            rollout = await _seed_rollout(session)
            await _seed_stranded(session, name="finalized", score_count=3)
            await _seed_stranded(
                session,
                name="new-era",
                created_at=_ROLLOUT_START + timedelta(hours=1),
            )
            stranded = await _seed_stranded(session, name="stranded")

        async with session.begin():
            selected = await stranded_prev_gen_candidates(
                session, rollout=rollout, settings=_ENABLED, now=_NOW
            )
        assert [c.agent_id for c in selected] == [stranded]

    async def test_cohort_member_is_not_carried_over_again(
        self, session: AsyncSession
    ) -> None:
        async with _seeding_the_retired_era(session):
            rollout = await _seed_rollout(session)
            member = await _seed_stranded(session, name="cohort")
            session.add(
                BenchmarkRolloutMember(
                    rollout_id=rollout.rollout_id,
                    agent_id=member,
                    position=1,
                    frozen_miner_hotkey="5Miner-cohort",
                    frozen_composite=0.9,
                )
            )

        async with session.begin():
            assert (
                await stranded_prev_gen_candidates(
                    session, rollout=rollout, settings=_ENABLED, now=_NOW
                )
                == []
            )

    async def test_below_current_screening_policy_is_not_selected(
        self, session: AsyncSession
    ) -> None:
        """Not a new gate: ``issue_ticket`` already refuses to lease these.

        Selecting one would only buy a dataset generation for work no validator
        could ever be handed. An operator rescreen brings it in on the next
        convergence pass with no carryover-specific action.
        """
        async with _seeding_the_retired_era(session):
            rollout = await _seed_rollout(session)
            stale = await _seed_stranded(
                session,
                name="stale-policy",
                screening_policy_version=SCREENING_POLICY_VERSION - 1,
            )

        async with session.begin():
            assert (
                await stranded_prev_gen_candidates(
                    session, rollout=rollout, settings=_ENABLED, now=_NOW
                )
                == []
            )
            agent = await session.get(Agent, stale)
            assert agent is not None
            agent.screening_policy_version = SCREENING_POLICY_VERSION

        async with session.begin():
            selected = await stranded_prev_gen_candidates(
                session, rollout=rollout, settings=_ENABLED, now=_NOW
            )
        assert [c.agent_id for c in selected] == [stale]

    async def test_queue_withdrawal_is_respected(self, session: AsyncSession) -> None:
        async with _seeding_the_retired_era(session):
            rollout = await _seed_rollout(session)
            withdrawn = await _seed_stranded(session, name="withdrawn")
            session.add(
                ValidatorQueueWithdrawal(
                    withdrawal_id=uuid4(),
                    agent_id=withdrawn,
                    bench_version=_FROM_VERSION,
                    reason="miner asked to stop",
                    actor="admin",
                    expected_snapshot="2/3",
                    score_count=2,
                    ticket_snapshot=[],
                    created_at=_NOW,
                )
            )

        async with session.begin():
            assert (
                await stranded_prev_gen_candidates(
                    session, rollout=rollout, settings=_ENABLED, now=_NOW
                )
                == []
            )


class TestExhaustionPolicy:
    @staticmethod
    async def _exhaust(session: AsyncSession, agent_id: UUID) -> None:
        """Burn the third validator's whole attempt budget without a score.

        Reproduces the live blocker exactly: "not enough expired tickets to
        restore quorum" at 2-of-3.
        """
        session.add(
            ValidatorTicket(
                agent_id=agent_id,
                bench_version=_FROM_VERSION,
                validator_hotkey="5Validator-exhausted",
                status=TicketStatus.EXPIRED,
                issued_at=_ROLLOUT_START - timedelta(days=4),
                deadline=_ROLLOUT_START - timedelta(days=3),
                retry_after=_ROLLOUT_START - timedelta(days=3),
                attempt_count=MAX_ATTEMPTS_PER_VERSION,
                created_at=_ROLLOUT_START - timedelta(days=4),
            )
        )
        await session.flush()

    async def test_exhausted_excluded_by_default_included_when_asked(
        self, session: AsyncSession
    ) -> None:
        async with _seeding_the_retired_era(session):
            rollout = await _seed_rollout(session)
            exhausted = await _seed_stranded(session, name="exhausted")
            await self._exhaust(session, exhausted)

        async with session.begin():
            default = await stranded_prev_gen_candidates(
                session, rollout=rollout, settings=_ENABLED, now=_NOW
            )
            widened = await stranded_prev_gen_candidates(
                session,
                rollout=rollout,
                settings=PrevGenCarryoverSettings(enabled=True, include_exhausted=True),
                now=_NOW,
            )
        assert default == []
        assert [c.agent_id for c in widened] == [exhausted]


class TestDedupe:
    async def test_coldkey_scope_keeps_one_per_owner(
        self, session: AsyncSession
    ) -> None:
        """Two hotkeys, one coldkey, same age: the owner gets a single slot."""
        async with _seeding_the_retired_era(session):
            rollout = await _seed_rollout(session)
            first = await _seed_stranded(
                session, name="ck-a", hotkey="5HotkeyA", coldkey="5Coldkey"
            )
            second = await _seed_stranded(
                session, name="ck-b", hotkey="5HotkeyB", coldkey="5Coldkey"
            )

        async with session.begin():
            coldkey = await stranded_prev_gen_candidates(
                session, rollout=rollout, settings=_ENABLED, now=_NOW
            )
            hotkey = await stranded_prev_gen_candidates(
                session,
                rollout=rollout,
                settings=PrevGenCarryoverSettings(enabled=True, dedupe_scope="hotkey"),
                now=_NOW,
            )
        assert len(coldkey) == 1
        assert coldkey[0].agent_id in {first, second}
        assert coldkey[0].owner_key == "coldkey:5Coldkey"
        assert {c.agent_id for c in hotkey} == {first, second}
        assert {c.owner_key for c in hotkey} == {"hotkey:5HotkeyA", "hotkey:5HotkeyB"}

    async def test_owner_with_a_newer_live_submission_gets_none(
        self, session: AsyncSession
    ) -> None:
        """The miner moved on by their own choice, so the old work stands down."""
        async with _seeding_the_retired_era(session):
            rollout = await _seed_rollout(session)
            await _seed_stranded(
                session,
                name="old",
                hotkey="5HotkeyA",
                coldkey="5Coldkey",
                age_days=9,
            )
            await _seed_stranded(
                session,
                name="newer",
                hotkey="5HotkeyB",
                coldkey="5Coldkey",
                age_days=1,
                score_count=0,
            )

        async with session.begin():
            selected = await stranded_prev_gen_candidates(
                session,
                rollout=rollout,
                settings=PrevGenCarryoverSettings(enabled=True, min_score_count=2),
                now=_NOW,
            )
        assert selected == []

    async def test_a_newer_rejected_submission_does_not_suppress(
        self, session: AsyncSession
    ) -> None:
        """A dead newer submission is no evidence the miner moved on."""
        async with _seeding_the_retired_era(session):
            rollout = await _seed_rollout(session)
            old = await _seed_stranded(
                session,
                name="old",
                hotkey="5HotkeyA",
                coldkey="5Coldkey",
                age_days=9,
            )
            await _seed_stranded(
                session,
                name="dead",
                hotkey="5HotkeyB",
                coldkey="5Coldkey",
                age_days=1,
                score_count=0,
                status=AgentStatus.REJECTED,
            )

        async with session.begin():
            selected = await stranded_prev_gen_candidates(
                session,
                rollout=rollout,
                settings=PrevGenCarryoverSettings(enabled=True, min_score_count=2),
                now=_NOW,
            )
        assert [c.agent_id for c in selected] == [old]

    async def test_scope_none_disables_suppression(self, session: AsyncSession) -> None:
        async with _seeding_the_retired_era(session):
            rollout = await _seed_rollout(session)
            old = await _seed_stranded(
                session,
                name="old",
                hotkey="5HotkeyA",
                coldkey="5Coldkey",
                age_days=9,
            )
            newer = await _seed_stranded(
                session,
                name="newer",
                hotkey="5HotkeyB",
                coldkey="5Coldkey",
                age_days=1,
            )

        async with session.begin():
            selected = await stranded_prev_gen_candidates(
                session,
                rollout=rollout,
                settings=PrevGenCarryoverSettings(enabled=True, dedupe_scope="none"),
                now=_NOW,
            )
        assert {c.agent_id for c in selected} == {old, newer}

    async def test_an_owner_already_adopted_gets_no_second_slot(
        self, session: AsyncSession
    ) -> None:
        async with _seeding_the_retired_era(session):
            rollout = await _seed_rollout(session)
            first = await _seed_stranded(
                session, name="ck-a", hotkey="5HotkeyA", coldkey="5Coldkey"
            )
            await _seed_stranded(
                session, name="ck-b", hotkey="5HotkeyB", coldkey="5Coldkey"
            )
        async with session.begin():
            candidates = await stranded_prev_gen_candidates(
                session, rollout=rollout, settings=_ENABLED, now=_NOW
            )
            assert len(candidates) == 1
            await adopt_carryover_agent(
                session,
                rollout=rollout,
                candidate=candidates[0],
                dataset=DatasetPin(seed=7, sha256="ab" * 32, run_size="full"),
                now=_NOW,
            )

        async with session.begin():
            assert (
                await stranded_prev_gen_candidates(
                    session, rollout=rollout, settings=_ENABLED, now=_NOW
                )
                == []
            )
        assert first is not None


class TestCapAndOrdering:
    async def test_cap_keeps_the_highest_progress_then_fifo(
        self, session: AsyncSession
    ) -> None:
        async with _seeding_the_retired_era(session):
            rollout = await _seed_rollout(session)
            newer_two = await _seed_stranded(
                session, name="two-newer", score_count=2, age_days=3
            )
            older_two = await _seed_stranded(
                session, name="two-older", score_count=2, age_days=8
            )
            zero = await _seed_stranded(
                session, name="zero", score_count=0, age_days=20
            )

        async with session.begin():
            ordered = await stranded_prev_gen_candidates(
                session,
                rollout=rollout,
                settings=PrevGenCarryoverSettings(enabled=True, min_score_count=0),
                now=_NOW,
            )
            capped = await stranded_prev_gen_candidates(
                session,
                rollout=rollout,
                settings=PrevGenCarryoverSettings(
                    enabled=True, min_score_count=0, max_agents=2
                ),
                now=_NOW,
            )
        # Progress first, then FIFO: the 20-day-old 0-of-3 sorts behind both
        # 2-of-3 rows despite being the oldest submission on the list.
        assert [c.agent_id for c in ordered] == [older_two, newer_two, zero]
        assert [c.agent_id for c in capped] == [older_two, newer_two]

    async def test_already_adopted_rows_count_against_the_cap(
        self, session: AsyncSession
    ) -> None:
        async with _seeding_the_retired_era(session):
            rollout = await _seed_rollout(session)
            first = await _seed_stranded(session, name="first", age_days=9)
            await _seed_stranded(session, name="second", age_days=8)
        async with session.begin():
            assert await _adopt(session, rollout=rollout, agent_id=first)

        async with session.begin():
            assert (
                await stranded_prev_gen_candidates(
                    session,
                    rollout=rollout,
                    settings=PrevGenCarryoverSettings(enabled=True, max_agents=1),
                    now=_NOW,
                )
                == []
            )


class TestFreshLaneIsNotDiluted:
    async def test_the_fresh_lane_cannot_reach_an_adopted_agent(
        self, session: AsyncSession
    ) -> None:
        """The structural reason carryover cannot consume a fresh slot.

        The fresh-submission lane issues with
        ``submitted_at_or_after=rollout.created_at``, which filters on
        ``Agent.created_at``. Every carryover agent is by definition older than
        that, so the fresh lane cannot select one -- even fully admitted, fully
        datasetted, and named in the adopted set. ``only_agent_ids`` is the only
        thing that reaches it, and only the cohort-lane carryover path passes it.
        """
        async with _seeding_the_retired_era(session):
            rollout = await _seed_rollout(session)
            adopted = await _seed_stranded(session, name="stranded")
        async with session.begin():
            assert await _adopt(session, rollout=rollout, agent_id=adopted)

        async with session.begin():
            fresh = await issue_ticket(
                session,
                validator_hotkey="5Validator-fresh",
                now=_NOW,
                ttl=_TTL,
                bench_version=_DESIRED_VERSION,
                artifact_mode="screened_only",
                submitted_at_or_after=rollout.created_at,
                fifo_start_at=rollout.created_at,
                completion_first=True,
            )
        assert fresh is None

        async with session.begin():
            cohort_lane = await issue_ticket(
                session,
                validator_hotkey="5Validator-cohort",
                now=_NOW,
                ttl=_TTL,
                bench_version=_DESIRED_VERSION,
                artifact_mode="screened_only",
                only_agent_ids=[adopted],
            )
        assert cohort_lane is not None
        assert cohort_lane.agent_id == adopted

    async def test_only_agent_ids_does_not_widen_any_other_rule(
        self, session: AsyncSession
    ) -> None:
        """Naming an id is a narrowing, never a bypass."""
        async with _seeding_the_retired_era(session):
            await _seed_rollout(session)
            # Named, but never adopted: no desired-version dataset, so
            # ``issue_ticket``'s own hard requirement still refuses it.
            never_adopted = await _seed_stranded(session, name="not-adopted")

        async with session.begin():
            ticket = await issue_ticket(
                session,
                validator_hotkey="5Validator-new",
                now=_NOW,
                ttl=_TTL,
                bench_version=_DESIRED_VERSION,
                artifact_mode="screened_only",
                only_agent_ids=[never_adopted],
            )
        assert ticket is None

    async def test_default_call_shape_is_unchanged(self, session: AsyncSession) -> None:
        """Every existing caller passes no ``only_agent_ids`` and is unaffected."""
        async with _seeding_the_retired_era(session):
            rollout = await _seed_rollout(session)
            adopted = await _seed_stranded(session, name="stranded")
            fresh_agent = await _seed_stranded(
                session,
                name="fresh",
                score_count=0,
                created_at=_ROLLOUT_START + timedelta(hours=2),
            )
            session.add(
                BenchmarkDataset(
                    agent_id=fresh_agent,
                    bench_version=_DESIRED_VERSION,
                    seed=7,
                    sha256="ef" * 32,
                    run_size="full",
                    created_at=_NOW,
                )
            )
        async with session.begin():
            assert await _adopt(session, rollout=rollout, agent_id=adopted)

        async with session.begin():
            ticket = await issue_ticket(
                session,
                validator_hotkey="5Validator-fresh",
                now=_NOW,
                ttl=_TTL,
                bench_version=_DESIRED_VERSION,
                artifact_mode="screened_only",
                submitted_at_or_after=rollout.created_at,
                fifo_start_at=rollout.created_at,
                completion_first=True,
            )
        assert ticket is not None
        assert ticket.agent_id == fresh_agent
