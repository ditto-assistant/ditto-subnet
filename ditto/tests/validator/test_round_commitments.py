"""The validator's generator-round commitment lane.

Platform elects a few validators per committed round to publish
``dgr1:{round}:{commitment}`` on chain. The lane publishes the exact payload,
reads it back, and reports the block; every failure is logged, reported when
possible, and never raised. It runs as its own task, so a hung Pylon call
cannot hold the weight loop.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ditto.api_models.generator_round_validator import (
    GeneratorRoundCommitmentJobResponse,
    round_commitment_payload,
)
from ditto.chain import ChainError
from ditto.tests.validator.test_worker import _BURN_HOTKEY, _config
from ditto.validator import worker as worker_mod
from ditto.validator.platform import PlatformError
from ditto.validator.round_commitments import RoundCommitmentPublisher
from ditto.validator.worker import ValidatorWorker

_HOTKEY = "5Validator" + "x" * 38
_COMMITMENT = "ab" * 32


def _job(round_index: int = 7) -> GeneratorRoundCommitmentJobResponse:
    return GeneratorRoundCommitmentJobResponse(
        round_index=round_index,
        commitment=_COMMITMENT,
        payload=round_commitment_payload(round_index, _COMMITMENT),
        claim_expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )


def _payload(round_index: int = 7) -> bytes:
    return f"dgr1:{round_index}:{_COMMITMENT}".encode()


def _platform(job: Any = None) -> MagicMock:
    platform = MagicMock()
    platform.request_generator_round_commitment_job = AsyncMock(
        return_value=_job() if job is None else job
    )
    platform.report_generator_round_commitment = AsyncMock()
    return platform


def _chain(readbacks: list[Any] | None = None) -> MagicMock:
    chain = MagicMock()
    chain.has_identity = MagicMock(return_value=True)
    chain.set_commitment = AsyncMock()
    chain.get_own_commitment = AsyncMock(
        side_effect=readbacks if readbacks is not None else [(4242, _payload())]
    )
    return chain


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def _publisher(platform: Any, chain: Any, **kwargs: Any) -> RoundCommitmentPublisher:
    clock = _Clock()
    return RoundCommitmentPublisher(
        platform,
        chain,
        _HOTKEY,
        readback_timeout=30.0,
        readback_interval=6.0,
        sleep=clock.sleep,
        clock=clock,
        **kwargs,
    )


async def test_publishes_the_exact_payload_and_reports_the_block() -> None:
    platform = _platform()
    chain = _chain([None, (4100, b"dgr1:6:" + b"cd" * 32), (4242, _payload())])
    assert await _publisher(platform, chain).run_once() == "confirmed"
    chain.set_commitment.assert_awaited_once_with(_payload())
    assert chain.get_own_commitment.await_count == 3
    platform.report_generator_round_commitment.assert_awaited_once()
    args = platform.report_generator_round_commitment.await_args
    assert args.kwargs["outcome"] == "confirmed"
    assert args.kwargs["block_number"] == 4242


async def test_no_job_publishes_nothing() -> None:
    platform = _platform()
    platform.request_generator_round_commitment_job = AsyncMock(return_value=None)
    chain = _chain()
    assert await _publisher(platform, chain).run_once() == "idle"
    chain.set_commitment.assert_not_awaited()


async def test_set_commitment_failure_is_reported_and_never_raised() -> None:
    platform = _platform()
    chain = _chain()
    chain.set_commitment = AsyncMock(side_effect=ChainError("pylon rejected it"))
    assert await _publisher(platform, chain).run_once() == "failed"
    args = platform.report_generator_round_commitment.await_args
    assert args.kwargs["outcome"] == "failed"
    assert "pylon rejected it" in args.kwargs["detail"]


async def test_a_value_never_seen_on_chain_is_a_failure() -> None:
    platform = _platform()
    chain = _chain()
    chain.get_own_commitment = AsyncMock(side_effect=RuntimeError("read failed"))
    assert await _publisher(platform, chain).run_once() == "failed"
    detail = platform.report_generator_round_commitment.await_args.kwargs["detail"]
    assert "not observed on chain" in detail


async def test_platform_and_report_failures_are_non_fatal() -> None:
    platform = _platform()
    platform.request_generator_round_commitment_job = AsyncMock(
        side_effect=PlatformError("platform down")
    )
    assert await _publisher(platform, _chain()).run_once() == "error"

    platform = _platform()
    platform.report_generator_round_commitment = AsyncMock(
        side_effect=PlatformError("report lost")
    )
    # The value is on chain; a lost report only means Platform re-elects.
    assert await _publisher(platform, _chain()).run_once() == "confirmed"


async def test_refuses_any_payload_but_the_round_commitment() -> None:
    forged = GeneratorRoundCommitmentJobResponse.model_construct(
        round_index=7,
        commitment=_COMMITMENT,
        payload="not-a-round-commitment",
        claim_expires_at=datetime.now(UTC),
    )
    platform = _platform(forged)
    chain = _chain()
    assert await _publisher(platform, chain).run_once() == "failed"
    chain.set_commitment.assert_not_awaited()
    assert (
        platform.report_generator_round_commitment.await_args.kwargs["outcome"]
        == "failed"
    )


def test_job_model_rejects_a_payload_that_is_not_the_commitment() -> None:
    with pytest.raises(ValueError, match="not this round's commitment"):
        GeneratorRoundCommitmentJobResponse(
            round_index=7,
            commitment=_COMMITMENT,
            payload=f"dgr1:8:{_COMMITMENT}",
            claim_expires_at=datetime.now(UTC),
        )


def test_supported_needs_an_identity_and_the_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chain = _chain()
    assert _publisher(_platform(), chain).supported()
    chain.has_identity = MagicMock(return_value=False)
    assert not _publisher(_platform(), chain).supported()
    assert not _publisher(_platform(), None).supported()
    monkeypatch.setenv("VALIDATOR_ROUND_COMMITMENTS", "false")
    assert not _publisher(_platform(), _chain()).supported()


async def test_unsupported_host_never_asks_platform() -> None:
    platform = _platform()
    assert await _publisher(platform, None).run_once() == "unsupported"
    platform.request_generator_round_commitment_job.assert_not_awaited()


async def test_a_hung_publication_never_holds_the_weight_loop() -> None:
    stop = asyncio.Event()
    publishing = asyncio.Event()
    platform = _platform()
    chain = _chain()

    async def _hang(_data: bytes) -> None:
        publishing.set()
        await asyncio.Event().wait()

    chain.set_commitment = AsyncMock(side_effect=_hang)
    worker = ValidatorWorker(
        config=_config(),
        platform=platform,
        dittobench=MagicMock(),
        chain=chain,
        keypair=MagicMock(),
        telemetry=MagicMock(),
    )
    scoring = asyncio.Event()

    async def _idle_scoring(*, set_weights: bool) -> int:
        assert set_weights is False
        await scoring.wait()
        return 0

    worker.run_once = AsyncMock(side_effect=_idle_scoring)  # type: ignore[method-assign]
    worker._chain_min_epoch_seconds = AsyncMock(return_value=0.0)  # type: ignore[method-assign]
    worker._seconds_until_weight_window = AsyncMock(return_value=0.0)  # type: ignore[method-assign]
    worker._update_weights = AsyncMock(  # type: ignore[method-assign]
        return_value=worker_mod._WeightOutcome(
            weights={_BURN_HOTKEY: 1.0}, submitted=True
        )
    )
    worker._observe_onchain_weight_state = AsyncMock(return_value=(100, 110))  # type: ignore[method-assign]
    worker._report_heartbeat = AsyncMock()  # type: ignore[method-assign]

    task = asyncio.create_task(worker.run_forever(stop))
    await asyncio.wait_for(publishing.wait(), timeout=1.0)
    for _ in range(50):
        if worker._update_weights.await_count:
            break
        await asyncio.sleep(0)
    assert worker._update_weights.await_count == 1
    scoring.set()
    stop.set()
    # Stopping cancels the hung publication instead of waiting on it.
    await asyncio.wait_for(task, timeout=1.0)
