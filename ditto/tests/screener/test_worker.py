"""Tests for the screener sweep loop (fakes for platform + gate)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.screener import ScreenerQueueItem, ScreenerQueueResponse
from ditto.api_models.validator import ArtifactResponse
from ditto.screener.config import ScreenerConfig
from ditto.screener.errors import PlatformError
from ditto.screener.gate import GateResult
from ditto.screener.worker import ScreenerWorker

_MINER = "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"


def _item(agent_id: UUID) -> ScreenerQueueItem:
    return ScreenerQueueItem(
        agent_id=agent_id,
        miner_hotkey=_MINER,
        name="a",
        sha256="de" * 32,
        status=AgentStatus.UPLOADED,
        created_at=datetime.now(UTC),
    )


class _FakeKeypair:
    def sign(self, _message: bytes) -> bytes:
        return b"\xcd" * 64


class _FakeGate:
    def __init__(self, result: GateResult) -> None:
        self.result = result
        self.calls: list[UUID] = []

    async def screen(self, *, agent_id: UUID, **_: Any) -> GateResult:
        self.calls.append(agent_id)
        return self.result


class _FakePlatform:
    def __init__(self, queues: list[list[ScreenerQueueItem]]) -> None:
        self._queues = queues
        self.verdicts: list[dict] = []
        self.submit_error: Exception | None = None
        self.stop_after_queue: asyncio.Event | None = None

    async def get_queue(self) -> ScreenerQueueResponse:
        items = self._queues.pop(0) if self._queues else []
        # Signal the loop to stop once the queue has drained (first empty sweep),
        # AFTER the item-bearing sweeps have been served + processed.
        if self.stop_after_queue is not None and not items:
            self.stop_after_queue.set()
        return ScreenerQueueResponse(items=items, count=len(items))

    async def get_artifact(self, agent_id: UUID) -> ArtifactResponse:
        return ArtifactResponse(
            agent_id=agent_id,
            sha256="de" * 32,
            download_url="https://storage.test/a.tar.gz",
            expires_at=datetime.now(UTC),
        )

    async def submit_result(  # type: ignore[no-untyped-def]
        self, agent_id, *, signature, passed, policy_version, detail=""
    ):
        if self.submit_error is not None:
            raise self.submit_error
        self.verdicts.append(
            {
                "agent_id": agent_id,
                "signature": signature,
                "passed": passed,
                "policy_version": policy_version,
                "detail": detail,
            }
        )

        class _R:
            status = type(
                "S", (), {"value": "evaluating" if passed else "screening_failed"}
            )()

        return _R()


def _worker(cfg: ScreenerConfig, platform, gate) -> ScreenerWorker:  # type: ignore[no-untyped-def]
    return ScreenerWorker(
        config=cfg, platform=platform, gate=gate, keypair=_FakeKeypair()
    )


async def test_screen_one_pass_posts_signed_pass_verdict(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    agent = uuid4()
    platform = _FakePlatform([])
    gate = _FakeGate(GateResult(True, ""))
    worker = _worker(make_config(), platform, gate)
    await worker._screen_one(_item(agent))
    assert gate.calls == [agent]
    assert len(platform.verdicts) == 1
    v = platform.verdicts[0]
    assert v["passed"] is True and v["signature"] == "cd" * 64 and v["detail"] == ""
    assert v["policy_version"] == 2


async def test_screen_one_fail_forwards_detail(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    platform = _FakePlatform([])
    gate = _FakeGate(GateResult(False, "build failed: E0432"))
    worker = _worker(make_config(), platform, gate)
    await worker._screen_one(_item(uuid4()))
    v = platform.verdicts[0]
    assert v["passed"] is False and "E0432" in v["detail"]


async def test_verdict_platform_error_swallowed(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    platform = _FakePlatform([])
    platform.submit_error = PlatformError("409 conflict")
    gate = _FakeGate(GateResult(True, ""))
    worker = _worker(make_config(), platform, gate)
    # Must not raise (a 409/late verdict is logged and skipped).
    await worker._screen_one(_item(uuid4()))
    assert platform.verdicts == []


async def test_run_forever_drains_queue_then_stops(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    a1, a2 = uuid4(), uuid4()
    # First sweep has two agents; the second (empty) sweep trips the stop.
    platform = _FakePlatform([[_item(a1), _item(a2)], []])
    stop = asyncio.Event()
    platform.stop_after_queue = stop  # set on the first empty sweep
    gate = _FakeGate(GateResult(True, ""))
    worker = _worker(make_config(), platform, gate)
    await asyncio.wait_for(worker.run_forever(stop), timeout=2.0)
    assert gate.calls == [a1, a2]
    assert {v["agent_id"] for v in platform.verdicts} == {a1, a2}


async def test_run_forever_exits_immediately_when_stopped(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    platform = _FakePlatform([])
    gate = _FakeGate(GateResult(True, ""))
    worker = _worker(make_config(), platform, gate)
    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(worker.run_forever(stop), timeout=2.0)
    assert gate.calls == []
