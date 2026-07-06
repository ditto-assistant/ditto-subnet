"""Unit tests for the validator worker sweep + KOTH+ATH weight computation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.validator import (
    ArtifactResponse,
    LedgerEntry,
    LedgerResponse,
    ScoreReport,
    SubmitScoreResponse,
    ValidatorQueueItem,
    ValidatorQueueResponse,
)
from ditto.chain import ChainError
from ditto.validator import worker as worker_mod
from ditto.validator.weights import compute_weights
from ditto.validator.worker import ValidatorWorker

_VALIDATOR_HOTKEY = "5CZq6MdanxF3j8ACp8oVtiaphTeyrA7QFPU92ke2jEFzK1mp"
_T0 = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)


def _entry(
    miner: str,
    composite: float,
    *,
    first_seen: datetime = _T0,
    agent_id: UUID | None = None,
) -> LedgerEntry:
    return LedgerEntry(
        miner_hotkey=miner,
        agent_id=agent_id or uuid4(),
        composite=composite,
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
        structural_fingerprint=None,
    )


def _config() -> MagicMock:
    cfg = MagicMock()
    cfg.validator_hotkey = _VALIDATOR_HOTKEY
    cfg.koth_margin = 0.01
    cfg.koth_tail_size = 4
    cfg.koth_champion_share = 0.9
    return cfg


def _platform_with_ledger(
    *, items: list[ValidatorQueueItem], ledger: list[LedgerEntry]
) -> MagicMock:
    platform = MagicMock()
    platform.get_queue = AsyncMock(
        return_value=ValidatorQueueResponse(items=items, count=len(items))
    )
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
        item = _queue_item("5MinerA" + "x" * 41, "alpha")
        # The weight vector comes from the LEDGER, not the swept composites.
        ledger = [_entry("5MinerA" + "x" * 41, 0.9)]
        platform = _platform_with_ledger(items=[item], ledger=ledger)

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
        assert platform.submit_score.await_count == 1
        chain.put_weights.assert_awaited_once_with({"5MinerA" + "x" * 41: 0.9})

    async def test_forwards_tarball_sha_to_scorer(self) -> None:
        # The registered digest must be forwarded so dittobench re-verifies the
        # fetched bytes and pins the build tag to the content hash.
        item = _queue_item("5MinerA" + "x" * 41, "alpha")
        platform = _platform_with_ledger(
            items=[item], ledger=[_entry("5MinerA" + "x" * 41, 0.9)]
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

    async def test_sha_mismatch_skips_agent(self) -> None:
        # If the queue item and artifact disagree on the digest, the agent is
        # skipped (never scored), but the sweep continues and weights still come
        # from the durable ledger.
        item = _queue_item("5MinerA" + "x" * 41, "alpha")
        platform = _platform_with_ledger(
            items=[item], ledger=[_entry("5Champ" + "x" * 42, 0.85)]
        )
        # Artifact reports a DIFFERENT sha than the queue item's "ab" * 32.
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
        chain.put_weights.assert_awaited_once_with({"5Champ" + "x" * 42: 0.9})

    async def test_empty_queue_still_sets_weights_from_ledger(self) -> None:
        # The regression that broke incentives: an empty queue must NOT skip
        # weight-setting, or the reigning champion is zeroed.
        ledger = [_entry("5Champion" + "x" * 39, 0.85)]
        platform = _platform_with_ledger(items=[], ledger=ledger)
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
        chain.put_weights.assert_awaited_once_with({"5Champion" + "x" * 39: 0.9})

    async def test_empty_ledger_skips_weights(self) -> None:
        platform = _platform_with_ledger(items=[], ledger=[])
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

    async def test_one_agent_failure_does_not_block_weights(self) -> None:
        from ditto.validator.errors import DittobenchError

        bad = _queue_item("5MinerB" + "x" * 41, "bad")
        good = _queue_item("5MinerG" + "x" * 41, "good")
        ledger = [_entry("5MinerG" + "x" * 41, 0.7)]
        platform = _platform_with_ledger(items=[bad, good], ledger=ledger)

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
        # The good miner is the lone eligible entry => champion share (0.9).
        chain.put_weights.assert_awaited_once_with({"5MinerG" + "x" * 41: 0.9})

    async def test_ledger_fetch_failure_leaves_weights_untouched(self) -> None:
        from ditto.validator.errors import PlatformError

        platform = _platform_with_ledger(items=[], ledger=[])
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
        platform = _platform_with_ledger(items=[], ledger=ledger)
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
        platform = _platform_with_ledger(items=[], ledger=ledger)
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
