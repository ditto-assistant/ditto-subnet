"""Regressions for the lease-deadline abort.

The invariant under test: **a validator that holds a ticket always resolves it
before the lease expires.** An unresolved ticket is indistinguishable in the
platform's ledger from a validator that died -- both read ``expired`` -- while
it holds one of the fleet's few scoring slots for the whole lease and says
nothing about the agent.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from ditto.validator import worker as worker_module
from ditto.validator.config import (
    LEASE_REPORT_MARGIN_SECONDS,
    lease_budget_seconds,
    run_budget_seconds,
)
from ditto.validator.dittobench import DittobenchClient
from ditto.validator.errors import LeaseDeadlineError, ValidatorInfrastructureError

# Production values: the operator's harness cap
# (VALIDATOR_DITTOBENCH_TIMEOUT_SECONDS) against the platform's _TICKET_TTL.
_HARNESS_CAP_SECONDS = 9900.0
_TICKET_TTL = timedelta(minutes=180)


class TestRunBudget:
    """The abort point is derived from the lease, never only from the dial."""

    def test_cap_binds_when_the_lease_is_long_enough_to_fund_it(self) -> None:
        now = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
        budget = run_budget_seconds(_HARNESS_CAP_SECONDS, now + _TICKET_TTL, now=now)
        assert budget == _HARNESS_CAP_SECONDS

    def test_lease_binds_when_it_is_shorter_than_the_dial(self) -> None:
        now = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
        budget = run_budget_seconds(
            _HARNESS_CAP_SECONDS, now + timedelta(minutes=30), now=now
        )
        assert budget == 1800.0 - LEASE_REPORT_MARGIN_SECONDS

    @pytest.mark.parametrize("ttl_minutes", [30, 45, 90, 120, 180, 430])
    def test_the_abort_always_lands_before_the_deadline_with_margin(
        self, ttl_minutes: int
    ) -> None:
        """The latent failure this closes.

        ``_TICKET_TTL`` has already moved 30 -> 45 -> 90 -> 120 -> 180 -> 430
        and back to 180 minutes. At 30 and 45 minutes a fixed harness cap can
        outlive a shortened lease, so the harness would still be polling when
        the ticket died -- guaranteed silent expiry. The budget must leave the
        reporting margin at every TTL, not just the current one.
        """
        now = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
        deadline = now + timedelta(minutes=ttl_minutes)
        budget = run_budget_seconds(_HARNESS_CAP_SECONDS, deadline, now=now)
        abort_at = now + timedelta(seconds=budget)
        assert abort_at < deadline
        assert (deadline - abort_at).total_seconds() >= LEASE_REPORT_MARGIN_SECONDS

    def test_an_unticketed_run_keeps_the_plain_cap(self) -> None:
        assert run_budget_seconds(_HARNESS_CAP_SECONDS, None) == _HARNESS_CAP_SECONDS

    def test_a_lease_inside_its_margin_reports_a_negative_budget(self) -> None:
        now = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
        assert lease_budget_seconds(now + timedelta(seconds=30), now=now) < 0

    def test_the_reporting_margin_covers_the_cancel_and_the_hand_back(self) -> None:
        """Both bounded calls spent out of the margin must fit inside it."""
        from ditto.validator.dittobench import _CANCEL_TIMEOUT_SECONDS

        spent = _CANCEL_TIMEOUT_SECONDS + worker_module._FAIL_REPORT_TIMEOUT_SECONDS
        assert spent < LEASE_REPORT_MARGIN_SECONDS


def _never_finishing_scorer() -> tuple[list[str], Any]:
    """A scorer that answers every poll promptly and never reaches a verdict."""
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "DELETE":
            return httpx.Response(202, json={"status": "failed"})
        return httpx.Response(200, json={"run_id": "run-1", "status": "running"})

    return methods, handler


class TestPollAbortsOnTheLease:
    async def test_a_run_that_outlives_its_lease_raises_rather_than_polling_on(
        self,
    ) -> None:
        """The run is aborted and cancelled instead of being polled to expiry."""
        methods, handler = _never_finishing_scorer()
        config = SimpleNamespace(
            dittobench_api_url="http://dittobench.test",
            # The dial is enormous; the lease is what must bind.
            dittobench_timeout_seconds=_HARNESS_CAP_SECONDS,
            dittobench_poll_seconds=0.01,
        )
        deadline = datetime.now(UTC) + timedelta(
            seconds=LEASE_REPORT_MARGIN_SECONDS + 0.2
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = DittobenchClient(cast(Any, config), http)
            with pytest.raises(LeaseDeadlineError, match="did not finish"):
                await asyncio.wait_for(
                    client._poll(
                        "run-1",
                        expected_bench_version=8,
                        ticket_deadline=deadline,
                    ),
                    timeout=30,
                )
        # Aborting must also release the sandbox, or the next lease lands on a
        # host still running the previous miner's harness.
        assert methods[-1] == "DELETE"

    async def test_the_abort_leaves_the_reporting_margin_intact(self) -> None:
        _, handler = _never_finishing_scorer()
        config = SimpleNamespace(
            dittobench_api_url="http://dittobench.test",
            dittobench_timeout_seconds=_HARNESS_CAP_SECONDS,
            dittobench_poll_seconds=0.01,
        )
        deadline = datetime.now(UTC) + timedelta(
            seconds=LEASE_REPORT_MARGIN_SECONDS + 0.2
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = DittobenchClient(cast(Any, config), http)
            with pytest.raises(LeaseDeadlineError):
                await asyncio.wait_for(
                    client._poll(
                        "run-1",
                        expected_bench_version=8,
                        ticket_deadline=deadline,
                    ),
                    timeout=30,
                )
        # The whole point: control returns with time still on the lease, so the
        # caller can sign and land a failure report.
        remaining = (deadline - datetime.now(UTC)).total_seconds()
        assert remaining > 0
        assert remaining > LEASE_REPORT_MARGIN_SECONDS * 0.5

    async def test_the_budget_is_wall_clock_not_accumulated_poll_intervals(
        self,
    ) -> None:
        """The accounting regression.

        The budget used to be accumulated ``dittobench_poll_seconds`` units,
        which charged nothing for the poll request itself, the progress
        callback, or the heartbeat it publishes. With a zero poll interval that
        counter never advances at all, so the loop had no bound in real time
        whatsoever -- this test hangs forever against the old accounting and is
        bounded here so a regression fails loudly instead of wedging the suite.
        """
        request_count = 0

        async def slow_handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_count
            if request.method == "DELETE":
                return httpx.Response(202, json={"status": "failed"})
            request_count += 1
            await asyncio.sleep(0.01)
            return httpx.Response(200, json={"run_id": "run-1", "status": "running"})

        config = SimpleNamespace(
            dittobench_api_url="http://dittobench.test",
            dittobench_timeout_seconds=0.2,
            dittobench_poll_seconds=0.0,
        )
        transport = httpx.MockTransport(slow_handler)
        async with httpx.AsyncClient(transport=transport) as http:
            client = DittobenchClient(cast(Any, config), http)
            with pytest.raises(worker_module.DittobenchError, match="did not finish"):
                await asyncio.wait_for(
                    client._poll("run-1", expected_bench_version=8),
                    timeout=30,
                )
        assert request_count > 0

    async def test_an_unticketed_poll_still_reports_the_plain_cap(self) -> None:
        """No lease means the dial is the only bound, and it still says so."""
        _, handler = _never_finishing_scorer()
        config = SimpleNamespace(
            dittobench_api_url="http://dittobench.test",
            dittobench_timeout_seconds=0.05,
            dittobench_poll_seconds=0.01,
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = DittobenchClient(cast(Any, config), http)
            with pytest.raises(worker_module.DittobenchError) as caught:
                await asyncio.wait_for(
                    client._poll("run-1", expected_bench_version=8),
                    timeout=30,
                )
        # A cap-bound abort is NOT a lease abort: the distinction is what lets
        # an operator tell "your dial fired" from "the lease ran out first".
        assert not isinstance(caught.value, LeaseDeadlineError)


def _hanging_worker(
    *, deadline: datetime, second_job: bool = False
) -> tuple[Any, MagicMock, MagicMock]:
    """A worker whose scorer never returns, leased until ``deadline``."""
    from ditto.tests.validator.test_worker import (  # noqa: PLC0415
        _config,
        _job,
        _platform_with_ledger,
        _report,
    )

    jobs = [_job("5MinerA" + "x" * 41, deadline=deadline)]
    if second_job:
        jobs.append(_job("5MinerB" + "x" * 41))
    platform = _platform_with_ledger(jobs=jobs, ledger=[])

    async def hang_on_the_short_lease(*_args: object, **kwargs: object) -> Any:
        """Hang for the short-leased ticket only; score any other one.

        Keyed on the lease rather than on a call counter: the abort can fire
        anywhere inside the bound -- including in the heartbeat that claims the
        slot, before the scorer is ever reached -- so "the first call" is not a
        stable identity. Reading ``ticket_deadline`` here also asserts that the
        lease actually reaches the scorer client.
        """
        if kwargs.get("ticket_deadline") == deadline:
            await asyncio.sleep(3600)
        return _report("run-b", 0.7)

    dittobench = MagicMock()
    dittobench.preflight = AsyncMock()
    dittobench.last_details = {}
    dittobench.score_tarball = AsyncMock(side_effect=hang_on_the_short_lease)

    worker = worker_module.ValidatorWorker(
        config=_config(),
        platform=platform,
        dittobench=dittobench,
        chain=MagicMock(put_weights=AsyncMock()),
        keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
    )
    return worker, platform, dittobench


class TestHangingAgentIsResolvedNotAbandoned:
    async def test_a_hang_is_reported_as_scoring_error_never_infrastructure(
        self,
    ) -> None:
        """The regression that matters most.

        ``infrastructure`` is the platform's *no-fault* classification: it mints
        a bounded retry grant that raises the attempt cap, applies an escalating
        cooldown, and re-leases the same artifact. An agent that hangs every
        single run therefore never consumes its budget and never reaches a
        verdict -- it just re-occupies quorum slots forever, which is exactly
        what starved the fleet. Running out of lease is the agent's own budget
        being spent, so it must consume the attempt.
        """
        deadline = datetime.now(UTC) + timedelta(
            seconds=LEASE_REPORT_MARGIN_SECONDS + 0.3
        )
        worker, platform, _ = _hanging_worker(deadline=deadline)

        await asyncio.wait_for(worker.run_once(set_weights=False), timeout=30)

        platform.report_ticket_failed.assert_awaited_once()
        _, reason, detail = platform.report_ticket_failed.await_args.args
        assert reason == "scoring_error"
        # And the hand-back names the cause, so an operator reading the ticket
        # sees "LeaseDeadlineError: ..." rather than an unlabelled class.
        assert detail.startswith("LeaseDeadlineError")

    async def test_the_ticket_is_resolved_rather_than_left_to_expire(self) -> None:
        deadline = datetime.now(UTC) + timedelta(
            seconds=LEASE_REPORT_MARGIN_SECONDS + 0.3
        )
        worker, platform, _ = _hanging_worker(deadline=deadline)

        await asyncio.wait_for(worker.run_once(set_weights=False), timeout=30)

        # Resolved: the hand-back landed, and it landed while the lease was
        # still alive so the platform can accept it.
        handed_job, _, _ = platform.report_ticket_failed.await_args.args
        assert handed_job.deadline == deadline
        assert datetime.now(UTC) < deadline

    async def test_one_hanging_agent_does_not_stall_the_rest_of_the_sweep(
        self,
    ) -> None:
        deadline = datetime.now(UTC) + timedelta(
            seconds=LEASE_REPORT_MARGIN_SECONDS + 1.0
        )
        worker, platform, _ = _hanging_worker(deadline=deadline, second_job=True)

        outcome = await asyncio.wait_for(worker.run_once(set_weights=False), timeout=30)

        # The slot moved on to the next ticket and scored it.
        assert outcome.queue_depth == 2
        platform.submit_score.assert_awaited_once()

    async def test_a_ticket_already_inside_its_margin_is_refused_before_starting(
        self,
    ) -> None:
        """Never begin work that cannot be resolved, and never claim silently."""
        from ditto.tests.validator.test_worker import _config, _job  # noqa: PLC0415

        job = _job(
            "5MinerA" + "x" * 41,
            deadline=datetime.now(UTC)
            + timedelta(seconds=LEASE_REPORT_MARGIN_SECONDS / 2),
        )
        worker = worker_module.ValidatorWorker(
            config=_config(),
            platform=MagicMock(),
            dittobench=MagicMock(),
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )
        worker._score_job = AsyncMock()  # type: ignore[method-assign]

        with pytest.raises(LeaseDeadlineError, match="not starting"):
            await worker._score_job_within_lease(job)

        cast(AsyncMock, worker._score_job).assert_not_awaited()

    async def test_a_genuine_infrastructure_failure_is_still_infrastructure(
        self,
    ) -> None:
        """The abort must not swallow the classification it does not own."""
        from ditto.tests.validator.test_worker import (  # noqa: PLC0415
            _config,
            _job,
            _platform_with_ledger,
        )

        jobs = [_job("5MinerA" + "x" * 41)]
        platform = _platform_with_ledger(jobs=jobs, ledger=[])
        dittobench = MagicMock()
        dittobench.preflight = AsyncMock()
        dittobench.score_tarball = AsyncMock(
            side_effect=ValidatorInfrastructureError("ollama forwarder lost")
        )
        worker = worker_module.ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=dittobench,
            chain=MagicMock(put_weights=AsyncMock()),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )

        await worker.run_once(set_weights=False)

        _, reason, detail = platform.report_ticket_failed.await_args.args
        assert reason == "infrastructure"
        # An error with no structured code still names itself, which beats the
        # three-value class and nothing else.
        assert detail == "ValidatorInfrastructureError: ollama forwarder lost"


class TestTheHandBackItselfIsBounded:
    async def test_a_stalled_platform_cannot_consume_the_reporting_margin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ "Best effort" must not mean "unbounded".

        This is the last call before the lease dies. A platform that accepts the
        connection and then stalls would otherwise spend the whole margin here
        and produce the silent expiry the abort exists to prevent.
        """
        from ditto.tests.validator.test_worker import (  # noqa: PLC0415
            _config,
            _job,
            _platform_with_ledger,
        )

        monkeypatch.setattr(worker_module, "_FAIL_REPORT_TIMEOUT_SECONDS", 0.05)
        jobs = [_job("5MinerA" + "x" * 41)]
        platform = _platform_with_ledger(jobs=jobs, ledger=[])

        async def stalls_forever(*_args: object, **_kwargs: object) -> None:
            await asyncio.sleep(3600)

        platform.report_ticket_failed = AsyncMock(side_effect=stalls_forever)
        dittobench = MagicMock()
        dittobench.preflight = AsyncMock()
        dittobench.score_tarball = AsyncMock(
            side_effect=ValidatorInfrastructureError("relay unreachable")
        )
        worker = worker_module.ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=dittobench,
            chain=MagicMock(put_weights=AsyncMock()),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )

        # Bounded: the sweep returns rather than hanging on the hand-back.
        outcome = await asyncio.wait_for(worker.run_once(set_weights=False), timeout=30)

        assert outcome.queue_depth == 1
        platform.report_ticket_failed.assert_awaited_once()
