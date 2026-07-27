"""Regressions for the platform-driven lease cancel (heartbeat protocol 17).

The behaviour under test: **a validator stops a run as soon as the platform's
authoritative roster stops listing its lease.** Before v17 an operator eviction
freed the platform-side slot instantly while the validator kept benchmarking to
completion on a lease it no longer held -- a full host slot burned for up to the
lease TTL to produce a score the platform then refused with a 409.

The other half of this file, and the half that matters more, is everything that
must *not* cancel. A false cancel destroys a healthy run that is minutes from a
verdict, so "the platform did not tell me" and "the platform told me I hold
nothing" are tested as separate states everywhere they can be confused.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from ditto.api_models.benchmark_capacity import ActiveBenchmarkSlot, BenchmarkCapacity
from ditto.api_models.validator import HeldLease, ValidatorHeartbeatResponse
from ditto.validator import worker as worker_module
from ditto.validator.build_info import HEARTBEAT_PROTOCOL_VERSION
from ditto.validator.errors import PlatformError
from ditto.validator.lease_roster import (
    RosterKnown,
    RosterUnknown,
    plan_cancellations,
    read_roster,
)


def _capacity(*slots: tuple[str, UUID]) -> BenchmarkCapacity:
    return BenchmarkCapacity(
        configured_slots=max(len(slots), 1),
        healthy_slots=[slot_id for slot_id, _ in slots] or ["slot-0"],
        admission="accepting",
        active=[
            ActiveBenchmarkSlot(
                slot_id=slot_id, agent_id=agent_id, bench_version=2, progress=None
            )
            for slot_id, agent_id in slots
        ],
    )


def _response(
    leases: list[HeldLease] | None, *, accepted: bool = True
) -> ValidatorHeartbeatResponse:
    return ValidatorHeartbeatResponse(
        accepted=accepted, seen_at=datetime.now(UTC), leases=leases
    )


def _lease(
    slot_id: str, agent_id: UUID, *, deadline: datetime | None = None
) -> HeldLease:
    return HeldLease(
        slot_id=slot_id,
        agent_id=agent_id,
        bench_version=2,
        deadline=deadline or datetime.now(UTC) + timedelta(hours=1),
    )


class TestReadingTheRoster:
    """``null`` and ``[]`` are different answers, in the types and not a comment."""

    def test_an_absent_field_is_unknown_and_carries_no_lease_information(self) -> None:
        roster = read_roster(_response(None))
        assert isinstance(roster, RosterUnknown)

    def test_an_empty_list_is_an_authoritative_you_hold_nothing(self) -> None:
        roster = read_roster(_response([]))
        assert roster == RosterKnown(frozenset())

    def test_a_populated_roster_keys_on_slot_and_agent(self) -> None:
        agent_id = uuid4()
        roster = read_roster(_response([_lease("slot-1", agent_id)]))
        assert roster == RosterKnown(frozenset({("slot-1", agent_id)}))


class TestPlanningCancellations:
    def test_a_listed_lease_is_never_cancelled(self) -> None:
        agent_id = uuid4()
        roster = read_roster(_response([_lease("slot-0", agent_id)]))
        assert (
            plan_cancellations(roster, advertised=_capacity(("slot-0", agent_id))) == ()
        )

    def test_an_omitted_lease_is_cancelled(self) -> None:
        running = uuid4()
        kept = uuid4()
        roster = read_roster(_response([_lease("slot-1", kept)]))
        advertised = _capacity(("slot-0", running), ("slot-1", kept))
        assert plan_cancellations(roster, advertised=advertised) == (
            ("slot-0", running),
        )

    def test_an_unknown_roster_cancels_nothing_even_with_work_advertised(self) -> None:
        """The single most important line in the feature.

        Absence of information must mean "keep running". A build that treated an
        unanswered heartbeat as an empty roster would kill every run on the fleet
        the first time the platform's query threw -- the false-idle verdict that
        ditto-platform #437, #443 and #496 were each written to undo.
        """
        agent_id = uuid4()
        roster = read_roster(_response(None))
        assert (
            plan_cancellations(roster, advertised=_capacity(("slot-0", agent_id))) == ()
        )

    def test_an_empty_roster_cancels_every_advertised_slot(self) -> None:
        """The shape of the 2026-07-27 incident: all leases evicted at once."""
        first, second = uuid4(), uuid4()
        roster = read_roster(_response([]))
        advertised = _capacity(("slot-0", first), ("slot-1", second))
        assert plan_cancellations(roster, advertised=advertised) == (
            ("slot-0", first),
            ("slot-1", second),
        )

    def test_a_drifted_deadline_on_the_same_lease_is_not_a_cancel(self) -> None:
        """The platform re-issues a lease in place; the reporter keeps the old
        deadline it was handed at claim time. That drift is ordinary and must
        never be able to authorize a kill, so the deadline is not an identity
        term -- which is the same line ``get_live_slot_ticket`` draws server-side.
        """
        agent_id = uuid4()
        renewed = _lease(
            "slot-0", agent_id, deadline=datetime.now(UTC) + timedelta(hours=3)
        )
        roster = read_roster(_response([renewed]))
        assert (
            plan_cancellations(roster, advertised=_capacity(("slot-0", agent_id))) == ()
        )

    def test_a_lease_the_reporter_never_advertised_is_not_cancellable(self) -> None:
        """The race guard, stated as a property.

        A slot claimed *after* this heartbeat was built cannot appear in
        ``advertised``, so this response can never cancel it -- no clock compared,
        no skew to get wrong. The platform read its ledger while handling the
        request, hence after every slot the request named had been claimed.
        """
        just_claimed = uuid4()
        roster = read_roster(_response([]))
        # Advertised nothing: the slot was claimed after the request was built.
        assert plan_cancellations(roster, advertised=_capacity()) == ()
        # The same empty roster does cancel the slot once it has been
        # advertised, so this is the ordering guard doing the work and not the
        # roster being toothless.
        assert plan_cancellations(
            roster, advertised=_capacity(("slot-0", just_claimed))
        ) == (("slot-0", just_claimed),)


def _worker_with_scorer(
    scorer: Any,
    *,
    heartbeat: Any,
    jobs: list[Any] | None = None,
) -> tuple[Any, MagicMock, MagicMock]:
    """A worker wired to ``scorer``, with a caller-controlled heartbeat."""
    from ditto.tests.validator.test_worker import (  # noqa: PLC0415
        _config,
        _job,
        _platform_with_ledger,
    )

    platform = _platform_with_ledger(
        jobs=jobs if jobs is not None else [_job("5MinerA" + "x" * 41)], ledger=[]
    )
    platform.submit_heartbeat = heartbeat

    dittobench = MagicMock()
    dittobench.preflight = AsyncMock()
    dittobench.last_details = {}
    dittobench.score_tarball = AsyncMock(side_effect=scorer)

    worker = worker_module.ValidatorWorker(
        config=_config(),
        platform=platform,
        dittobench=dittobench,
        chain=MagicMock(put_weights=AsyncMock()),
        keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
    )
    return worker, platform, dittobench


@pytest.fixture(autouse=True)
def _fast_heartbeat_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Match ``test_worker``: coalescing must not stall these sweeps."""
    from ditto.tests.validator.test_worker import (  # noqa: PLC0415
        _ImmediateHeartbeatClock,
    )

    monkeypatch.setattr(worker_module, "_new_heartbeat_clock", _ImmediateHeartbeatClock)


class _RosterHeartbeat:
    """Answer every heartbeat with a roster derived from what was advertised.

    ``evict`` names the agents the platform has stopped holding a lease for, so
    the double behaves exactly like the real endpoint: it echoes back the leases
    it still holds among those the reporter just claimed.
    """

    def __init__(self, *, evict: set[UUID] | None = None, leases: Any = ...) -> None:
        self._evict = evict or set()
        self._fixed = leases
        self.requests: list[Any] = []

    @property
    def mock(self) -> AsyncMock:
        """An ``AsyncMock`` the worker can await.

        Bound to ``respond`` rather than the instance: ``AsyncMock`` only awaits
        a ``side_effect`` that ``iscoroutinefunction`` recognises, and a callable
        object is not one.
        """
        return AsyncMock(side_effect=self.respond)

    async def respond(self, request: Any) -> ValidatorHeartbeatResponse:
        self.requests.append(request)
        if self._fixed is not ...:
            return _response(self._fixed)
        capacity = request.benchmark_capacity
        active = capacity.active if capacity is not None else []
        return _response(
            [
                _lease(slot.slot_id, slot.agent_id)
                for slot in active
                if slot.agent_id not in self._evict
            ]
        )


class TestTheWorkerActsOnTheRoster:
    async def test_an_evicted_lease_stops_the_run_and_frees_the_slot(self) -> None:
        """The whole point: the slot comes back now, not in ninety minutes.

        The scorer here never returns, exactly like the three production runs
        that were still benchmarking on revoked leases after the 2026-07-27
        evictions. Without the roster this sweep only ends when the lease
        deadline fires; with it, the first heartbeat that advertises the slot
        also brings back the answer that stops it.
        """
        from ditto.tests.validator.test_worker import _job  # noqa: PLC0415

        job = _job("5MinerA" + "x" * 41)

        async def hang(*_args: object, **_kwargs: object) -> Any:
            await asyncio.sleep(3600)

        # The platform already evicted this agent's lease before the sweep ran,
        # so every roster it answers with omits it.
        heartbeat = _RosterHeartbeat(evict={job.agent_id})
        worker, platform, _ = _worker_with_scorer(
            hang, heartbeat=heartbeat.mock, jobs=[job]
        )

        outcome = await asyncio.wait_for(worker.run_once(set_weights=False), timeout=20)

        # The lease was claimed and produced no score, which is the honest
        # accounting: the platform took the work back, so there is no verdict.
        assert outcome.queue_depth == 1
        # Nothing is reported back. The platform revoked the lease itself, so
        # `scoring_error` would consume an attempt it already took away and
        # `infrastructure` would mint a no-fault grant and re-lease forever.
        platform.report_ticket_failed.assert_not_awaited()
        platform.submit_score.assert_not_awaited()
        # The slot is genuinely free, not merely un-scored.
        assert all(slot.active_agent_id is None for slot in worker._slots.values())

    async def test_one_evicted_slot_does_not_stop_its_healthy_sibling(self) -> None:
        """Cancellation is per lease, never per validator.

        An operator evicting one submission must not take down the other runs on
        the same host, so the roster answer is applied slot by slot.
        """
        from ditto.tests.validator.test_worker import _job, _report  # noqa: PLC0415

        # Keyed on the lease, the way test_lease_deadline does: the scorer sees
        # ``ticket_deadline`` and nothing else that identifies which job it is.
        evicted_deadline = datetime.now(UTC) + timedelta(hours=2)
        evicted = _job("5MinerA" + "x" * 41, deadline=evicted_deadline)
        kept = _job("5MinerB" + "x" * 41)

        async def score(*_args: object, **kwargs: object) -> Any:
            if kwargs.get("ticket_deadline") == evicted_deadline:
                await asyncio.sleep(3600)
            return _report("run-kept", 0.72)

        heartbeat = _RosterHeartbeat(evict={evicted.agent_id})
        worker, platform, _ = _worker_with_scorer(
            score, heartbeat=heartbeat.mock, jobs=[evicted, kept]
        )

        await asyncio.wait_for(worker.run_once(set_weights=False), timeout=20)

        # The surviving lease still produced and submitted a score.
        platform.submit_score.assert_awaited_once()
        platform.report_ticket_failed.assert_not_awaited()

    async def test_a_held_lease_is_scored_normally(self) -> None:
        """The control. A roster that lists the lease changes nothing."""
        from ditto.tests.validator.test_worker import _report  # noqa: PLC0415

        async def score(*_args: object, **_kwargs: object) -> Any:
            return _report("run-ok", 0.81)

        heartbeat = _RosterHeartbeat()
        worker, platform, _ = _worker_with_scorer(score, heartbeat=heartbeat.mock)

        await asyncio.wait_for(worker.run_once(set_weights=False), timeout=20)

        platform.submit_score.assert_awaited()
        platform.report_ticket_failed.assert_not_awaited()

    async def test_a_failing_heartbeat_never_cancels_anything(self) -> None:
        """An unreachable platform is silence, and silence is not a verdict.

        This is structural rather than a rule to remember: the roster is only
        read from a response that exists, so a heartbeat that raised cannot
        reach the cancel path at all.
        """
        from ditto.tests.validator.test_worker import _report  # noqa: PLC0415

        async def score(*_args: object, **_kwargs: object) -> Any:
            return _report("run-ok", 0.77)

        worker, platform, _ = _worker_with_scorer(
            score, heartbeat=AsyncMock(side_effect=PlatformError("platform offline"))
        )

        await asyncio.wait_for(worker.run_once(set_weights=False), timeout=20)

        platform.submit_score.assert_awaited()
        platform.report_ticket_failed.assert_not_awaited()
        assert all(not slot.revoked.is_set() for slot in worker._slots.values())

    async def test_a_pre_v17_platform_leaves_behaviour_exactly_as_it_was(self) -> None:
        """Newer validator, older platform: the field is simply absent.

        ``ValidatorHeartbeatResponse`` does not forbid extras and defaults
        ``leases`` to ``None``, so a v17 build talking to a platform that has
        never heard of the roster reads "not answered" and cancels nothing. That
        is what lets this half ship first.
        """
        from ditto.tests.validator.test_worker import _report  # noqa: PLC0415

        async def score(*_args: object, **_kwargs: object) -> Any:
            return _report("run-ok", 0.66)

        legacy = AsyncMock(
            return_value=ValidatorHeartbeatResponse.model_validate(
                {"accepted": True, "seen_at": datetime.now(UTC).isoformat()}
            )
        )
        worker, platform, _ = _worker_with_scorer(score, heartbeat=legacy)

        await asyncio.wait_for(worker.run_once(set_weights=False), timeout=20)

        assert legacy.await_args_list[0].args[0].protocol_version == (
            HEARTBEAT_PROTOCOL_VERSION
        )
        platform.submit_score.assert_awaited()
        platform.report_ticket_failed.assert_not_awaited()
        assert all(not slot.revoked.is_set() for slot in worker._slots.values())

    async def test_a_run_that_finishes_before_the_cancel_lands_is_a_clean_no_op(
        self,
    ) -> None:
        """Losing the race is normal, not an error.

        Between building a heartbeat and reading its answer, the run it
        advertised can finish. The roster still names that slot as gone, and the
        right response is to do nothing: the score is already made, and if the
        lease really was revoked the platform refuses it with the clean 409
        ditto-platform#515 already guarantees. Nothing here may raise, cancel a
        successor, or turn the completion into a failure hand-back.
        """
        finished = uuid4()
        worker, _, _ = _worker_with_scorer(
            AsyncMock(), heartbeat=_RosterHeartbeat().mock
        )
        slot = worker._slots["slot-0"]
        # The run advertised in the request has since completed and cleared.
        assert slot.active_agent_id is None

        worker._apply_lease_roster(
            _response([]), advertised=_capacity(("slot-0", finished))
        )

        assert not slot.revoked.is_set()

    async def test_a_slot_that_moved_on_is_not_cancelled_by_the_old_answer(
        self,
    ) -> None:
        """The same race, one step later: the slot already took a new lease.

        Cancelling here would kill a run the platform does hold a lease for, so
        the live agent is re-checked against the roster's key before anything is
        stopped.
        """
        previous, current = uuid4(), uuid4()
        worker, _, _ = _worker_with_scorer(
            AsyncMock(), heartbeat=_RosterHeartbeat().mock
        )
        slot = worker._slots["slot-0"]
        slot.active_agent_id = current

        worker._apply_lease_roster(
            _response([_lease("slot-0", current)]),
            advertised=_capacity(("slot-0", previous)),
        )

        assert not slot.revoked.is_set()

    async def test_revocation_does_not_leak_into_the_next_lease(self) -> None:
        """A revocation belongs to the lease that provoked it.

        The flag is cleared as each lease starts scoring, so a slot whose
        previous lease was evicted is not born cancelled.
        """
        worker, _, _ = _worker_with_scorer(
            AsyncMock(), heartbeat=_RosterHeartbeat().mock
        )
        slot = worker._slots["slot-0"]
        slot.revoked.set()

        from ditto.tests.validator.test_worker import _job, _report  # noqa: PLC0415

        worker._dittobench.score_tarball = AsyncMock(
            return_value=_report("run-next", 0.5)
        )
        report = await asyncio.wait_for(
            worker._score_job_within_lease(_job("5MinerC" + "x" * 41)), timeout=20
        )

        assert report.composite == 0.5
        assert not slot.revoked.is_set()
