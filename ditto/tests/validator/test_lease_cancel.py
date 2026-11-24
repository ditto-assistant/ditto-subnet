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
from ditto.validator.errors import PlatformError, PlatformInfrastructureError
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


def _retest_job(*, slot_id: str = "slot-0") -> Any:
    """A continual-retest (top-five confirmation) lease with one pinned seed."""
    from ditto.tests.validator.test_worker import (  # noqa: PLC0415
        _confirmation_pins,
        _job,
    )

    job = _job("5MinerR" + "x" * 41, slot_id=slot_id)
    return job.model_copy(
        update={
            "confirmation_datasets": _confirmation_pins(job.agent_id, count=1),
            "deadline": datetime.now(UTC) + timedelta(hours=3),
        }
    )


class TestTheRetestLaneActsOnTheRoster:
    """The continual-retest lane honours revocation exactly like canonical work.

    Before this, ``_run_top5_confirmation_lane`` ran its evaluation with no
    revocation watchdog. An operator reject that force-expired a retest lease
    set ``slot.revoked`` and nothing listened: the run went on until its own
    poll budget (hours), the sweep's ``asyncio.gather`` never returned, and once
    an updater drain landed every idle sibling slot had stopped polling -- a
    whole cohort issued nothing while heartbeats still said ``accepting``.
    """

    async def test_a_revoked_retest_lease_cancels_its_run_and_frees_the_slot(
        self,
    ) -> None:
        job = _retest_job()
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def hang(*_args: object, **_kwargs: object) -> Any:
            started.set()
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                # Where ``DittobenchClient._poll`` kills the scorer container.
                cancelled.set()
                raise

        worker, platform, _ = _worker_with_scorer(
            AsyncMock(), heartbeat=_RosterHeartbeat().mock, jobs=[]
        )
        platform.request_top5_confirmation_job = AsyncMock(side_effect=[job, None])
        worker._evaluate = AsyncMock(side_effect=hang)  # type: ignore[method-assign]

        lane = asyncio.create_task(worker._run_top5_confirmation_lane())
        await asyncio.wait_for(started.wait(), timeout=20)
        assert worker._slots[job.slot_id].active_agent_id == job.agent_id

        # The platform force-expired the lease: its roster now omits it.
        worker._apply_lease_roster(
            _response([]), advertised=worker._capacity_snapshot()
        )

        claimed = await asyncio.wait_for(lane, timeout=20)

        # The lease was claimed, so the sweep polls once more for real work.
        assert claimed is True
        assert cancelled.is_set()
        # Silence on the wire, exactly like a revoked canonical lease.
        platform.report_ticket_failed.assert_not_awaited()
        platform.submit_top5_confirmation_score.assert_not_awaited()
        assert all(slot.active_agent_id is None for slot in worker._slots.values())

    async def test_a_revoked_retest_lease_lets_the_sweep_end(self) -> None:
        """The incident shape: the sweep must not be held by a revoked retest.

        The canonical queue is empty, so the sweep dispatches the retest lane,
        which claims a lease whose run never returns on its own. The roster
        answering the run's own heartbeat omits that lease, and the whole sweep
        completes promptly instead of at the poll budget.
        """
        job = _retest_job()
        heartbeat = _RosterHeartbeat(evict={job.agent_id})
        worker, platform, _ = _worker_with_scorer(
            AsyncMock(), heartbeat=heartbeat.mock, jobs=[]
        )
        platform.request_job = AsyncMock(return_value=None)
        platform.request_top5_confirmation_job = AsyncMock(side_effect=[job, None])

        async def advertise_then_hang(*_args: object, **_kwargs: object) -> Any:
            # Stand in for the active-run heartbeat a real scorer emits.
            await worker._report_heartbeat("running_benchmark")
            await asyncio.sleep(3600)

        worker._evaluate = AsyncMock(  # type: ignore[method-assign]
            side_effect=advertise_then_hang
        )

        await asyncio.wait_for(worker.run_once(set_weights=False), timeout=20)

        # The roster really did see (and drop) the retest lease.
        assert any(
            slot.agent_id == job.agent_id
            for request in heartbeat.requests
            if request.benchmark_capacity is not None
            for slot in request.benchmark_capacity.active
        )
        platform.report_ticket_failed.assert_not_awaited()
        platform.submit_top5_confirmation_score.assert_not_awaited()
        assert all(slot.active_agent_id is None for slot in worker._slots.values())

    async def test_a_held_retest_lease_is_submitted_normally(self) -> None:
        """The control: a roster that lists the retest lease changes nothing."""
        from ditto.tests.validator.test_worker import _report  # noqa: PLC0415

        job = _retest_job()
        seed = job.confirmation_datasets[0].seed
        worker, platform, _ = _worker_with_scorer(
            AsyncMock(), heartbeat=_RosterHeartbeat().mock, jobs=[]
        )
        platform.request_top5_confirmation_job = AsyncMock(side_effect=[job, None])

        async def evaluate(*_args: object, **_kwargs: object) -> Any:
            await worker._report_heartbeat("running_benchmark")
            return _report("run-retest", 0.7).model_copy(update={"seed": seed})

        worker._evaluate = AsyncMock(side_effect=evaluate)  # type: ignore[method-assign]

        assert await asyncio.wait_for(worker._run_top5_confirmation_lane(), 20)

        platform.submit_top5_confirmation_score.assert_awaited_once()
        platform.report_ticket_failed.assert_not_awaited()
        assert not worker._slots[job.slot_id].revoked.is_set()

    async def test_a_roster_drop_during_the_retest_submission_cancels_nothing(
        self,
    ) -> None:
        """The lane submits after ``_run_within_lease`` returns.

        Its watchdog is already cancelled and the lease timeout has exited, so
        a roster that omits the lease while ``submit_top5_confirmation_score``
        is in flight sets ``revoked`` and nothing acts on it.
        """
        from ditto.tests.validator.test_worker import _report  # noqa: PLC0415

        job = _retest_job()
        seed = job.confirmation_datasets[0].seed
        worker, platform, _ = _worker_with_scorer(
            AsyncMock(), heartbeat=_RosterHeartbeat().mock, jobs=[]
        )
        platform.request_top5_confirmation_job = AsyncMock(side_effect=[job, None])
        worker._evaluate = AsyncMock(  # type: ignore[method-assign]
            return_value=_report("run-retest", 0.7).model_copy(update={"seed": seed})
        )
        accepted: list[UUID] = []

        async def dropped_mid_submission(agent_id: UUID, **_kwargs: object) -> None:
            worker._apply_lease_roster(
                _response([]), advertised=worker._capacity_snapshot()
            )
            await asyncio.sleep(0.1)
            accepted.append(agent_id)

        platform.submit_top5_confirmation_score = AsyncMock(
            side_effect=dropped_mid_submission
        )

        assert await asyncio.wait_for(worker._run_top5_confirmation_lane(), 20)

        assert worker._slots[job.slot_id].revoked.is_set()
        assert accepted == [job.agent_id]
        platform.report_ticket_failed.assert_not_awaited()
        assert all(slot.active_agent_id is None for slot in worker._slots.values())

    async def test_a_stale_revocation_does_not_cancel_the_next_retest(self) -> None:
        """A revocation left over from a previous lease is not inherited."""
        from ditto.tests.validator.test_worker import _report  # noqa: PLC0415

        job = _retest_job()
        seed = job.confirmation_datasets[0].seed
        worker, platform, _ = _worker_with_scorer(
            AsyncMock(), heartbeat=_RosterHeartbeat().mock, jobs=[]
        )
        worker._slots[job.slot_id].revoked.set()
        platform.request_top5_confirmation_job = AsyncMock(side_effect=[job, None])
        worker._evaluate = AsyncMock(  # type: ignore[method-assign]
            return_value=_report("run-retest", 0.7).model_copy(update={"seed": seed})
        )

        assert await asyncio.wait_for(worker._run_top5_confirmation_lane(), 20)

        platform.submit_top5_confirmation_score.assert_awaited_once()
        platform.report_ticket_failed.assert_not_awaited()


class TestDrainAdvertisesImmediately:
    async def test_a_drain_requested_mid_sweep_stops_advertising_accepting(
        self,
    ) -> None:
        """Heartbeats say ``draining`` as soon as the updater asks.

        Idle slots stop polling the moment a drain is requested, so a sweep
        held open by a long run must not keep advertising ``accepting``.
        """
        heartbeat = _RosterHeartbeat()
        worker, _, _ = _worker_with_scorer(AsyncMock(), heartbeat=heartbeat.mock)
        drain = asyncio.Event()
        worker._drain_requested = drain

        await worker._report_heartbeat("polling")
        assert heartbeat.requests[-1].benchmark_capacity.admission == "accepting"

        drain.set()
        await worker._report_heartbeat("running_benchmark")

        capacity = heartbeat.requests[-1].benchmark_capacity
        assert capacity.admission == "draining"
        assert capacity.healthy_slots == []

    async def test_a_remembered_drain_blocks_new_work_in_a_later_sweep(
        self,
    ) -> None:
        """A sweep called without an event gates on the one it remembered.

        The heartbeat reads the remembered event, so the slot loop must too;
        otherwise the validator says ``draining`` while still claiming work.
        """
        heartbeat = _RosterHeartbeat()
        scorer = AsyncMock()
        worker, _, dittobench = _worker_with_scorer(scorer, heartbeat=heartbeat.mock)
        drain = asyncio.Event()
        drain.set()
        worker._drain_requested = drain

        await asyncio.wait_for(worker.run_once(set_weights=False), timeout=20)

        dittobench.score_tarball.assert_not_awaited()
        assert heartbeat.requests[-1].benchmark_capacity.admission == "draining"

    async def test_a_withdrawn_drain_stops_advertising_draining(self) -> None:
        """Clearing the event mid-sweep restores ``accepting``."""
        heartbeat = _RosterHeartbeat()
        worker, _, _ = _worker_with_scorer(AsyncMock(), heartbeat=heartbeat.mock)
        drain = asyncio.Event()
        worker._drain_requested = drain

        drain.set()
        await worker._report_heartbeat("running_benchmark")
        assert heartbeat.requests[-1].benchmark_capacity.admission == "draining"

        drain.clear()
        await worker._report_heartbeat("running_benchmark")
        assert heartbeat.requests[-1].benchmark_capacity.admission == "accepting"


def _accepting_worker(
    *,
    upload: Any,
    job: Any,
) -> tuple[Any, MagicMock, _RosterHeartbeat]:
    """A canonical lease whose score Platform accepts, then drops from the roster.

    ``submit_score`` models the real endpoint: the ticket becomes ``SCORED`` and
    every roster answered afterwards omits it, exactly as
    ``tickets.py``'s ``ISSUED``-only roster query does. ``upload`` stands in for
    ``PlatformClient.submit_transcript``.
    """
    from ditto.tests.validator.test_worker import _report  # noqa: PLC0415

    report = _report("run-accepted", 0.8).model_copy(
        update={"details": {"transcript_sha256": "ee" * 32}}
    )

    async def score(*_args: object, **_kwargs: object) -> Any:
        return report

    heartbeat = _RosterHeartbeat()
    worker, platform, dittobench = _worker_with_scorer(
        score, heartbeat=heartbeat.mock, jobs=[job]
    )
    dittobench.take_transcript = MagicMock(return_value=b'{"cases":[]}')
    dittobench.take_harness_memory = MagicMock(return_value=None)

    async def accept(agent_id: UUID, **_kwargs: object) -> None:
        heartbeat._evict.add(agent_id)

    platform.submit_score = AsyncMock(side_effect=accept)
    platform.submit_transcript = AsyncMock(side_effect=upload)
    return worker, platform, heartbeat


class TestAnAcceptedScoreIsNotRevoked:
    """#2966: once Platform accepts the score, the lease is spent, not revoked.

    ``submit_score`` marks the ticket ``SCORED`` and the roster drops it at
    once, while the slot is still uploading the signed transcript. A heartbeat
    in that window -- the slot's own 10 s tick or any sibling slot's, since
    every heartbeat carries every slot -- must not cancel the upload as a
    revocation, and the lease budget must not turn a slow upload into a
    ``scoring_error`` hand-back for a ticket that is already scored.
    """

    async def test_a_heartbeat_during_the_transcript_upload_does_not_revoke(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from ditto.tests.validator.test_worker import _job  # noqa: PLC0415

        job = _job("5MinerA" + "x" * 41)
        finished: list[str] = []
        worker: Any = None

        async def upload(*_args: object, run_id: str, **_kw: object) -> None:
            # The roster answering this heartbeat no longer lists the lease.
            await worker._report_heartbeat("running_benchmark")
            await asyncio.sleep(0.05)
            finished.append(run_id)

        worker, platform, heartbeat = _accepting_worker(upload=upload, job=job)
        after_score = MagicMock()
        worker._after_score = after_score

        with caplog.at_level("WARNING"):
            await asyncio.wait_for(worker.run_once(set_weights=False), timeout=20)

        # The heartbeat really did advertise the slot after acceptance.
        assert job.agent_id in heartbeat._evict
        platform.submit_score.assert_awaited_once()
        assert finished == ["run-accepted"]
        after_score.assert_called_once_with(job.agent_id, 8)
        platform.report_ticket_failed.assert_not_awaited()
        assert "revoked by the platform" not in caplog.text
        assert all(slot.active_agent_id is None for slot in worker._slots.values())

    async def test_a_roster_read_while_the_accept_is_in_flight_does_not_revoke(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Platform commits ``SCORED`` before ``submit_score``'s answer arrives.

        A sibling heartbeat answered in that gap omits the lease while the slot
        is not yet marked accepted, so it does set ``revoked``. The watchdog
        must still let the accepted score's transcript finish.
        """
        from ditto.tests.validator.test_worker import _job  # noqa: PLC0415

        job = _job("5MinerA" + "x" * 41)
        finished: list[str] = []
        worker: Any = None

        async def upload(*_args: object, run_id: str, **_kw: object) -> None:
            await asyncio.sleep(0.05)
            finished.append(run_id)

        worker, platform, heartbeat = _accepting_worker(upload=upload, job=job)

        async def accept_then_heartbeat(agent_id: UUID, **_kwargs: object) -> None:
            heartbeat._evict.add(agent_id)
            await worker._report_heartbeat("running_benchmark")

        platform.submit_score = AsyncMock(side_effect=accept_then_heartbeat)

        with caplog.at_level("WARNING"):
            await asyncio.wait_for(worker.run_once(set_weights=False), timeout=20)

        assert finished == ["run-accepted"]
        platform.report_ticket_failed.assert_not_awaited()
        assert "revoked by the platform" not in caplog.text

    async def test_a_refused_submission_on_a_revoked_lease_hands_nothing_back(
        self,
    ) -> None:
        """The other answer to the same race: Platform really took the lease.

        The submission is not cancelled, so Platform refuses it with a 409. The
        roster already said the lease was gone, so this is the revocation it
        was -- silence on the wire, never a ``scoring_error`` that would spend
        an attempt Platform already withdrew.
        """
        from ditto.tests.validator.test_worker import _job  # noqa: PLC0415

        job = _job("5MinerA" + "x" * 41)
        worker: Any = None

        async def upload(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("a refused score has no transcript to publish")

        worker, platform, heartbeat = _accepting_worker(upload=upload, job=job)

        async def revoked_then_refused(agent_id: UUID, **_kwargs: object) -> None:
            heartbeat._evict.add(agent_id)
            await worker._report_heartbeat("running_benchmark")
            raise PlatformError("score rejected (409): no live lease")

        platform.submit_score = AsyncMock(side_effect=revoked_then_refused)
        stages: list[str] = []
        publish = worker._publish_benchmark_progress

        async def record_stage(stage: str, **kwargs: Any) -> bool:
            stages.append(stage)
            return bool(await publish(stage, **kwargs))

        worker._publish_benchmark_progress = record_stage

        await asyncio.wait_for(worker.run_once(set_weights=False), timeout=20)

        platform.submit_score.assert_awaited_once()
        platform.submit_transcript.assert_not_awaited()
        platform.report_ticket_failed.assert_not_awaited()
        # Exactly the wire of a watchdog revocation: no failure progress either.
        assert "submitting_result" in stages
        assert "failed_retrying" not in stages
        assert all(slot.active_agent_id is None for slot in worker._slots.values())

    async def test_a_refused_submission_on_a_held_lease_is_still_handed_back(
        self,
    ) -> None:
        """The control: without a revocation, a refusal is a scoring failure."""
        from ditto.tests.validator.test_worker import _job  # noqa: PLC0415

        job = _job("5MinerA" + "x" * 41)

        async def upload(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("a refused score has no transcript to publish")

        worker, platform, _ = _accepting_worker(upload=upload, job=job)
        platform.submit_score = AsyncMock(
            side_effect=PlatformError("score rejected (422): bad signature")
        )

        await asyncio.wait_for(worker.run_once(set_weights=False), timeout=20)

        platform.report_ticket_failed.assert_awaited_once()
        assert platform.report_ticket_failed.await_args.args[1] == "scoring_error"

    async def test_the_lease_deadline_during_the_upload_hands_nothing_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ditto.tests.validator.test_worker import _job  # noqa: PLC0415

        # Half a second of lease budget: scoring and submission fit, a slow
        # transcript upload does not.
        monkeypatch.setattr(worker_module, "lease_budget_seconds", lambda *_a: 0.5)
        job = _job("5MinerA" + "x" * 41)
        finished: list[str] = []

        async def slow_upload(*_args: object, run_id: str, **_kw: object) -> None:
            await asyncio.sleep(1.0)
            finished.append(run_id)

        worker, platform, _ = _accepting_worker(upload=slow_upload, job=job)

        await asyncio.wait_for(worker.run_once(set_weights=False), timeout=20)

        platform.submit_score.assert_awaited_once()
        assert finished == ["run-accepted"]
        platform.report_ticket_failed.assert_not_awaited()

    async def test_a_hung_upload_is_bounded_without_a_hand_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Post-acceptance work has its own bound, so a hung upload frees the slot."""
        from ditto.tests.validator.test_worker import _job  # noqa: PLC0415

        monkeypatch.setattr(worker_module, "_POST_SCORE_TIMEOUT_SECONDS", 0.2)
        job = _job("5MinerA" + "x" * 41)

        async def hang(*_args: object, **_kwargs: object) -> None:
            await asyncio.sleep(3600)

        worker, platform, _ = _accepting_worker(upload=hang, job=job)
        after_score = MagicMock()
        worker._after_score = after_score

        await asyncio.wait_for(worker.run_once(set_weights=False), timeout=20)

        platform.submit_score.assert_awaited_once()
        platform.report_ticket_failed.assert_not_awaited()
        # The canary offer still follows the accepted score.
        after_score.assert_called_once_with(job.agent_id, 8)
        assert all(slot.active_agent_id is None for slot in worker._slots.values())

    async def test_the_run_budget_cannot_cancel_an_in_flight_submission(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The run budget ends a margin early; the ticket deadline bounds submit.

        Platform commits the score, but its answer arrives after the run budget
        (deadline minus the reporting margin) has run out. Cancelling the
        request there would lose the transcript and hand back ``scoring_error``
        for a ticket that is already scored.
        """
        from ditto.tests.validator.test_worker import _job  # noqa: PLC0415

        monkeypatch.setattr(worker_module, "lease_budget_seconds", lambda *_a: 0.3)
        job = _job("5MinerA" + "x" * 41)
        finished: list[str] = []

        async def upload(*_args: object, run_id: str, **_kw: object) -> None:
            finished.append(run_id)

        worker, platform, heartbeat = _accepting_worker(upload=upload, job=job)

        async def slow_accept(agent_id: UUID, **_kwargs: object) -> None:
            heartbeat._evict.add(agent_id)
            await asyncio.sleep(0.6)

        platform.submit_score = AsyncMock(side_effect=slow_accept)

        await asyncio.wait_for(worker.run_once(set_weights=False), timeout=20)

        platform.submit_score.assert_awaited_once()
        assert finished == ["run-accepted"]
        platform.report_ticket_failed.assert_not_awaited()

    async def test_the_ticket_deadline_still_bounds_a_hung_submission(self) -> None:
        """Lifting the run budget at submission keeps the deadline as a bound."""
        worker, _, _ = _worker_with_scorer(
            AsyncMock(), heartbeat=_RosterHeartbeat().mock
        )
        slot = worker._slots["slot-0"]
        loop = asyncio.get_running_loop()
        deadline = datetime.now(UTC) + timedelta(seconds=0.4)
        async with asyncio.timeout(0.1) as run_budget:
            slot.lease_timeout = run_budget
            worker._bound_submission_by_ticket_deadline(slot, deadline)
            when = run_budget.when()
            assert when is not None
            assert 0.3 < when - loop.time() <= 0.4
        slot.lease_timeout = None

    async def test_an_outer_cancel_after_a_mid_submission_revocation_propagates(
        self,
    ) -> None:
        """Only the watchdog's own cancel is a revocation.

        The roster dropped the lease while the score was being submitted, so
        ``revoked`` is set without a cancel, and Platform accepted the score.
        A cancel from an enclosing scope during the upload (shutdown, an outer
        ``wait_for``) must reach the caller, not be swallowed as a revocation
        that lets the slot go on claiming leases.
        """
        from ditto.tests.validator.test_worker import _job  # noqa: PLC0415

        job = _job("5MinerA" + "x" * 41)
        worker: Any = None

        async def upload(*_args: object, **_kwargs: object) -> None:
            current = asyncio.current_task()
            assert current is not None
            current.cancel()
            await asyncio.sleep(1)

        worker, platform, heartbeat = _accepting_worker(upload=upload, job=job)

        async def revoked_then_accepted(agent_id: UUID, **_kwargs: object) -> None:
            heartbeat._evict.add(agent_id)
            await worker._report_heartbeat("running_benchmark")

        platform.submit_score = AsyncMock(side_effect=revoked_then_accepted)

        run = asyncio.create_task(worker.run_once(set_weights=False))
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(run, timeout=20)
        platform.report_ticket_failed.assert_not_awaited()

    @pytest.mark.parametrize(
        "failure",
        [
            PlatformInfrastructureError("score submit failed: ReadTimeout"),
            PlatformInfrastructureError("score rejected (503): unavailable"),
        ],
        ids=["transport", "5xx"],
    )
    async def test_a_transient_failure_on_a_dropped_lease_is_still_handed_back(
        self, failure: Exception
    ) -> None:
        """Only a definitive refusal is read as the revocation.

        A transport/5xx failure that outlasted ``submit_score``'s retries may
        follow a score Platform committed, and an accepted score drops the lease
        from the roster too. Such a failure says nothing about why the lease is
        gone, so it keeps its ``infrastructure`` hand-back; ``fail_job`` charges
        nothing when no open ticket remains.
        """
        from ditto.tests.validator.test_worker import _job  # noqa: PLC0415

        job = _job("5MinerA" + "x" * 41)
        worker: Any = None

        async def upload(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("a failed submission has no transcript to publish")

        worker, platform, heartbeat = _accepting_worker(upload=upload, job=job)

        async def dropped_then_failed(agent_id: UUID, **_kwargs: object) -> None:
            heartbeat._evict.add(agent_id)
            await worker._report_heartbeat("running_benchmark")
            raise failure

        platform.submit_score = AsyncMock(side_effect=dropped_then_failed)

        await asyncio.wait_for(worker.run_once(set_weights=False), timeout=20)

        platform.submit_score.assert_awaited_once()
        assert worker._slots["slot-0"].revoked.is_set()
        platform.report_ticket_failed.assert_awaited_once()
        assert platform.report_ticket_failed.await_args.args[1] == "infrastructure"
        assert all(slot.active_agent_id is None for slot in worker._slots.values())

    async def test_an_unparseable_answer_on_a_dropped_lease_is_not_a_revocation(
        self,
    ) -> None:
        """A 200 that does not parse more likely carried an accepted score.

        It is never relabelled as a silent revocation. It is contained as an
        ``infrastructure`` hand-back, as on a held lease, rather than escaping
        the sweep.
        """
        from ditto.tests.validator.test_worker import _job  # noqa: PLC0415

        job = _job("5MinerA" + "x" * 41)
        worker: Any = None

        async def upload(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("a failed submission has no transcript to publish")

        worker, platform, heartbeat = _accepting_worker(upload=upload, job=job)

        async def dropped_then_unparseable(agent_id: UUID, **_kwargs: object) -> None:
            heartbeat._evict.add(agent_id)
            await worker._report_heartbeat("running_benchmark")
            raise ValueError("score response did not validate")

        platform.submit_score = AsyncMock(side_effect=dropped_then_unparseable)

        await asyncio.wait_for(worker.run_once(set_weights=False), timeout=20)

        platform.submit_score.assert_awaited_once()
        assert worker._slots["slot-0"].revoked.is_set()
        platform.report_ticket_failed.assert_awaited_once()
        assert platform.report_ticket_failed.await_args.args[1] == "infrastructure"
        assert all(slot.active_agent_id is None for slot in worker._slots.values())

    async def test_a_late_watchdog_does_not_cancel_work_after_an_accepted_score(
        self,
    ) -> None:
        """The roster drop was recorded mid-submission; the watchdog woke late.

        The run had already marked the score accepted and cleared its ticket
        before the watchdog task ran. Its cancel would land on the final
        heartbeat and turn the accepted score into ``LeaseRevokedError``.
        """
        from ditto.tests.validator.test_worker import _job  # noqa: PLC0415

        job = _job("5MinerA" + "x" * 41)
        worker, _, _ = _worker_with_scorer(
            AsyncMock(), heartbeat=_RosterHeartbeat().mock
        )
        slot = worker._slots[job.slot_id]

        async def accepted_then_cleared() -> str:
            slot.active_agent_id = job.agent_id
            slot.submitting_score = True
            # Recorded, not acted on: the watchdog is woken but has not run.
            slot.revoked.set()
            worker._mark_score_accepted(slot)
            worker._clear_active_ticket()
            # The final ``polling`` heartbeat, where a late cancel would land.
            await asyncio.sleep(0.05)
            return "accepted"

        result = await worker._run_until_revoked(
            accepted_then_cleared, job=job, slot=slot
        )

        assert result == "accepted"
        # The watchdog is gone, so the next lease starts clean.
        assert not slot.submitting_score
        assert not slot.score_accepted

    async def test_a_late_watchdog_still_cancels_before_submission(self) -> None:
        """The control: without a submission the late watchdog still cancels."""
        from ditto.tests.validator.test_worker import _job  # noqa: PLC0415

        job = _job("5MinerA" + "x" * 41)
        worker, _, _ = _worker_with_scorer(
            AsyncMock(), heartbeat=_RosterHeartbeat().mock
        )
        slot = worker._slots[job.slot_id]

        async def revoked_then_cleared() -> str:
            slot.active_agent_id = job.agent_id
            slot.revoked.set()
            worker._clear_active_ticket()
            await asyncio.sleep(0.05)
            return "not reached"

        with pytest.raises(worker_module.LeaseRevokedError):
            await worker._run_until_revoked(revoked_then_cleared, job=job, slot=slot)

    async def test_a_revocation_still_cancels_a_run_that_has_not_submitted(
        self,
    ) -> None:
        """The control: the exemption starts at submission, not before."""
        worker, _, _ = _worker_with_scorer(
            AsyncMock(), heartbeat=_RosterHeartbeat().mock
        )
        slot = worker._slots["slot-0"]
        agent_id = uuid4()
        slot.active_agent_id = agent_id

        worker._apply_lease_roster(
            _response([]), advertised=_capacity(("slot-0", agent_id))
        )

        assert slot.revoked.is_set()
