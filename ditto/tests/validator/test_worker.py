"""Unit tests for the validator worker sweep + weight computation."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.validator import (
    ArtifactResponse,
    ScoreReport,
    SubmitScoreResponse,
    ValidatorQueueItem,
    ValidatorQueueResponse,
)
from ditto.validator.weights import compute_weights
from ditto.validator.worker import ValidatorWorker

_VALIDATOR_HOTKEY = "5CZq6MdanxF3j8ACp8oVtiaphTeyrA7QFPU92ke2jEFzK1mp"


class TestComputeWeights:
    def test_drops_non_positive_and_keeps_positive(self) -> None:
        weights = compute_weights({"a": 0.8, "b": 0.0, "c": 0.4})
        assert weights == {"a": 0.8, "c": 0.4}

    def test_all_zero_returns_empty(self) -> None:
        assert compute_weights({"a": 0.0, "b": 0.0}) == {}


def _queue_item(miner_hotkey: str, name: str) -> ValidatorQueueItem:
    return ValidatorQueueItem(
        agent_id=uuid4(),
        miner_hotkey=miner_hotkey,
        name=name,
        sha256="ab" * 32,
        status=AgentStatus.EVALUATING,
        created_at=datetime.now(UTC),
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
    )


def _config() -> MagicMock:
    cfg = MagicMock()
    cfg.validator_hotkey = _VALIDATOR_HOTKEY
    return cfg


class TestRunOnce:
    async def test_scores_each_agent_and_sets_weights(self) -> None:
        item_a = _queue_item("5MinerA" + "x" * 41, "alpha")
        item_b = _queue_item("5MinerB" + "x" * 41, "beta")

        platform = MagicMock()
        platform.get_queue = AsyncMock(
            return_value=ValidatorQueueResponse(items=[item_a, item_b], count=2)
        )
        platform.get_artifact = AsyncMock(
            return_value=ArtifactResponse(
                agent_id=item_a.agent_id,
                sha256="ab" * 32,
                download_url="https://signed.example/x.tar.gz?sig=1",
                expires_at=datetime.now(UTC),
            )
        )
        platform.submit_score = AsyncMock(
            return_value=SubmitScoreResponse(
                agent_id=item_a.agent_id, status=AgentStatus.SCORED, accepted=True
            )
        )

        composites = iter([0.9, 0.4])
        dittobench = MagicMock()
        dittobench.score_tarball = AsyncMock(
            side_effect=lambda **_: _report("run", next(composites))
        )

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
        assert platform.submit_score.await_count == 2
        chain.put_weights.assert_awaited_once_with(
            {item_a.miner_hotkey: 0.9, item_b.miner_hotkey: 0.4}
        )

    async def test_empty_queue_skips_weights(self) -> None:
        platform = MagicMock()
        platform.get_queue = AsyncMock(
            return_value=ValidatorQueueResponse(items=[], count=0)
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

        assert await worker.run_once() == 0
        chain.put_weights.assert_not_awaited()

    async def test_one_agent_failure_does_not_block_others(self) -> None:
        from ditto.validator.errors import DittobenchError

        good = _queue_item("5MinerG" + "x" * 41, "good")
        bad = _queue_item("5MinerB" + "x" * 41, "bad")

        platform = MagicMock()
        platform.get_queue = AsyncMock(
            return_value=ValidatorQueueResponse(items=[bad, good], count=2)
        )
        platform.get_artifact = AsyncMock(
            return_value=ArtifactResponse(
                agent_id=good.agent_id,
                sha256="ab" * 32,
                download_url="https://signed.example/x.tar.gz?sig=1",
                expires_at=datetime.now(UTC),
            )
        )
        platform.submit_score = AsyncMock(
            return_value=SubmitScoreResponse(
                agent_id=good.agent_id, status=AgentStatus.SCORED, accepted=True
            )
        )

        async def _score(**_: object) -> ScoreReport:
            # First call (bad) raises; second (good) succeeds.
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
        # Only the good agent's miner is in the weight vector.
        chain.put_weights.assert_awaited_once_with({good.miner_hotkey: 0.7})
