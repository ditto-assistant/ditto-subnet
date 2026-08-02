"""The retired-era floor, asserted at the storage layer.

Every test here writes through a real Postgres and checks the DATABASE's
answer, not the API's. That distinction is the point of the change: v2-v5 were
already unreachable through three forward-only application guards, and v6 was
unreachable only because a runtime setting shipped ``False``. Both are
statements about code that the next refactor can move -- and one of them
already had: the source-backfill resume path sat above the very gate that was
supposed to stop it, so v6 leases renewed themselves in production with no
rollout open and no flag set.

So these tests deliberately bypass the API and insert rows directly. If a
future change re-opens a retired era anywhere in the application, the ledger
still refuses to record it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.api_models.ticket_status import TicketPurpose, TicketStatus
from ditto.db.models import Agent, BenchmarkRollout, Score, ValidatorTicket
from ditto.db.queries.benchmark_rollout import MIN_SCOREABLE_BENCH_VERSION
from ditto.tests.legacy_era import retired_era_writes_allowed

_NOW = datetime(2026, 7, 28, 12, 0, 0, tzinfo=UTC)
_HOTKEY = "5Validator"


async def _seed_agent(session: AsyncSession) -> UUID:
    aid = uuid4()
    async with session.begin():
        session.add(
            Agent(
                agent_id=aid,
                miner_hotkey="5Miner",
                name=f"agent-{aid.hex[:8]}",
                sha256="ab" * 32,
                status=AgentStatus.SCORED,
                screening_policy_version=SCREENING_POLICY_VERSION,
                created_at=_NOW,
            )
        )
    return aid


def _score(agent_id: UUID, bench_version: int, *, hotkey: str = _HOTKEY) -> Score:
    return Score(
        agent_id=agent_id,
        bench_version=bench_version,
        validator_hotkey=hotkey,
        run_id=f"run-{bench_version}-{hotkey}",
        signature="aa",
        seed=8675309,
        composite=0.5,
        tool_mean=0.5,
        memory_mean=0.5,
        median_ms=100,
        n=114,
        details={"bench_version": bench_version},
        generated_at=_NOW,
    )


def _ticket(
    agent_id: UUID, bench_version: int, *, status: TicketStatus = TicketStatus.ISSUED
) -> ValidatorTicket:
    return ValidatorTicket(
        agent_id=agent_id,
        bench_version=bench_version,
        validator_hotkey=_HOTKEY,
        status=status,
        purpose=TicketPurpose.CANONICAL_QUORUM,
        purpose_revision=1,
        issued_at=_NOW,
        deadline=_NOW + timedelta(minutes=90),
        attempt_count=1,
    )


class TestTheLedgerRefusesARetiredEra:
    """A sub-v7 score cannot be stored, whatever asks for it."""

    @pytest.mark.parametrize("bench_version", [2, 3, 4, 5, 6])
    async def test_every_retired_version_is_refused(
        self, session: AsyncSession, bench_version: int
    ) -> None:
        """Not just v6. The floor is a floor, not a patch for one version."""
        agent_id = await _seed_agent(session)
        with pytest.raises(IntegrityError, match="scores_bench_version_floor"):
            async with session.begin():
                session.add(_score(agent_id, bench_version))

    async def test_the_active_era_is_unaffected(self, session: AsyncSession) -> None:
        agent_id = await _seed_agent(session)
        async with session.begin():
            session.add(_score(agent_id, MIN_SCOREABLE_BENCH_VERSION))
        stored = await session.get(
            Score, (agent_id, MIN_SCOREABLE_BENCH_VERSION, _HOTKEY)
        )
        assert stored is not None

    async def test_a_retired_score_cannot_be_rewritten_either(
        self, session: AsyncSession
    ) -> None:
        """UPDATE is checked too, and that is deliberate.

        ``NOT VALID`` skips the existing-row SCAN, not the per-row check on
        write. A historical v6 row stays exactly as it is and stays readable,
        but nothing can rewrite it -- so the retired ledger is frozen, not just
        closed to new entries.
        """
        agent_id = await _seed_agent(session)
        async with retired_era_writes_allowed(session), session.begin():
            session.add(_score(agent_id, 6))

        stored = await session.get(Score, (agent_id, 6, _HOTKEY))
        assert stored is not None
        # ``get`` autobegins a read transaction; close it so the write below
        # opens its own.
        await session.commit()
        with pytest.raises(IntegrityError, match="scores_bench_version_floor"):
            async with session.begin():
                stored.composite = 0.9


class TestHistoryStaysIntact:
    """The floor blocks new writes. It is not a purge."""

    async def test_existing_retired_rows_remain_readable_and_queryable(
        self, session: AsyncSession
    ) -> None:
        """This is the production shape: rows that predate the constraint.

        They are grandfathered by ``NOT VALID`` exactly as the 1,685 real
        sub-v7 ``scores`` rows are, and they must still select, filter and
        aggregate normally afterwards.
        """
        agent_id = await _seed_agent(session)
        async with retired_era_writes_allowed(session), session.begin():
            for version in (2, 3, 4, 5, 6):
                session.add(_score(agent_id, version, hotkey=f"{_HOTKEY}-{version}"))

        rows = (
            await session.scalars(
                select(Score)
                .where(Score.agent_id == agent_id)
                .order_by(Score.bench_version)
            )
        ).all()
        assert [row.bench_version for row in rows] == [2, 3, 4, 5, 6]
        assert all(row.composite == 0.5 for row in rows)
        await session.commit()

        # And the floor is genuinely back on afterwards -- the helper restores
        # it, so a test using legacy rows cannot silently disarm the next one.
        with pytest.raises(IntegrityError, match="scores_bench_version_floor"):
            async with session.begin():
                session.add(_score(agent_id, 6, hotkey="5Another"))

    async def test_the_constraint_is_not_valid_so_it_never_scanned(
        self, session: AsyncSession
    ) -> None:
        """Proves the migration stayed metadata-only.

        A VALIDATED constraint would have had to read every row in ``scores``
        to be created, which on a hot table is the shape that deadlocked #481.
        ``convalidated = false`` is the evidence it did not.
        """
        validated = await session.scalar(
            text(
                "SELECT convalidated FROM pg_constraint "
                "WHERE conname = 'scores_bench_version_floor'"
            )
        )
        assert validated is False


class TestNoRetiredEraLease:
    """Admission, not just recording."""

    async def test_a_retired_ticket_cannot_be_created(
        self, session: AsyncSession
    ) -> None:
        agent_id = await _seed_agent(session)
        with pytest.raises(IntegrityError, match="retired and cannot be leased"):
            async with session.begin():
                session.add(_ticket(agent_id, 6))

    async def test_an_inflight_retired_lease_can_still_drain(
        self, session: AsyncSession
    ) -> None:
        """The in-flight case, and the reason this is a trigger not a CHECK.

        A lease that was already live when the floor landed has to be able to
        reach a terminal state. A CHECK would refuse the ``issued -> expired``
        UPDATE and strand it in ``issued`` forever, holding a fleet slot with
        no way to close it. The trigger permits the drain and refuses only the
        re-lease.
        """
        agent_id = await _seed_agent(session)
        async with retired_era_writes_allowed(session), session.begin():
            session.add(_ticket(agent_id, 6))

        ticket = await session.get(ValidatorTicket, (agent_id, 6, _HOTKEY))
        assert ticket is not None
        await session.commit()
        async with session.begin():
            ticket.status = TicketStatus.EXPIRED
            ticket.deadline = _NOW
            ticket.retry_after = _NOW
        await session.refresh(ticket)
        assert ticket.status == TicketStatus.EXPIRED

    async def test_a_drained_retired_lease_cannot_be_re_issued(
        self, session: AsyncSession
    ) -> None:
        """The UPDATE-reissue bypass, closed.

        Re-leasing is an UPDATE, so an INSERT-only guard would have let
        ``replace_validator_score_after_infrastructure_failure`` and the
        score-retest queue hand a retired era another 90-minute lease straight
        from Backroom. Both are UPDATEs of exactly this shape.
        """
        agent_id = await _seed_agent(session)
        async with retired_era_writes_allowed(session), session.begin():
            session.add(_ticket(agent_id, 6, status=TicketStatus.SCORED))

        ticket = await session.get(ValidatorTicket, (agent_id, 6, _HOTKEY))
        assert ticket is not None
        await session.commit()
        with pytest.raises(IntegrityError, match="retired and cannot be leased"):
            async with session.begin():
                ticket.status = TicketStatus.ISSUED
                ticket.deadline = _NOW + timedelta(minutes=90)

    async def test_a_live_retired_lease_cannot_be_renewed_in_place(
        self, session: AsyncSession
    ) -> None:
        """The renewal case, which is the one that actually happened.

        ``issue_ticket``'s reuse branch does not resurrect an expired row here
        -- it writes ``status = ISSUED`` over a row that is ALREADY issued and
        pushes ``deadline`` out. That is how the source-backfill resume path
        kept a v6 lease alive indefinitely in production: the status never
        changed, so there was no transition for a status-watching guard to see.

        A guard keyed on ``OLD.status <> 'issued'`` would pass this test's
        setup and then let the renewal through, reproducing the original hole
        in the schema. The floor keys on the LEASE moving instead -- a
        refreshed ``issued_at`` or a deadline pushed forward -- so an
        indefinitely renewing retired lease is refused.
        """
        agent_id = await _seed_agent(session)
        async with retired_era_writes_allowed(session), session.begin():
            session.add(_ticket(agent_id, 6))

        ticket = await session.get(ValidatorTicket, (agent_id, 6, _HOTKEY))
        assert ticket is not None
        assert ticket.status == TicketStatus.ISSUED
        await session.commit()
        with pytest.raises(IntegrityError, match="retired and cannot be leased"):
            async with session.begin():
                # Exactly what the reuse branch writes: same status, fresh
                # stamp, deadline pushed out.
                ticket.issued_at = _NOW + timedelta(minutes=1)
                ticket.deadline = _NOW + timedelta(minutes=91)

    async def test_a_live_retired_lease_can_still_record_a_failure(
        self, session: AsyncSession
    ) -> None:
        """The drain must survive the tighter guard.

        Tightening the lease arm risks catching the bookkeeping a live lease
        does on its way to a terminal state. Reporting a failure leaves the row
        ``issued`` and touches neither ``issued_at`` nor ``deadline``, so it
        must still commit.
        """
        agent_id = await _seed_agent(session)
        async with retired_era_writes_allowed(session), session.begin():
            session.add(_ticket(agent_id, 6))

        ticket = await session.get(ValidatorTicket, (agent_id, 6, _HOTKEY))
        assert ticket is not None
        await session.commit()
        async with session.begin():
            ticket.failure_reason = "scoring_error"
            ticket.first_reported_at = _NOW
        await session.refresh(ticket)
        assert ticket.failure_reason == "scoring_error"


class TestNoRolloutIntoARetiredEra:
    async def test_a_rollout_cannot_target_a_retired_version(
        self, session: AsyncSession
    ) -> None:
        with pytest.raises(IntegrityError, match="benchmark_rollout_desired_floor"):
            async with session.begin():
                session.add(
                    BenchmarkRollout(
                        rollout_id=uuid4(),
                        from_version=4,
                        desired_version=5,
                        status="collecting",
                        cohort_size=5,
                        created_at=_NOW,
                    )
                )

    async def test_a_rollout_into_the_live_era_is_unaffected(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            session.add(
                BenchmarkRollout(
                    rollout_id=uuid4(),
                    from_version=MIN_SCOREABLE_BENCH_VERSION,
                    desired_version=MIN_SCOREABLE_BENCH_VERSION + 1,
                    status="collecting",
                    cohort_size=5,
                    created_at=_NOW,
                )
            )
