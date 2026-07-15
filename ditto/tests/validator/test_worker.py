"""Unit tests for the validator worker sweep + KOTH+ATH weight computation."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.benchmark_progress import MAX_BENCHMARK_CHECKS
from ditto.api_models.validator import (
    ArtifactResponse,
    JobResponse,
    LedgerEntry,
    LedgerResponse,
    ScoreReport,
    SubmitScoreResponse,
    ValidatorHeartbeatResponse,
)
from ditto.chain import ChainError
from ditto.validator import worker as worker_mod
from ditto.validator.dittobench import DittobenchProgressSnapshot
from ditto.validator.errors import (
    DittobenchError,
    PlatformError,
    ValidatorInfrastructureError,
)
from ditto.validator.onchain_seed import derive_seed
from ditto.validator.weights import apply_miner_emission_cap, compute_weights
from ditto.validator.worker import ValidatorWorker

_VALIDATOR_HOTKEY = "5CZq6MdanxF3j8ACp8oVtiaphTeyrA7QFPU92ke2jEFzK1mp"
_BURN_HOTKEY = "5Burn" + "x" * 43
_T0 = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)


def _entry(
    miner: str,
    composite: float,
    *,
    first_seen: datetime = _T0,
    agent_id: UUID | None = None,
    n: int = 128,
) -> LedgerEntry:
    return LedgerEntry(
        miner_hotkey=miner,
        agent_id=agent_id or uuid4(),
        composite=composite,
        n=n,
        first_seen=first_seen,
        sha256="ab" * 32,
        size_bytes=524288,
        run_id="run_1",
        seed=1,
        validator_hotkey=_VALIDATOR_HOTKEY,
        signature="ab" * 64,
        status=AgentStatus.SCORED,
    )


# Mixed value types (float margin/share, int tail_size), so type as Any to
# unpack cleanly into compute_weights' int/float params under `mypy ditto/`.
_KOTH: dict[str, Any] = {"margin": 0.01, "tail_size": 4, "champion_share": 0.9}


class TestComputeWeights:
    def test_empty_ledger_returns_empty(self) -> None:
        assert compute_weights([], **_KOTH) == {}

    def test_all_zero_returns_empty(self) -> None:
        assert compute_weights([_entry("a", 0.0), _entry("b", 0.0)], **_KOTH) == {}

    def test_single_miner_takes_all(self) -> None:
        # No runners-up: the champion is the whole vector.
        assert compute_weights([_entry("a", 0.8)], **_KOTH) == {"a": 0.9}

    def test_champion_and_tail_split(self) -> None:
        entries = [
            _entry("champ", 0.90, first_seen=_T0),
            _entry("r1", 0.70, first_seen=_T0 + timedelta(minutes=1)),
            _entry("r2", 0.50, first_seen=_T0 + timedelta(minutes=2)),
        ]
        w = compute_weights(entries, **_KOTH)
        assert w["champ"] == pytest.approx(0.9)
        # 0.1 split over the two runners-up.
        assert w["r1"] == pytest.approx(0.05)
        assert w["r2"] == pytest.approx(0.05)

    def test_sub_margin_challenger_does_not_dethrone(self) -> None:
        # 'first' created earlier; 'second' beats it but by less than 1% => the
        # incumbent (first-seen) keeps the crown.
        first = _entry("first", 0.800, first_seen=_T0)
        second = _entry("second", 0.805, first_seen=_T0 + timedelta(minutes=1))
        w = compute_weights([first, second], **_KOTH)
        assert w["first"] == pytest.approx(0.9)
        assert w["second"] == pytest.approx(0.1)

    def test_over_margin_challenger_dethrones(self) -> None:
        first = _entry("first", 0.80, first_seen=_T0)
        second = _entry("second", 0.90, first_seen=_T0 + timedelta(minutes=1))
        w = compute_weights([first, second], **_KOTH)
        assert w["second"] == pytest.approx(0.9)
        assert w["first"] == pytest.approx(0.1)

    def test_scoring_order_independent(self) -> None:
        # Same entries in a different list order must crown the same champion:
        # the fold sorts by first_seen, not input order.
        a = _entry("a", 0.80, first_seen=_T0)
        b = _entry("b", 0.807, first_seen=_T0 + timedelta(minutes=1))
        c = _entry("c", 0.81, first_seen=_T0 + timedelta(minutes=2))
        forward = compute_weights([a, b, c], **_KOTH)
        shuffled = compute_weights([c, a, b], **_KOTH)
        assert forward == shuffled
        # a=0.80 -> b 0.807 !> 0.808 (no) -> c 0.81 > 0.808 (yes) => champ c.
        assert forward["c"] == pytest.approx(0.9)

    def test_tail_smaller_than_available(self) -> None:
        entries = [_entry(f"m{i}", 0.9 - i * 0.1, first_seen=_T0) for i in range(6)]
        w = compute_weights(entries, margin=0.01, tail_size=2, champion_share=0.9)
        # 1 champion + exactly 2 tail miners = 3 non-zero weights.
        assert len(w) == 3


class TestMinerEmissionCap:
    def test_empty_ledger_burns_everything(self) -> None:
        assert apply_miner_emission_cap(
            {}, miner_share=0.2, burn_hotkey=_BURN_HOTKEY
        ) == {_BURN_HOTKEY: 1.0}

    def test_single_miner_gets_exactly_twenty_percent(self) -> None:
        assert apply_miner_emission_cap(
            {"miner": 0.9}, miner_share=0.2, burn_hotkey=_BURN_HOTKEY
        ) == {"miner": pytest.approx(0.2), _BURN_HOTKEY: pytest.approx(0.8)}

    def test_normalizes_koth_vector_inside_miner_share(self) -> None:
        capped = apply_miner_emission_cap(
            {"champ": 0.9, "tail": 0.1},
            miner_share=0.2,
            burn_hotkey=_BURN_HOTKEY,
        )
        assert capped == {
            "champ": pytest.approx(0.18),
            "tail": pytest.approx(0.02),
            _BURN_HOTKEY: pytest.approx(0.8),
        }
        assert sum(capped.values()) == pytest.approx(1.0)

    def test_burn_hotkey_cannot_enter_miner_pool(self) -> None:
        assert apply_miner_emission_cap(
            {_BURN_HOTKEY: 0.9, "miner": 0.1},
            miner_share=0.2,
            burn_hotkey=_BURN_HOTKEY,
        ) == {"miner": pytest.approx(0.2), _BURN_HOTKEY: pytest.approx(0.8)}


def _job(
    miner_hotkey: str,
    *,
    sha256: str = "ab" * 32,
    dataset_sha256: str | None = "cd" * 32,
    deadline: datetime | None = None,
) -> JobResponse:
    return JobResponse(
        agent_id=uuid4(),
        miner_hotkey=miner_hotkey,
        sha256=sha256,
        deadline=deadline or datetime.now(UTC) + timedelta(hours=1),
        seed=12345,
        dataset_sha256=dataset_sha256,
        run_size="full",
    )


def _report(run_id: str, composite: float) -> ScoreReport:
    return ScoreReport(
        run_id=run_id,
        seed=1,
        composite=composite,
        tool_mean=composite,
        memory_mean=composite,
        median_ms=500,
        n=30,
        generated_at=datetime.now(UTC),
        per_case=[],
        structural_fingerprint=None,
        details=None,
    )


def _config() -> MagicMock:
    cfg = MagicMock()
    cfg.validator_hotkey = _VALIDATOR_HOTKEY
    cfg.netuid = 3
    cfg.koth_margin = 0.01
    cfg.koth_tail_size = 4
    cfg.koth_champion_share = 0.9
    cfg.koth_dethrone_z = 1.64
    cfg.miner_emission_share = 0.2
    cfg.burn_hotkey = _BURN_HOTKEY
    cfg.min_stake_tao = 0.0
    cfg.sweep_seconds = 120
    cfg.epoch_seconds = 3600
    cfg.queue_limit = 16
    return cfg


def _platform_with_ledger(
    *, jobs: list[JobResponse], ledger: list[LedgerEntry]
) -> MagicMock:
    platform = MagicMock()
    platform.submit_heartbeat = AsyncMock(
        return_value=ValidatorHeartbeatResponse(
            accepted=True, seen_at=datetime.now(UTC)
        )
    )
    # Ticket poll: hand out one ticket per job, then 204 (None) to end the sweep.
    platform.request_job = AsyncMock(side_effect=[*jobs, None])
    platform.get_ledger = AsyncMock(
        return_value=LedgerResponse(entries=ledger, count=len(ledger))
    )
    platform.get_artifact = AsyncMock(
        return_value=ArtifactResponse(
            agent_id=uuid4(),
            sha256="ab" * 32,
            download_url="https://signed.example/x.tar.gz?sig=1",
            expires_at=datetime.now(UTC),
        )
    )
    platform.submit_score = AsyncMock(
        return_value=SubmitScoreResponse(
            agent_id=uuid4(), status=AgentStatus.SCORED, accepted=True
        )
    )
    return platform


class TestRunOnce:
    async def test_scores_queue_and_sets_weights_from_ledger(self) -> None:
        job = _job("5MinerA" + "x" * 41)
        # The weight vector comes from the LEDGER, not the swept composites.
        ledger = [_entry("5MinerA" + "x" * 41, 0.9)]
        platform = _platform_with_ledger(jobs=[job], ledger=ledger)

        dittobench = MagicMock()
        dittobench.score_tarball = AsyncMock(return_value=_report("run", 0.9))
        chain = MagicMock()
        chain.put_weights = AsyncMock()
        keypair = MagicMock()
        keypair.sign = MagicMock(return_value=b"\x01" * 64)

        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=dittobench,
            chain=chain,
            keypair=keypair,
        )

        n = await worker.run_once()
        assert n == 1
        heartbeats = [
            call.args[0] for call in platform.submit_heartbeat.await_args_list
        ]
        assert [heartbeat.state for heartbeat in heartbeats] == [
            "polling",
            "running_benchmark",
            "running_benchmark",
            "running_benchmark",
            "polling",
            "updating_weights",
            "idle",
        ]
        heartbeat = heartbeats[0]
        assert heartbeat.validator_hotkey == _VALIDATOR_HOTKEY
        assert heartbeat.protocol_version == 4
        assert len(heartbeat.code_digest) == 64
        running = [
            heartbeat
            for heartbeat in heartbeats
            if heartbeat.state == "running_benchmark"
        ]
        assert [heartbeat.active_agent_id for heartbeat in running] == [
            job.agent_id,
            job.agent_id,
            job.agent_id,
        ]
        progresses = [heartbeat.benchmark_progress for heartbeat in running]
        assert all(progress is not None for progress in progresses)
        assert [progress.stage for progress in progresses if progress is not None] == [
            "preparing",
            "finalizing",
            "submitting_result",
        ]
        assert all(
            progress.ticket_deadline == job.deadline
            for progress in progresses
            if progress is not None
        )
        assert all(
            heartbeat.active_agent_id is None
            for heartbeat in heartbeats
            if heartbeat.state != "running_benchmark"
        )
        assert platform.submit_score.await_count == 1
        assert (
            platform.submit_score.await_args.kwargs["ticket_deadline"] == job.deadline
        )
        chain.put_weights.assert_awaited_once_with(
            {"5MinerA" + "x" * 41: 0.2, _BURN_HOTKEY: 0.8}
        )

    async def test_heartbeat_failure_does_not_block_scoring(self) -> None:
        platform = _platform_with_ledger(jobs=[], ledger=[])
        platform.submit_heartbeat.side_effect = PlatformError("platform old")
        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=MagicMock(),
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )

        assert await worker.run_once(set_weights=False) == 0
        platform.request_job.assert_awaited_once()

    async def test_failed_preflight_claims_no_ticket_but_still_sets_weights(
        self,
    ) -> None:
        ledger = [_entry("5MinerA" + "x" * 41, 0.9)]
        platform = _platform_with_ledger(jobs=[], ledger=ledger)
        dittobench = MagicMock()
        dittobench.preflight = AsyncMock(
            side_effect=ValidatorInfrastructureError("forwarder unavailable")
        )
        chain = MagicMock(put_weights=AsyncMock())
        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=dittobench,
            chain=chain,
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )

        assert await worker.run_once() == 0

        platform.request_job.assert_not_awaited()
        platform.get_artifact.assert_not_awaited()
        chain.put_weights.assert_awaited_once_with(
            {"5MinerA" + "x" * 41: 0.2, _BURN_HOTKEY: 0.8}
        )

    async def test_preflight_recovery_claims_on_next_normal_sweep(self) -> None:
        job = _job("5MinerA" + "x" * 41)
        platform = _platform_with_ledger(jobs=[job], ledger=[])
        dittobench = MagicMock()
        dittobench.preflight = AsyncMock(
            side_effect=[ValidatorInfrastructureError("ollama unavailable"), None]
        )
        dittobench.score_tarball = AsyncMock(return_value=_report("run", 0.9))
        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=dittobench,
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )

        assert await worker.run_once(set_weights=False) == 0
        platform.request_job.assert_not_awaited()
        assert await worker.run_once(set_weights=False) == 1
        platform.submit_score.assert_awaited_once()

    async def test_midrun_infrastructure_failure_ends_sweep_without_next_claim(
        self,
    ) -> None:
        jobs = [_job("5MinerA" + "x" * 41), _job("5MinerB" + "x" * 41)]
        platform = _platform_with_ledger(jobs=jobs, ledger=[])
        dittobench = MagicMock()
        dittobench.preflight = AsyncMock()
        dittobench.score_tarball = AsyncMock(
            side_effect=ValidatorInfrastructureError("ollama forwarder lost")
        )
        chain = MagicMock(put_weights=AsyncMock())
        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=dittobench,
            chain=chain,
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )

        assert await worker.run_once() == 1

        assert platform.request_job.await_count == 1
        platform.submit_score.assert_not_awaited()
        chain.put_weights.assert_awaited_once_with({_BURN_HOTKEY: 1.0})

    async def test_expired_ticket_report_is_not_submitted(self) -> None:
        platform = _platform_with_ledger(jobs=[], ledger=[])
        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=MagicMock(),
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )

        with pytest.raises(PlatformError, match="expired before score submission"):
            await worker._submit_report(
                uuid4(),
                "5MinerA" + "x" * 41,
                _report("late-run", 0.9),
                ticket_deadline=datetime.now(UTC) - timedelta(seconds=1),
            )

        platform.submit_score.assert_not_awaited()

    async def test_long_benchmark_refreshes_running_heartbeat(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(worker_mod, "_ACTIVE_HEARTBEAT_SECONDS", 0.001)
        platform = _platform_with_ledger(jobs=[_job("5MinerA" + "x" * 41)], ledger=[])

        async def slow_score(**_: object) -> ScoreReport:
            await asyncio.sleep(0.005)
            return _report("run", 0.9)

        dittobench = MagicMock(score_tarball=AsyncMock(side_effect=slow_score))
        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=dittobench,
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )

        await worker.run_once(set_weights=False)

        states = [
            call.args[0].state for call in platform.submit_heartbeat.await_args_list
        ]
        assert states.count("running_benchmark") >= 2
        assert states[-1] == "idle"

    async def test_ticketed_progress_maps_stages_and_clears_after_submit(self) -> None:
        job = _job("5MinerA" + "x" * 41)
        platform = _platform_with_ledger(jobs=[], ledger=[])

        async def score_with_progress(  # type: ignore[no-untyped-def]
            *, progress_callback, **_
        ) -> ScoreReport:
            for snapshot in (
                DittobenchProgressSnapshot(stage="building_harness"),
                DittobenchProgressSnapshot(stage="starting_harness"),
                DittobenchProgressSnapshot(stage="running_benchmark"),
                DittobenchProgressSnapshot(
                    stage="running_benchmark", completed=51, total=114
                ),
                DittobenchProgressSnapshot(
                    stage="finalizing", completed=114, total=114
                ),
            ):
                await progress_callback(snapshot)
            return _report("run", 0.9).model_copy(update={"n": 114})

        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=MagicMock(
                score_tarball=AsyncMock(side_effect=score_with_progress)
            ),
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )

        report = await worker._score_job(job)

        assert report.n == 114
        progress = [
            call.args[0].benchmark_progress
            for call in platform.submit_heartbeat.await_args_list
            if call.args[0].benchmark_progress is not None
        ]
        assert [item.stage for item in progress] == [
            "preparing",
            "building_harness",
            "starting_harness",
            "running_benchmark",
            "finalizing",
            "submitting_result",
        ]
        assert all(item.ticket_deadline == job.deadline for item in progress)
        cleared = platform.submit_heartbeat.await_args_list[-1].args[0]
        assert cleared.state == "polling"
        assert cleared.active_agent_id is None
        assert cleared.benchmark_progress is None
        platform.submit_score.assert_awaited_once()

    async def test_failed_run_reports_only_generic_retry_stage(self) -> None:
        job = _job("5MinerA" + "x" * 41)
        platform = _platform_with_ledger(jobs=[], ledger=[])

        async def fail_score(  # type: ignore[no-untyped-def]
            *, progress_callback, **_
        ) -> ScoreReport:
            await progress_callback(
                DittobenchProgressSnapshot(stage="building_harness")
            )
            raise DittobenchError("private build log and container id")

        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=MagicMock(score_tarball=AsyncMock(side_effect=fail_score)),
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )

        with pytest.raises(DittobenchError):
            await worker._score_job(job)

        serialized = [
            call.args[0].model_dump(mode="json")
            for call in platform.submit_heartbeat.await_args_list
        ]
        assert [
            body["benchmark_progress"]["stage"]
            for body in serialized
            if body["benchmark_progress"] is not None
        ] == ["preparing", "building_harness", "failed_retrying"]
        assert "private build log" not in str(serialized)
        assert serialized[-1]["benchmark_progress"]["stage"] == "failed_retrying"
        sent = platform.submit_heartbeat.await_count
        assert await worker._report_heartbeat("polling") is True
        assert platform.submit_heartbeat.await_count == sent
        platform.submit_score.assert_not_awaited()

    async def test_same_stage_counts_are_bucketed_throttled_and_reset(self) -> None:
        platform = _platform_with_ledger(jobs=[], ledger=[])
        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=MagicMock(),
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )
        deadline = datetime.now(UTC) + timedelta(hours=1)

        await worker._begin_active_ticket(uuid4(), deadline)
        await worker._publish_benchmark_progress("running_benchmark")
        sent = platform.submit_heartbeat.await_count
        await worker._publish_benchmark_progress(
            "running_benchmark", completed=5, total=100
        )
        assert platform.submit_heartbeat.await_count == sent

        assert worker._last_progress_heartbeat_monotonic is not None
        worker._last_progress_heartbeat_monotonic -= 61
        await worker._publish_benchmark_progress(
            "running_benchmark", completed=10, total=100
        )
        assert platform.submit_heartbeat.await_count == sent + 1

        # A same-stage poll with unstable/malformed counts cannot erase a safe
        # aggregate already delivered, including on the periodic refresh.
        await worker._publish_benchmark_progress("running_benchmark")
        await worker._emit_active_heartbeat()
        retained = platform.submit_heartbeat.await_args_list[-1].args[0]
        assert retained.benchmark_progress.completed == 10
        assert retained.benchmark_progress.total == 100

        # Internal scorer phases may recur, but public lifecycle never regresses.
        await worker._on_dittobench_progress(
            DittobenchProgressSnapshot(stage="starting_harness")
        )
        assert worker._benchmark_progress is not None
        assert worker._benchmark_progress.stage == "running_benchmark"

        await worker._publish_benchmark_progress(
            "failed_retrying", completed=10, total=100
        )
        failed = platform.submit_heartbeat.await_args_list[-1].args[0]
        assert failed.benchmark_progress.stage == "failed_retrying"

        worker._clear_active_ticket()
        next_agent = uuid4()
        next_deadline = deadline + timedelta(minutes=30)
        await worker._begin_active_ticket(next_agent, next_deadline)
        latest = platform.submit_heartbeat.await_args_list[-1].args[0]
        assert latest.active_agent_id == next_agent
        assert latest.benchmark_progress is not None
        assert latest.benchmark_progress.stage == "preparing"
        assert latest.benchmark_progress.ticket_deadline == next_deadline

    async def test_failed_delivery_does_not_advance_progress_throttle(self) -> None:
        platform = _platform_with_ledger(jobs=[], ledger=[])
        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=MagicMock(),
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )
        await worker._begin_active_ticket(
            uuid4(), datetime.now(UTC) + timedelta(hours=1)
        )
        last_delivered = worker._last_progress_heartbeat_monotonic
        platform.submit_heartbeat.return_value = ValidatorHeartbeatResponse(
            accepted=False, seen_at=datetime.now(UTC)
        )

        assert await worker._publish_benchmark_progress("building_harness") is False
        assert worker._last_progress_heartbeat_monotonic == last_delivered

    async def test_failed_stage_is_retried_before_visibility_retention(self) -> None:
        job = _job("5MinerA" + "x" * 41)
        platform = _platform_with_ledger(jobs=[], ledger=[])
        accepted = lambda value: ValidatorHeartbeatResponse(  # noqa: E731
            accepted=value, seen_at=datetime.now(UTC)
        )
        platform.submit_heartbeat.side_effect = [
            accepted(True),  # preparing
            accepted(False),  # raw scorer failed stage
            accepted(True),  # exception-path forced retry
        ]

        async def fail_after_failed_status(  # type: ignore[no-untyped-def]
            *, progress_callback, **_
        ) -> ScoreReport:
            await progress_callback(DittobenchProgressSnapshot(stage="failed_retrying"))
            raise DittobenchError("private scorer failure")

        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=MagicMock(
                score_tarball=AsyncMock(side_effect=fail_after_failed_status)
            ),
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )

        with pytest.raises(DittobenchError):
            await worker._score_job(job)

        progress = [
            call.args[0].benchmark_progress
            for call in platform.submit_heartbeat.await_args_list
        ]
        assert [item.stage for item in progress if item is not None] == [
            "preparing",
            "failed_retrying",
            "failed_retrying",
        ]
        assert platform.submit_heartbeat.await_count == 3
        sent = platform.submit_heartbeat.await_count
        assert await worker._report_heartbeat("polling") is True
        assert platform.submit_heartbeat.await_count == sent

    async def test_overlapping_sends_record_only_the_accepted_snapshot(self) -> None:
        platform = _platform_with_ledger(jobs=[], ledger=[])
        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=MagicMock(),
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )
        await worker._begin_active_ticket(
            uuid4(), datetime.now(UTC) + timedelta(hours=1)
        )
        platform.submit_heartbeat.reset_mock()

        first_started = asyncio.Event()
        first_release = asyncio.Event()
        second_started = asyncio.Event()
        second_release = asyncio.Event()
        requests: list[Any] = []

        async def serialize(request: Any) -> ValidatorHeartbeatResponse:
            requests.append(request)
            if len(requests) == 1:
                first_started.set()
                await first_release.wait()
            else:
                second_started.set()
                await second_release.wait()
            return ValidatorHeartbeatResponse(accepted=True, seen_at=datetime.now(UTC))

        platform.submit_heartbeat.side_effect = serialize
        first = asyncio.create_task(
            worker._publish_benchmark_progress(
                "running_benchmark", completed=10, total=100
            )
        )
        await first_started.wait()
        second = asyncio.create_task(
            worker._publish_benchmark_progress("finalizing", completed=100, total=100)
        )
        while (
            worker._benchmark_progress is None
            or worker._benchmark_progress.stage != "finalizing"
        ):
            await asyncio.sleep(0)

        first_release.set()
        await second_started.wait()
        assert requests[0].benchmark_progress.completed == 10
        assert worker._last_progress_bucket == 10

        second_release.set()
        assert await first is True
        assert await second is True
        assert requests[1].benchmark_progress.stage == "finalizing"
        assert worker._last_progress_bucket == 100

    @pytest.mark.parametrize(
        ("n", "ticket_deadline"),
        [
            (0, datetime.now(UTC) + timedelta(hours=1)),
            (MAX_BENCHMARK_CHECKS + 1, datetime.now(UTC) + timedelta(hours=1)),
            (114, datetime(2026, 7, 14, 12, 0, 0)),
        ],
    )
    async def test_invalid_telemetry_cannot_block_terminal_score_submission(
        self, n: int, ticket_deadline: datetime
    ) -> None:
        platform = _platform_with_ledger(jobs=[], ledger=[])
        report = _report("run", 0.9).model_copy(update={"n": n})
        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=MagicMock(score_tarball=AsyncMock(return_value=report)),
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )

        result = await worker._evaluate_and_submit(
            uuid4(),
            "ab" * 32,
            "5MinerA" + "x" * 41,
            ticket_deadline=ticket_deadline,
        )

        assert result.n == n
        platform.submit_score.assert_awaited_once()

    async def test_hanging_progress_heartbeat_cannot_block_terminal_submit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(worker_mod, "_ACTIVE_TELEMETRY_TIMEOUT_SECONDS", 0.001)
        job = _job("5MinerA" + "x" * 41)
        platform = _platform_with_ledger(jobs=[], ledger=[])

        async def hang(_: object) -> None:
            await asyncio.Event().wait()

        platform.submit_heartbeat = AsyncMock(side_effect=hang)
        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=MagicMock(
                score_tarball=AsyncMock(return_value=_report("run", 0.9))
            ),
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )

        report = await asyncio.wait_for(worker._score_job(job), timeout=0.2)

        assert report.composite == 0.9
        platform.submit_score.assert_awaited_once()

    async def test_unticketed_rescore_has_no_agent_or_progress(self) -> None:
        platform = _platform_with_ledger(jobs=[], ledger=[])
        dittobench = MagicMock(
            score_tarball=AsyncMock(return_value=_report("rescore", 0.8))
        )
        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=dittobench,
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )

        await worker._evaluate(uuid4(), "ab" * 32, seed=7)

        running = platform.submit_heartbeat.await_args_list[0].args[0]
        assert running.state == "running_benchmark"
        assert running.active_agent_id is None
        assert running.benchmark_progress is None
        assert dittobench.score_tarball.await_args.kwargs["progress_callback"] is None

    async def test_forwards_tarball_sha_to_scorer(self) -> None:
        # The registered digest must be forwarded so dittobench re-verifies the
        # fetched bytes and pins the build tag to the content hash.
        job = _job("5MinerA" + "x" * 41)
        platform = _platform_with_ledger(
            jobs=[job], ledger=[_entry("5MinerA" + "x" * 41, 0.9)]
        )
        dittobench = MagicMock()
        dittobench.score_tarball = AsyncMock(return_value=_report("run", 0.9))
        keypair = MagicMock()
        keypair.sign = MagicMock(return_value=b"\x01" * 64)

        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=dittobench,
            chain=MagicMock(put_weights=AsyncMock()),
            keypair=keypair,
        )
        await worker.run_once()

        dittobench.score_tarball.assert_awaited_once()
        kwargs = dittobench.score_tarball.await_args.kwargs
        assert kwargs["tarball_sha256"] == "ab" * 32
        assert kwargs["tarball_url"].startswith("https://")
        # The ticket pins the dataset: seed + dataset_sha256 + run_size are
        # forwarded so the scorer takes the tamper-evident /v1/score path.
        assert kwargs["seed"] == 12345
        assert kwargs["dataset_sha256"] == "cd" * 32
        assert kwargs["run_size"] == "full"

    async def test_sha_mismatch_skips_agent(self) -> None:
        # If the queue item and artifact disagree on the digest, the agent is
        # skipped (never scored), but the sweep continues and weights still come
        # from the durable ledger.
        job = _job("5MinerA" + "x" * 41)
        platform = _platform_with_ledger(
            jobs=[job], ledger=[_entry("5Champ" + "x" * 42, 0.85)]
        )
        # Artifact reports a DIFFERENT sha than the ticket's "ab" * 32.
        platform.get_artifact = AsyncMock(
            return_value=ArtifactResponse(
                agent_id=uuid4(),
                sha256="cd" * 32,
                download_url="https://signed.example/x.tar.gz?sig=1",
                expires_at=datetime.now(UTC),
            )
        )
        dittobench = MagicMock()
        dittobench.score_tarball = AsyncMock(return_value=_report("run", 0.9))
        chain = MagicMock(put_weights=AsyncMock())

        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=dittobench,
            chain=chain,
            keypair=MagicMock(),
        )
        n = await worker.run_once()

        assert n == 1  # the item was pulled...
        dittobench.score_tarball.assert_not_awaited()  # ...but never scored
        platform.submit_score.assert_not_awaited()
        chain.put_weights.assert_awaited_once_with(
            {"5Champ" + "x" * 42: 0.2, _BURN_HOTKEY: 0.8}
        )

    async def test_ground_seed_ticket_is_refused(self) -> None:
        # P2: a ticket whose seed does not re-derive from its pinned on-chain
        # block hash could have been chosen by the platform (seed grinding).
        # It is refused before the artifact is even fetched; the sweep
        # continues and weights still come from the ledger.
        agent_id = uuid4()
        block_hash = "0x" + "12" * 32
        job = _job("5MinerA" + "x" * 41).model_copy(
            update={
                "agent_id": agent_id,
                "dataset_seed_block_hash": block_hash,
                "seed": derive_seed(block_hash, agent_id) + 1,
            }
        )
        platform = _platform_with_ledger(
            jobs=[job], ledger=[_entry("5Champ" + "x" * 42, 0.85)]
        )
        dittobench = MagicMock()
        dittobench.score_tarball = AsyncMock(return_value=_report("run", 0.9))
        chain = MagicMock(put_weights=AsyncMock())

        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=dittobench,
            chain=chain,
            keypair=MagicMock(),
        )
        n = await worker.run_once()

        assert n == 1  # counted against the sweep cap...
        platform.get_artifact.assert_not_awaited()  # ...but refused unscored
        dittobench.score_tarball.assert_not_awaited()
        platform.submit_score.assert_not_awaited()
        chain.put_weights.assert_awaited_once_with(
            {"5Champ" + "x" * 42: 0.2, _BURN_HOTKEY: 0.8}
        )

    async def test_derived_seed_ticket_is_scored(self) -> None:
        # The companion arm: a ticket whose seed DOES re-derive proceeds.
        agent_id = uuid4()
        block_hash = "0x" + "34" * 32
        job = _job("5MinerA" + "x" * 41).model_copy(
            update={
                "agent_id": agent_id,
                "dataset_seed_block_hash": block_hash,
                "seed": derive_seed(block_hash, agent_id),
            }
        )
        platform = _platform_with_ledger(
            jobs=[job], ledger=[_entry("5MinerA" + "x" * 41, 0.9)]
        )
        dittobench = MagicMock()
        dittobench.score_tarball = AsyncMock(return_value=_report("run", 0.9))
        chain = MagicMock(put_weights=AsyncMock())
        keypair = MagicMock()
        keypair.sign = MagicMock(return_value=b"\x01" * 64)

        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=dittobench,
            chain=chain,
            keypair=keypair,
        )
        await worker.run_once()

        dittobench.score_tarball.assert_awaited_once()
        platform.submit_score.assert_awaited_once()

    async def test_lapsed_ticket_is_skipped_unscored(self) -> None:
        # A ticket already past its deadline is counted as pulled but never
        # scored — the platform re-opens it, so spending a full run would only
        # produce a score the platform would reject as late.
        job = _job(
            "5MinerA" + "x" * 41,
            deadline=datetime.now(UTC) - timedelta(seconds=1),
        )
        platform = _platform_with_ledger(
            jobs=[job], ledger=[_entry("5Champ" + "x" * 42, 0.85)]
        )
        dittobench = MagicMock()
        dittobench.score_tarball = AsyncMock(return_value=_report("run", 0.9))
        chain = MagicMock(put_weights=AsyncMock())

        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=dittobench,
            chain=chain,
            keypair=MagicMock(),
        )
        n = await worker.run_once()

        assert n == 1  # counted against the sweep cap...
        platform.get_artifact.assert_not_awaited()  # ...but not even fetched
        dittobench.score_tarball.assert_not_awaited()
        platform.submit_score.assert_not_awaited()

    async def test_empty_queue_still_sets_weights_from_ledger(self) -> None:
        # The regression that broke incentives: an empty queue must NOT skip
        # weight-setting, or the reigning champion is zeroed.
        ledger = [_entry("5Champion" + "x" * 39, 0.85)]
        platform = _platform_with_ledger(jobs=[], ledger=ledger)
        chain = MagicMock()
        chain.put_weights = AsyncMock()

        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=MagicMock(),
            chain=chain,
            keypair=MagicMock(),
        )

        assert await worker.run_once() == 0
        chain.put_weights.assert_awaited_once_with(
            {"5Champion" + "x" * 39: 0.2, _BURN_HOTKEY: 0.8}
        )

    async def test_empty_ledger_burns_all_miner_emission(self) -> None:
        platform = _platform_with_ledger(jobs=[], ledger=[])
        chain = MagicMock()
        chain.put_weights = AsyncMock()

        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=MagicMock(),
            chain=chain,
            keypair=MagicMock(),
        )
        assert await worker.run_once() == 0
        chain.put_weights.assert_awaited_once_with({_BURN_HOTKEY: 1.0})

    async def test_job_poll_failure_still_submits_safe_idle_weights(self) -> None:
        platform = _platform_with_ledger(jobs=[], ledger=[])
        platform.request_job = AsyncMock(side_effect=PlatformError("invalid request"))
        chain = MagicMock()
        chain.put_weights = AsyncMock()

        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=MagicMock(),
            chain=chain,
            keypair=MagicMock(),
        )

        assert await worker.run_once() == 0
        chain.put_weights.assert_awaited_once_with({_BURN_HOTKEY: 1.0})

    async def test_job_poll_failure_preserves_accepted_score_weights(self) -> None:
        ledger = [_entry("5Champion" + "x" * 39, 0.85)]
        platform = _platform_with_ledger(jobs=[], ledger=ledger)
        platform.request_job = AsyncMock(side_effect=PlatformError("unavailable"))
        chain = MagicMock()
        chain.put_weights = AsyncMock()

        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=MagicMock(),
            chain=chain,
            keypair=MagicMock(),
        )

        assert await worker.run_once() == 0
        chain.put_weights.assert_awaited_once_with(
            {"5Champion" + "x" * 39: 0.2, _BURN_HOTKEY: 0.8}
        )

    async def test_stale_ledger_is_still_folded_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A platform-served last-known-good (stale) ledger must still set weights —
        # the pool is durable — but log that the platform is degraded.
        ledger = [_entry("5Champion" + "x" * 39, 0.85)]
        platform = _platform_with_ledger(jobs=[], ledger=ledger)
        platform.get_ledger = AsyncMock(
            return_value=LedgerResponse(
                entries=ledger, count=len(ledger), stale=True, age_seconds=120
            )
        )
        chain = MagicMock()
        chain.put_weights = AsyncMock()

        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=MagicMock(),
            chain=chain,
            keypair=MagicMock(),
        )
        with caplog.at_level("WARNING"):
            await worker.run_once()
        chain.put_weights.assert_awaited_once_with(
            {"5Champion" + "x" * 39: 0.2, _BURN_HOTKEY: 0.8}
        )
        assert any("STALE" in r.message for r in caplog.records)

    async def test_no_permit_skips_weight_submission(self) -> None:
        # A validator hotkey without a permit must not burn an epoch submitting
        # weights the chain will reject; skip loudly instead.
        ledger = [_entry("5Champion" + "x" * 39, 0.85)]
        platform = _platform_with_ledger(jobs=[], ledger=ledger)
        chain = MagicMock()
        chain.put_weights = AsyncMock()
        chain.has_validator_permit = AsyncMock(return_value=False)

        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=MagicMock(),
            chain=chain,
            keypair=MagicMock(),
        )
        assert await worker.run_once() == 0
        chain.has_validator_permit.assert_awaited_once()
        chain.put_weights.assert_not_awaited()

    async def test_permit_present_sets_weights(self) -> None:
        ledger = [_entry("5Champion" + "x" * 39, 0.85)]
        platform = _platform_with_ledger(jobs=[], ledger=ledger)
        chain = MagicMock()
        chain.put_weights = AsyncMock()
        chain.has_validator_permit = AsyncMock(return_value=True)

        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=MagicMock(),
            chain=chain,
            keypair=MagicMock(),
        )
        assert await worker.run_once() == 0
        chain.put_weights.assert_awaited_once_with(
            {"5Champion" + "x" * 39: 0.2, _BURN_HOTKEY: 0.8}
        )

    async def test_permit_check_error_fails_open(self) -> None:
        # A flaky metagraph read must not wedge weight-setting; proceed and let
        # the chain enforce.
        ledger = [_entry("5Champion" + "x" * 39, 0.85)]
        platform = _platform_with_ledger(jobs=[], ledger=ledger)
        chain = MagicMock()
        chain.put_weights = AsyncMock()
        chain.has_validator_permit = AsyncMock(side_effect=ChainError("pylon down"))

        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=MagicMock(),
            chain=chain,
            keypair=MagicMock(),
        )
        assert await worker.run_once() == 0
        chain.put_weights.assert_awaited_once()

    async def test_stake_below_minimum_skips_weight_submission(self) -> None:
        # With VALIDATOR_MIN_STAKE_TAO set, a demonstrably short stake must not
        # burn an epoch on a guaranteed chain rejection.
        ledger = [_entry("5Champion" + "x" * 39, 0.85)]
        platform = _platform_with_ledger(jobs=[], ledger=ledger)
        cfg = _config()
        cfg.min_stake_tao = 1000.0
        chain = MagicMock()
        chain.put_weights = AsyncMock()
        chain.has_validator_permit = AsyncMock(return_value=True)
        chain.get_stake_tao = AsyncMock(return_value=10.0)

        worker = ValidatorWorker(
            config=cfg,
            platform=platform,
            dittobench=MagicMock(),
            chain=chain,
            keypair=MagicMock(),
        )
        assert await worker.run_once() == 0
        chain.get_stake_tao.assert_awaited_once()
        chain.put_weights.assert_not_awaited()

    async def test_stake_above_minimum_sets_weights(self) -> None:
        ledger = [_entry("5Champion" + "x" * 39, 0.85)]
        platform = _platform_with_ledger(jobs=[], ledger=ledger)
        cfg = _config()
        cfg.min_stake_tao = 1000.0
        chain = MagicMock()
        chain.put_weights = AsyncMock()
        chain.has_validator_permit = AsyncMock(return_value=True)
        chain.get_stake_tao = AsyncMock(return_value=5000.0)

        worker = ValidatorWorker(
            config=cfg,
            platform=platform,
            dittobench=MagicMock(),
            chain=chain,
            keypair=MagicMock(),
        )
        assert await worker.run_once() == 0
        chain.put_weights.assert_awaited_once_with(
            {"5Champion" + "x" * 39: 0.2, _BURN_HOTKEY: 0.8}
        )

    async def test_stake_check_error_fails_open(self) -> None:
        # Same fail-open posture as the permit check: a flaky read proceeds.
        ledger = [_entry("5Champion" + "x" * 39, 0.85)]
        platform = _platform_with_ledger(jobs=[], ledger=ledger)
        cfg = _config()
        cfg.min_stake_tao = 1000.0
        chain = MagicMock()
        chain.put_weights = AsyncMock()
        chain.has_validator_permit = AsyncMock(return_value=True)
        chain.get_stake_tao = AsyncMock(side_effect=ChainError("pylon down"))

        worker = ValidatorWorker(
            config=cfg,
            platform=platform,
            dittobench=MagicMock(),
            chain=chain,
            keypair=MagicMock(),
        )
        assert await worker.run_once() == 0
        chain.put_weights.assert_awaited_once()

    async def test_min_stake_disabled_never_reads_stake(self) -> None:
        # min_stake_tao=0 (the default; localnet has staking disabled) must not
        # even touch the stake read.
        ledger = [_entry("5Champion" + "x" * 39, 0.85)]
        platform = _platform_with_ledger(jobs=[], ledger=ledger)
        chain = MagicMock()
        chain.put_weights = AsyncMock()
        chain.has_validator_permit = AsyncMock(return_value=True)
        chain.get_stake_tao = AsyncMock(return_value=0.0)

        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=MagicMock(),
            chain=chain,
            keypair=MagicMock(),
        )
        assert await worker.run_once() == 0
        chain.get_stake_tao.assert_not_awaited()
        chain.put_weights.assert_awaited_once()

    async def test_set_weights_false_scores_without_touching_weights(self) -> None:
        # The scoring-only sweep (between weight-set epochs) drains the queue and
        # re-scores stale champions (a ledger read) but never submits weights.
        job = _job("5MinerA" + "x" * 41)
        ledger = [_entry("5MinerA" + "x" * 41, 0.9)]
        platform = _platform_with_ledger(jobs=[job], ledger=ledger)
        dittobench = MagicMock()
        dittobench.score_tarball = AsyncMock(return_value=_report("run", 0.9))
        chain = MagicMock()
        chain.put_weights = AsyncMock()
        keypair = MagicMock()
        keypair.sign = MagicMock(return_value=b"\x01" * 64)

        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=dittobench,
            chain=chain,
            keypair=keypair,
        )
        n = await worker.run_once(set_weights=False)
        assert n == 1
        assert platform.submit_score.await_count == 1
        platform.get_ledger.assert_awaited()  # read for the stale-champion re-score
        chain.put_weights.assert_not_awaited()

    async def test_one_agent_failure_does_not_block_weights(self) -> None:
        from ditto.validator.errors import DittobenchError

        bad = _job("5MinerB" + "x" * 41)
        good = _job("5MinerG" + "x" * 41)
        ledger = [_entry("5MinerG" + "x" * 41, 0.7)]
        platform = _platform_with_ledger(jobs=[bad, good], ledger=ledger)

        async def _score(**_: object) -> ScoreReport:
            if _score.calls == 0:  # type: ignore[attr-defined]
                _score.calls += 1  # type: ignore[attr-defined]
                raise DittobenchError("build failed")
            return _report("run", 0.7)

        _score.calls = 0  # type: ignore[attr-defined]
        dittobench = MagicMock()
        dittobench.score_tarball = AsyncMock(side_effect=_score)
        chain = MagicMock()
        chain.put_weights = AsyncMock()
        keypair = MagicMock()
        keypair.sign = MagicMock(return_value=b"\x01" * 64)

        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=dittobench,
            chain=chain,
            keypair=keypair,
        )

        n = await worker.run_once()
        assert n == 2
        # The lone eligible miner receives the released 20%; 80% stays burned.
        chain.put_weights.assert_awaited_once_with(
            {"5MinerG" + "x" * 41: 0.2, _BURN_HOTKEY: 0.8}
        )

    async def test_ledger_fetch_failure_leaves_weights_untouched(self) -> None:
        from ditto.validator.errors import PlatformError

        platform = _platform_with_ledger(jobs=[], ledger=[])
        platform.get_ledger = AsyncMock(side_effect=PlatformError("ledger 503"))
        chain = MagicMock()
        chain.put_weights = AsyncMock()

        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=MagicMock(),
            chain=chain,
            keypair=MagicMock(),
        )
        assert await worker.run_once() == 0
        chain.put_weights.assert_not_awaited()

    async def test_put_weights_retries_transient_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(worker_mod, "_WEIGHT_SET_RETRY_SECONDS", 0.0)
        ledger = [_entry("5Champion" + "x" * 39, 0.85)]
        platform = _platform_with_ledger(jobs=[], ledger=ledger)
        chain = MagicMock()
        chain.put_weights = AsyncMock(side_effect=[ChainError("timeout"), None])

        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=MagicMock(),
            chain=chain,
            keypair=MagicMock(),
        )
        await worker.run_once()
        # Failed once, retried, then succeeded.
        assert chain.put_weights.await_count == 2

    async def test_put_weights_gives_up_after_max_attempts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(worker_mod, "_WEIGHT_SET_RETRY_SECONDS", 0.0)
        ledger = [_entry("5Champion" + "x" * 39, 0.85)]
        platform = _platform_with_ledger(jobs=[], ledger=ledger)
        chain = MagicMock()
        chain.put_weights = AsyncMock(side_effect=ChainError("down"))

        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=MagicMock(),
            chain=chain,
            keypair=MagicMock(),
        )
        # Must not raise — the durable ledger means next epoch retries.
        await worker.run_once()
        assert chain.put_weights.await_count == worker_mod._WEIGHT_SET_ATTEMPTS


class TestRetryBackoff:
    def test_transient_backoff_is_exponential(self) -> None:
        err = ChainError("connection reset")
        delays = [worker_mod._retry_delay_seconds(a, err) for a in (1, 2, 3)]
        assert delays == [2.0, 4.0, 8.0]

    def test_rate_limit_backoff_uses_block_time_base(self) -> None:
        # A rate-limit rejection retried inside the same block is a guaranteed
        # second rejection, so it backs off from a full block time.
        err = ChainError("subtensor returned: SettingWeightsTooFast")
        delays = [worker_mod._retry_delay_seconds(a, err) for a in (1, 2)]
        assert delays == [12.0, 24.0]

    @pytest.mark.parametrize(
        "message",
        [
            "SettingWeightsTooFast",
            "weights rate limit exceeded",
            "RateLimitExceeded",
            "you are setting weights too fast",
        ],
    )
    def test_rate_limit_detection(self, message: str) -> None:
        assert worker_mod._is_rate_limit_error(ChainError(message))

    def test_ordinary_error_is_not_rate_limit(self) -> None:
        assert not worker_mod._is_rate_limit_error(ChainError("pylon 502"))


class TestChainCadenceFloor:
    def _worker(self, chain: MagicMock) -> ValidatorWorker:
        return ValidatorWorker(
            config=_config(),
            platform=MagicMock(),
            dittobench=MagicMock(),
            chain=chain,
            keypair=MagicMock(),
        )

    async def test_floor_is_rate_limit_blocks_times_block_time(self) -> None:
        chain = MagicMock()
        chain.get_weights_rate_limit = AsyncMock(return_value=100)
        chain.get_tempo = AsyncMock(return_value=360)
        assert await self._worker(chain)._chain_min_epoch_seconds() == 1200.0

    async def test_missing_read_method_falls_back_to_config(self) -> None:
        # A sink without the hyperparameter reads (older setter) keeps the
        # configured cadence: floor 0 means max() picks epoch_seconds.
        chain = MagicMock(spec=["put_weights"])
        assert await self._worker(chain)._chain_min_epoch_seconds() == 0.0

    async def test_read_error_falls_back_to_config(self) -> None:
        chain = MagicMock()
        chain.get_weights_rate_limit = AsyncMock(side_effect=ChainError("down"))
        assert await self._worker(chain)._chain_min_epoch_seconds() == 0.0

    async def test_unknown_netuid_falls_back_to_config(self) -> None:
        chain = MagicMock()
        chain.get_weights_rate_limit = AsyncMock(return_value=None)
        assert await self._worker(chain)._chain_min_epoch_seconds() == 0.0

    async def test_non_numeric_read_falls_back_to_config(self) -> None:
        # A sink returning garbage must fail open, not crash the loop at boot.
        chain = MagicMock()
        chain.get_weights_rate_limit = AsyncMock(return_value=object())
        assert await self._worker(chain)._chain_min_epoch_seconds() == 0.0


class TestConfirmAndSubmit:
    """P4: the re-score confirmation over K common seeds submits exactly ONE
    signed score (the median-composite run) carrying every per-seed composite."""

    def _seeded_report(self, seed: int, composite: float) -> ScoreReport:
        r = _report(f"run-{seed}", composite)
        return r.model_copy(update={"seed": seed})

    async def _worker(self, dittobench: MagicMock, platform: MagicMock) -> Any:
        keypair = MagicMock()
        keypair.sign = MagicMock(return_value=b"\x01" * 64)
        return ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=dittobench,
            chain=MagicMock(),
            keypair=keypair,
        )

    async def test_submits_one_median_run_with_all_confirmations(self) -> None:
        agent_id = uuid4()
        composites = {10: 0.90, 20: 0.70, 30: 0.80}  # median = 0.80 (seed 30)

        async def _score(**kw: Any) -> ScoreReport:
            return self._seeded_report(kw["seed"], composites[kw["seed"]])

        dittobench = MagicMock()
        dittobench.score_tarball = AsyncMock(side_effect=_score)
        dittobench.last_details = {"bench_version": 3}
        platform = _platform_with_ledger(jobs=[], ledger=[])
        platform.get_artifact = AsyncMock(
            return_value=ArtifactResponse(
                agent_id=agent_id,
                sha256="ab" * 32,
                download_url="https://signed.example/x.tar.gz",
                expires_at=datetime.now(UTC),
            )
        )
        w = await self._worker(dittobench, platform)

        out = await w._confirm_and_submit(
            agent_id, "ab" * 32, "5Miner" + "x" * 42, seeds=[10, 20, 30]
        )

        # One evaluation per seed, but exactly one submitted score.
        assert dittobench.score_tarball.await_count == 3
        assert platform.submit_score.await_count == 1
        report = platform.submit_score.await_args.kwargs["report"]
        # The representative is a real run (the median composite/seed), and it
        # carries the sorted per-seed composites for the fold's median.
        assert report.composite == 0.80
        assert report.seed == 30
        assert report.confirmation_composites == [0.70, 0.80, 0.90]
        assert out is report

    async def test_single_surviving_seed_has_no_confirmations(self) -> None:
        # If all but one seed fail, this degrades to a plain single-seed submit
        # (no confirmation_composites, so the fold uses the raw composite).
        agent_id = uuid4()

        async def _score(**kw: Any) -> ScoreReport:
            if kw["seed"] == 20:
                return self._seeded_report(20, 0.75)
            raise worker_mod.DittobenchError("boom")

        dittobench = MagicMock()
        dittobench.score_tarball = AsyncMock(side_effect=_score)
        dittobench.last_details = None
        platform = _platform_with_ledger(jobs=[], ledger=[])
        platform.get_artifact = AsyncMock(
            return_value=ArtifactResponse(
                agent_id=agent_id,
                sha256="ab" * 32,
                download_url="https://signed.example/x.tar.gz",
                expires_at=datetime.now(UTC),
            )
        )
        w = await self._worker(dittobench, platform)

        report = await w._confirm_and_submit(
            agent_id, "ab" * 32, "5Miner" + "x" * 42, seeds=[10, 20, 30]
        )
        assert platform.submit_score.await_count == 1
        assert report is not None
        assert report.composite == 0.75
        assert report.confirmation_composites is None

    async def test_all_seeds_fail_returns_none_and_submits_nothing(self) -> None:
        agent_id = uuid4()
        dittobench = MagicMock()
        dittobench.score_tarball = AsyncMock(
            side_effect=worker_mod.DittobenchError("boom")
        )
        dittobench.last_details = None
        platform = _platform_with_ledger(jobs=[], ledger=[])
        platform.get_artifact = AsyncMock(
            return_value=ArtifactResponse(
                agent_id=agent_id,
                sha256="ab" * 32,
                download_url="https://signed.example/x.tar.gz",
                expires_at=datetime.now(UTC),
            )
        )
        w = await self._worker(dittobench, platform)

        out = await w._confirm_and_submit(
            agent_id, "ab" * 32, "5Miner" + "x" * 42, seeds=[10, 20, 30]
        )
        assert out is None
        platform.submit_score.assert_not_awaited()
