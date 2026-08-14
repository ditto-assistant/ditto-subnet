"""Unit tests for the validator worker sweep + KOTH+ATH weight computation."""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast, get_args
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.benchmark_capacity import ActiveBenchmarkSlot
from ditto.api_models.benchmark_progress import (
    MAX_BENCHMARK_CHECKS,
    BenchmarkProgressStage,
)
from ditto.api_models.inference import InferenceExchangeResponse, InferenceGrantOffer
from ditto.api_models.stack_health import (
    ValidatorComponentHealth,
    ValidatorStackHealth,
)
from ditto.api_models.system_health import DockerHealth, SystemMetrics
from ditto.api_models.validator import (
    ArtifactResponse,
    ConfirmationDatasetPin,
    FailJobResponse,
    JobResponse,
    LedgerEntry,
    LedgerResponse,
    ScoreReport,
    SubmitScoreResponse,
    ValidatorHeartbeatResponse,
)
from ditto.api_models.validator_capabilities import (
    InferenceCalibrationRoute,
    ScorerBenchmarkCapability,
    ScorerLivenessProbe,
    V7InferenceCalibration,
)
from ditto.chain import ChainError
from ditto.validator import worker as worker_mod
from ditto.validator.build_info import HEARTBEAT_PROTOCOL_VERSION
from ditto.validator.dittobench import (
    DittobenchProgressSnapshot,
    InferenceBrokerSession,
)
from ditto.validator.errors import (
    DittobenchError,
    PlatformError,
    PlatformInfrastructureError,
    SandboxOomError,
    ValidatorInfrastructureError,
)
from ditto.validator.onchain_seed import derive_seed
from ditto.validator.resource_gate import (
    DEFAULT_RESOURCE_CEILINGS,
    ResourceCeilings,
)
from ditto.validator.stack_health import fallback_stack_health
from ditto.validator.weights import (
    apply_miner_emission_cap,
    compute_weights,
    filter_weight_confirmed,
    resolve_miner_emission_share,
)
from ditto.validator.worker import ValidatorWorker

_VALIDATOR_HOTKEY = "5CZq6MdanxF3j8ACp8oVtiaphTeyrA7QFPU92ke2jEFzK1mp"
_BURN_HOTKEY = "5Burn" + "x" * 43
_T0 = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)


def _compose_default_benchmark_capacity() -> int:
    """Read the shipped slot default out of docker-compose.yml.

    Deriving it here rather than restating it keeps the slot tests honest: the
    fleet runs the compose default, so a default that drifts away from the
    behaviour these tests assert is exactly the regression worth catching.
    """
    compose = Path(__file__).parents[3] / "docker-compose.yml"
    pattern = re.compile(
        r"VALIDATOR_BENCHMARK_CAPACITY:\s*\$\{VALIDATOR_BENCHMARK_CAPACITY:-(\d+)\}"
    )
    match = pattern.search(compose.read_text())
    assert match is not None, "compose must ship a VALIDATOR_BENCHMARK_CAPACITY default"
    return int(match.group(1))


_COMPOSE_DEFAULT_BENCHMARK_CAPACITY = _compose_default_benchmark_capacity()


class _ImmediateHeartbeatClock:
    """Advance requested sleeps immediately so worker tests stay deterministic."""

    def __init__(self) -> None:
        self.now = 1_000_000.25

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await asyncio.sleep(0)


class _ControlledHeartbeatClock(worker_mod._HeartbeatClock):
    """Hold the coalesced flush until a test advances the wall clock."""

    def __init__(self) -> None:
        self.now = 1_000_000.25
        self.sleep_started = asyncio.Event()
        self._release_sleep = asyncio.Event()

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        target = self.now + seconds
        self.sleep_started.set()
        await self._release_sleep.wait()
        self.now = max(self.now, target)

    def advance_to_next_second(self) -> None:
        self.now = int(self.now) + 1.25
        self._release_sleep.set()


@pytest.fixture(autouse=True)
def _fast_heartbeat_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_mod, "_new_heartbeat_clock", _ImmediateHeartbeatClock)


def _entry(
    miner: str,
    composite: float,
    *,
    first_seen: datetime = _T0,
    agent_id: UUID | None = None,
    n: int = 128,
    bench_version: int | None = None,
    composite_stderr: float | None = None,
    confirmation_composites: list[float] | None = None,
    confirmation_seeds: list[int] | None = None,
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
        bench_version=bench_version,
        composite_stderr=composite_stderr,
        confirmation_composites=confirmation_composites,
        confirmation_seeds=confirmation_seeds,
        signature="ab" * 64,
        score_proofs=[],
        status=AgentStatus.SCORED,
    )


# Mixed value types (float margin, int tail_size, tuple shares), so type as Any
# to unpack cleanly into compute_weights' parameters under `mypy ditto/`.
_KOTH: dict[str, Any] = {
    "margin": 0.01,
    "tail_size": 4,
    "rank_shares": (0.65, 0.14, 0.10, 0.07, 0.04),
}


class TestComputeWeights:
    def test_empty_ledger_returns_empty(self) -> None:
        assert compute_weights([], **_KOTH) == {}

    def test_all_zero_returns_empty(self) -> None:
        assert compute_weights([_entry("a", 0.0), _entry("b", 0.0)], **_KOTH) == {}

    def test_single_miner_takes_all(self) -> None:
        # No runners-up: the champion is the whole vector.
        assert compute_weights([_entry("a", 0.8)], **_KOTH) == {"a": 0.65}

    def test_preconfirmation_fold_filters_v9_per_entry(self) -> None:
        v8 = _entry("v8", 0.7, bench_version=8)
        v9 = _entry("v9", 0.9, bench_version=9)

        assert filter_weight_confirmed([v8, v9]) == [v8]

    def test_shadow_fold_pays_ordinary_v9_quorum(self) -> None:
        v8 = _entry("v8", 0.7, bench_version=8)
        v9 = _entry("v9", 0.9, bench_version=9)

        assert filter_weight_confirmed([v8, v9], enforce=False) == [v8, v9]

    def test_preconfirmation_fold_rejects_v9_only_pool(self) -> None:
        assert filter_weight_confirmed([_entry("v9", 0.9, bench_version=9)]) == []

    def test_v9_receipt_field_activates_when_later_layer_adds_it(self) -> None:
        receipt_bearing_v9 = _entry("v9", 0.9, bench_version=9).model_copy(
            update={"v9_confirmation": object()}
        )

        assert filter_weight_confirmed([receipt_bearing_v9]) == [receipt_bearing_v9]

    def test_preconfirmation_fold_fails_closed_for_unknown_future_version(self) -> None:
        assert filter_weight_confirmed([_entry("future", 0.9, bench_version=10)]) == []

    def test_champion_and_tail_split(self) -> None:
        entries = [
            _entry("champ", 0.90, first_seen=_T0),
            _entry("r1", 0.70, first_seen=_T0 + timedelta(minutes=1)),
            _entry("r2", 0.50, first_seen=_T0 + timedelta(minutes=2)),
        ]
        w = compute_weights(entries, **_KOTH)
        assert w["champ"] == pytest.approx(0.65)
        assert w["r1"] == pytest.approx(0.14)
        assert w["r2"] == pytest.approx(0.10)

    def test_full_emission_set_uses_ranked_distribution(self) -> None:
        entries = [
            _entry(
                f"m{i}",
                0.90 - i * 0.01,
                first_seen=_T0 + timedelta(minutes=i),
            )
            for i in range(5)
        ]
        assert compute_weights(entries, **_KOTH) == pytest.approx(
            {"m0": 0.65, "m1": 0.14, "m2": 0.10, "m3": 0.07, "m4": 0.04}
        )

    def test_ceiling_deadlock_pays_the_full_uncapped_best_score_cohort(self) -> None:
        entries = [
            _entry("champ", 0.996348, first_seen=_T0),
            *[
                _entry(
                    f"tied-{index}",
                    0.997012,
                    first_seen=_T0 + timedelta(minutes=index + 1),
                )
                for index in range(5)
            ],
        ]

        weights = compute_weights(entries, **_KOTH, tie_pooling=True)

        assert "champ" not in weights
        assert weights == pytest.approx({f"tied-{index}": 0.20 for index in range(5)})

    def test_attainable_crown_keeps_ranked_slot_pooling(self) -> None:
        entries = [
            _entry("champ", 0.90, first_seen=_T0),
            *[
                _entry(
                    f"tied-{index}",
                    0.905,
                    first_seen=_T0 + timedelta(minutes=index + 1),
                )
                for index in range(4)
            ],
        ]

        weights = compute_weights(entries, **_KOTH, tie_pooling=True)

        assert weights["champ"] == pytest.approx(0.65)
        for index in range(4):
            assert weights[f"tied-{index}"] == pytest.approx(0.0875)

    def test_exact_tie_with_champion_pools_every_occupied_slot(self) -> None:
        entries = [
            _entry(f"m{index}", 0.997012, first_seen=_T0 + timedelta(minutes=index))
            for index in range(5)
        ]

        assert compute_weights(entries, **_KOTH, tie_pooling=True) == pytest.approx(
            {f"m{index}": 0.20 for index in range(5)}
        )

    def test_non_exact_tie_requires_shared_seed_evidence(self) -> None:
        entries = [
            _entry("champ", 0.900, first_seen=_T0, composite_stderr=0.2),
            _entry(
                "challenger",
                0.905,
                first_seen=_T0 + timedelta(minutes=1),
                composite_stderr=0.2,
            ),
        ]

        weights = compute_weights(entries, **_KOTH, dethrone_z=1.64, tie_pooling=True)

        assert weights == pytest.approx({"champ": 0.65, "challenger": 0.14})

    def test_shared_seed_statistical_tie_pools_non_exact_scores(self) -> None:
        entries = [
            _entry(
                "champ",
                0.900,
                first_seen=_T0,
                confirmation_seeds=[1, 2, 3],
                confirmation_composites=[0.88, 0.90, 0.92],
            ),
            _entry(
                "challenger",
                0.905,
                first_seen=_T0 + timedelta(minutes=1),
                confirmation_seeds=[1, 2, 3],
                confirmation_composites=[0.89, 0.895, 0.93],
            ),
        ]

        weights = compute_weights(entries, **_KOTH, dethrone_z=1.64, tie_pooling=True)

        assert weights == pytest.approx({"champ": 0.395, "challenger": 0.395})

    def test_tie_groups_do_not_chain_from_one_neighbor_to_the_next(self) -> None:
        entries = [
            _entry("champ", 0.90, first_seen=_T0),
            _entry("a", 0.80, first_seen=_T0 + timedelta(minutes=1)),
            _entry("b", 0.80, first_seen=_T0 + timedelta(minutes=2)),
            _entry("c", 0.79, first_seen=_T0 + timedelta(minutes=3)),
        ]

        weights = compute_weights(entries, **_KOTH, tie_pooling=True)

        assert weights == pytest.approx(
            {"champ": 0.65, "a": 0.12, "b": 0.12, "c": 0.07}
        )

    def test_duplicate_hotkey_cannot_claim_multiple_tail_slots(self) -> None:
        entries = [
            _entry("champ", 0.90, first_seen=_T0),
            _entry("same", 0.80, first_seen=_T0 + timedelta(minutes=1)),
            _entry("same", 0.79, first_seen=_T0 + timedelta(minutes=2)),
            _entry("next", 0.70, first_seen=_T0 + timedelta(minutes=3)),
        ]

        assert compute_weights(entries, **_KOTH, tie_pooling=True) == pytest.approx(
            {"champ": 0.65, "same": 0.14, "next": 0.10}
        )

    @pytest.mark.parametrize(
        "rank_shares",
        [
            (),
            (0.65,),
            (0.65, 0.14, 0.10, 0.07, 0.0),
            (0.65, 0.14, float("nan"), 0.07, 0.04),
            (0.65, 0.14, 0.10, 0.07, 0.03),
        ],
    )
    def test_rejects_invalid_rank_distribution(
        self, rank_shares: tuple[float, ...]
    ) -> None:
        with pytest.raises(ValueError):
            compute_weights(
                [_entry("champ", 0.9)],
                margin=0.01,
                tail_size=4,
                rank_shares=rank_shares,
            )

    def test_sub_margin_challenger_does_not_dethrone(self) -> None:
        # 'first' created earlier; 'second' beats it by less than the fixed 0.01
        # composite-point margin, so the incumbent keeps the crown.
        first = _entry("first", 0.800, first_seen=_T0)
        second = _entry("second", 0.805, first_seen=_T0 + timedelta(minutes=1))
        w = compute_weights([first, second], **_KOTH)
        assert w["first"] == pytest.approx(0.65)
        assert w["second"] == pytest.approx(0.14)

    def test_over_margin_challenger_dethrones(self) -> None:
        first = _entry("first", 0.80, first_seen=_T0)
        second = _entry("second", 0.90, first_seen=_T0 + timedelta(minutes=1))
        w = compute_weights([first, second], **_KOTH)
        assert w["second"] == pytest.approx(0.65)
        assert w["first"] == pytest.approx(0.14)

    def test_scoring_order_independent(self) -> None:
        # Same entries in a different list order must crown the same champion:
        # the fold sorts by first_seen, not input order.
        a = _entry("a", 0.80, first_seen=_T0)
        b = _entry("b", 0.807, first_seen=_T0 + timedelta(minutes=1))
        c = _entry("c", 0.811, first_seen=_T0 + timedelta(minutes=2))
        forward = compute_weights([a, b, c], **_KOTH)
        shuffled = compute_weights([c, a, b], **_KOTH)
        assert forward == shuffled
        # a=0.80 -> b 0.807 !> 0.81 (no) -> c 0.811 > 0.81 (yes) => champ c.
        assert forward["c"] == pytest.approx(0.65)

    def test_tail_smaller_than_available(self) -> None:
        entries = [_entry(f"m{i}", 0.9 - i * 0.1, first_seen=_T0) for i in range(6)]
        w = compute_weights(
            entries, margin=0.01, tail_size=2, rank_shares=(0.65, 0.21, 0.14)
        )
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

    def test_full_share_leaves_no_burn_weight(self) -> None:
        # The deployed share: miners take everything, the burn hotkey is absent.
        capped = apply_miner_emission_cap(
            {"champ": 0.9, "tail": 0.1},
            miner_share=1.0,
            burn_hotkey=_BURN_HOTKEY,
        )
        assert capped == {"champ": pytest.approx(0.9), "tail": pytest.approx(0.1)}
        assert _BURN_HOTKEY not in capped

    def test_full_share_still_burns_an_empty_ledger(self) -> None:
        assert apply_miner_emission_cap(
            {}, miner_share=1.0, burn_hotkey=_BURN_HOTKEY
        ) == {_BURN_HOTKEY: 1.0}

    def test_burn_hotkey_cannot_enter_miner_pool(self) -> None:
        assert apply_miner_emission_cap(
            {_BURN_HOTKEY: 0.9, "miner": 0.1},
            miner_share=0.2,
            burn_hotkey=_BURN_HOTKEY,
        ) == {"miner": pytest.approx(0.2), _BURN_HOTKEY: pytest.approx(0.8)}


class TestPlatformBurnShare:
    """The burn is operator policy on the ledger, with the constant as fallback."""

    def test_absent_field_keeps_the_compiled_default(self) -> None:
        # An older platform omits it; pydantic defaults it to 0.0, which is what
        # MINER_EMISSION_SHARE = 1.0 already folds. Omission is never a change.
        ledger = LedgerResponse(entries=[], count=0)
        assert resolve_miner_emission_share(ledger, default_share=1.0) == 1.0

    def test_served_share_wins_over_the_default(self) -> None:
        ledger = LedgerResponse(entries=[], count=0, burn_share=0.25)
        assert resolve_miner_emission_share(ledger, default_share=1.0) == pytest.approx(
            0.75
        )

    def test_full_burn_is_folded(self) -> None:
        ledger = LedgerResponse(entries=[], count=0, burn_share=1.0)
        assert resolve_miner_emission_share(ledger, default_share=1.0) == pytest.approx(
            0.0
        )

    @pytest.mark.parametrize(
        "burn_share",
        [None, "0.5", float("nan"), float("inf"), -0.1, 1.5, True],
        ids=["none", "string", "nan", "inf", "negative", "over-one", "bool"],
    )
    def test_anything_that_is_not_a_share_falls_back(self, burn_share: object) -> None:
        """A truncated or malformed ledger must degrade to the shipped behaviour,
        never to an arbitrary split. Constructed by hand rather than through the
        model because the model would already have rejected these."""
        malformed = cast("LedgerResponse", SimpleNamespace(burn_share=burn_share))
        assert resolve_miner_emission_share(malformed, default_share=1.0) == 1.0


class TestWeightFoldUsesPlatformBurn:
    async def _submitted_weights(self, ledger: LedgerResponse) -> dict[str, float]:
        platform = MagicMock()
        platform.get_ledger = AsyncMock(return_value=ledger)
        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=MagicMock(),
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )
        worker._registered_ledger_entries = AsyncMock(  # type: ignore[method-assign]
            return_value=list(ledger.entries)
        )
        worker._validator_permitted = AsyncMock(  # type: ignore[method-assign]
            return_value=True
        )
        worker._stake_sufficient = AsyncMock(return_value=True)  # type: ignore[method-assign]
        worker._log_commit_reveal_mode = AsyncMock()  # type: ignore[method-assign]
        worker._put_weights_with_retry = AsyncMock(  # type: ignore[method-assign]
            return_value=True
        )
        outcome = await worker._update_weights()
        assert outcome.submitted
        return outcome.weights

    async def test_default_ledger_pays_miners_everything(self) -> None:
        entries = [_entry("5Champ" + "x" * 42, 0.90), _entry("5Tail" + "x" * 43, 0.50)]
        weights = await self._submitted_weights(
            LedgerResponse(entries=entries, count=len(entries))
        )
        assert _BURN_HOTKEY not in weights
        assert sum(weights.values()) == pytest.approx(1.0)

    async def test_served_burn_scales_the_vector_without_reordering_it(self) -> None:
        """The operator dial must move the miner/burn split and nothing else:
        each miner keeps its share *of what miners receive*."""
        entries = [_entry("5Champ" + "x" * 42, 0.90), _entry("5Tail" + "x" * 43, 0.50)]
        uncapped = await self._submitted_weights(
            LedgerResponse(entries=entries, count=len(entries))
        )
        burned = await self._submitted_weights(
            LedgerResponse(entries=entries, count=len(entries), burn_share=0.4)
        )

        assert burned[_BURN_HOTKEY] == pytest.approx(0.4)
        assert sum(burned.values()) == pytest.approx(1.0)
        miners = {k: v for k, v in burned.items() if k != _BURN_HOTKEY}
        assert set(miners) == set(uncapped)
        for hotkey, weight in miners.items():
            assert weight == pytest.approx(uncapped[hotkey] * 0.6)

    async def test_enforce_unconfirmed_v9_row_cannot_freeze_v8_weight_update(
        self,
    ) -> None:
        v8 = _entry("5V8" + "x" * 44, 0.70, bench_version=8)
        v9 = _entry("5V9" + "x" * 44, 0.99, bench_version=9)
        ledger = LedgerResponse(
            entries=[v9, v8], count=2, v9_confirmation_mode="enforce"
        )
        platform = MagicMock()
        platform.get_ledger = AsyncMock(return_value=ledger)
        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=MagicMock(),
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )
        worker._registered_ledger_entries = AsyncMock(  # type: ignore[method-assign]
            side_effect=lambda entries: list(entries)
        )
        worker._validator_permitted = AsyncMock(  # type: ignore[method-assign]
            return_value=True
        )
        worker._stake_sufficient = AsyncMock(return_value=True)  # type: ignore[method-assign]
        worker._log_commit_reveal_mode = AsyncMock()  # type: ignore[method-assign]
        worker._put_weights_with_retry = AsyncMock(  # type: ignore[method-assign]
            return_value=True
        )

        outcome = await worker._update_weights()

        assert outcome.submitted
        assert outcome.weights == {v8.miner_hotkey: pytest.approx(1.0)}
        registered_call = worker._registered_ledger_entries.await_args
        assert registered_call is not None
        registered_input = registered_call.args[0]
        assert registered_input == [v8]
        worker._put_weights_with_retry.assert_awaited_once_with(  # type: ignore[attr-defined]
            {v8.miner_hotkey: pytest.approx(1.0)}
        )

    async def test_v9_only_enforce_ledger_preserves_existing_weights(self) -> None:
        v9 = _entry("5V9" + "x" * 44, 0.99, bench_version=9)
        platform = MagicMock()
        platform.get_ledger = AsyncMock(
            return_value=LedgerResponse(
                entries=[v9], count=1, v9_confirmation_mode="enforce"
            )
        )
        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=MagicMock(),
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )
        worker._registered_ledger_entries = AsyncMock()  # type: ignore[method-assign]
        worker._put_weights_with_retry = AsyncMock()  # type: ignore[method-assign]

        outcome = await worker._update_weights()

        assert not outcome.submitted
        assert outcome.weights == {}
        assert outcome.leaderboard == [(v9.miner_hotkey, v9.composite)]
        worker._registered_ledger_entries.assert_not_awaited()  # type: ignore[attr-defined]
        worker._put_weights_with_retry.assert_not_awaited()  # type: ignore[attr-defined]

    async def test_v9_only_shadow_ledger_restores_miner_weights(self) -> None:
        """Regression: off/shadow v9 is ordinary reward authority.

        Confirmation receipt filtering is activated by the ledger marker, not
        by the benchmark version alone.  Otherwise a completed v9 rollout turns
        every positive score into an empty vector and strands the old burn.
        """
        v9 = _entry("5V9" + "x" * 44, 0.99, bench_version=9)
        weights = await self._submitted_weights(
            LedgerResponse(
                entries=[v9],
                count=1,
                v9_confirmation_mode=None,
                burn_share=0,
            )
        )

        assert weights == {v9.miner_hotkey: pytest.approx(1.0)}
        assert _BURN_HOTKEY not in weights

    async def test_v9_enforce_without_receipts_preserves_existing_weights(self) -> None:
        v9 = _entry("5V9" + "x" * 44, 0.99, bench_version=9)
        platform = MagicMock()
        platform.get_ledger = AsyncMock(
            return_value=LedgerResponse(
                entries=[v9],
                count=1,
                v9_confirmation_mode="enforce",
            )
        )
        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=MagicMock(),
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )
        worker._registered_ledger_entries = AsyncMock()  # type: ignore[method-assign]
        worker._put_weights_with_retry = AsyncMock()  # type: ignore[method-assign]

        outcome = await worker._update_weights()

        assert not outcome.submitted
        worker._registered_ledger_entries.assert_not_awaited()  # type: ignore[attr-defined]
        worker._put_weights_with_retry.assert_not_awaited()  # type: ignore[attr-defined]


def _job(
    miner_hotkey: str,
    *,
    slot_id: str = "slot-0",
    sha256: str = "ab" * 32,
    dataset_sha256: str | None = "cd" * 32,
    deadline: datetime | None = None,
) -> JobResponse:
    return JobResponse(
        agent_id=uuid4(),
        miner_hotkey=miner_hotkey,
        sha256=sha256,
        deadline=deadline or datetime.now(UTC) + timedelta(hours=1),
        slot_id=slot_id,
        seed=12345,
        dataset_sha256=dataset_sha256,
        run_size="full",
        bench_version=8,
        minimum_screening_policy_version=9,
        requires_screened_image=True,
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
        bench_version=8,
    )


def _confirmation_pins(
    agent_id: UUID, *, bench_version: int = 8, count: int = 3
) -> list[ConfirmationDatasetPin]:
    return [
        ConfirmationDatasetPin(
            seed=seed,
            dataset_sha256=hashlib.sha256(str(seed).encode()).hexdigest(),
            run_size="full",
        )
        for seed in worker_mod.confirmation_seeds(
            [str(agent_id)], version=bench_version, count=count
        )
    ]


def _config() -> MagicMock:
    cfg = MagicMock()
    cfg.validator_hotkey = _VALIDATOR_HOTKEY
    cfg.netuid = 3
    cfg.koth_margin = 0.01
    cfg.koth_tail_size = 4
    cfg.koth_rank_shares = (0.65, 0.14, 0.10, 0.07, 0.04)
    cfg.koth_dethrone_z = 1.64
    cfg.koth_confirmation_seeds = 3
    cfg.top5_max_confirmation_seeds = 32
    cfg.top5_catch_up_rate = 2
    cfg.top5_max_cohort_size = 25
    cfg.miner_emission_share = 1.0
    cfg.burn_hotkey = _BURN_HOTKEY
    cfg.min_stake_tao = 0.0
    cfg.sweep_seconds = 120
    cfg.epoch_seconds = 3600
    cfg.queue_limit = 16
    cfg.dittobench_mock = True
    return cfg


def _platform_with_ledger(
    *,
    jobs: list[JobResponse],
    ledger: list[LedgerEntry],
    active_bench_version: int | None = 8,
) -> MagicMock:
    platform = MagicMock()
    platform.submit_heartbeat = AsyncMock(
        return_value=ValidatorHeartbeatResponse(
            accepted=True, seen_at=datetime.now(UTC)
        )
    )
    # Ticket poll: hand out one ticket per job, then 204 (None) to end the sweep.
    platform.request_job = AsyncMock(side_effect=[*jobs, None])
    platform.request_top5_confirmation_job = AsyncMock(
        side_effect=PlatformError("top-five lane unavailable")
    )
    platform.submit_top5_confirmation_score = AsyncMock()
    platform.get_ledger = AsyncMock(
        return_value=LedgerResponse(
            entries=ledger,
            active_bench_version=active_bench_version,
            count=len(ledger),
        )
    )
    platform.get_artifact = AsyncMock(
        return_value=ArtifactResponse(
            agent_id=uuid4(),
            sha256="ab" * 32,
            download_url="https://signed.example/x.tar.gz?sig=1",
            expires_at=datetime.now(UTC),
            screened_image_url="https://signed.example/x-image.tar?sig=1",
            screened_image_sha256="12" * 32,
            screened_image_size_bytes=123,
            screened_image_id="sha256:" + "34" * 32,
            screened_image_ref=(
                "ditto-screen/550e8400-e29b-41d4-a716-446655440000:latest"
            ),
            bench_version=8,
            screening_policy_version=9,
        )
    )
    platform.submit_score = AsyncMock(
        return_value=SubmitScoreResponse(
            agent_id=uuid4(), status=AgentStatus.SCORED, accepted=True
        )
    )
    platform.report_ticket_failed = AsyncMock(
        return_value=FailJobResponse(agent_id=uuid4(), reopened=True)
    )
    return platform


class TestTop5ConfirmationLane:
    async def test_cold_start_plans_v9_from_platform_ledger_authority(self) -> None:
        entry = _entry("5MinerV9" + "x" * 39, 0.9).model_copy(
            update={"bench_version": 9}
        )
        platform = _platform_with_ledger(
            jobs=[], ledger=[entry], active_bench_version=9
        )
        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=MagicMock(),
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )

        await worker._run_top5_confirmation_lane()

        platform.request_top5_confirmation_job.assert_awaited_once_with(
            champion_agent_id=entry.agent_id,
            member_agent_id=entry.agent_id,
        )

    async def test_older_ledger_without_authority_does_not_guess_v8(self) -> None:
        entry = _entry("5MinerV9" + "x" * 39, 0.9).model_copy(
            update={"bench_version": 9}
        )
        platform = _platform_with_ledger(
            jobs=[], ledger=[entry], active_bench_version=None
        )
        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=MagicMock(),
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )

        await worker._run_top5_confirmation_lane()

        platform.request_top5_confirmation_job.assert_not_awaited()

    async def test_idle_slots_catch_up_distinct_members_concurrently(self) -> None:
        entries = [
            _entry(f"5Miner{index}" + "x" * 40, 0.90 - index * 0.01).model_copy(
                update={"bench_version": 8}
            )
            for index in range(3)
        ]
        platform = _platform_with_ledger(jobs=[], ledger=entries)
        slot_by_agent = {
            entry.agent_id: f"slot-{index}" for index, entry in enumerate(entries)
        }

        async def request_job(
            *, champion_agent_id: UUID, member_agent_id: UUID
        ) -> JobResponse:
            assert champion_agent_id in slot_by_agent
            entry = next(item for item in entries if item.agent_id == member_agent_id)
            return _job(
                entry.miner_hotkey, slot_id=slot_by_agent[member_agent_id]
            ).model_copy(
                update={
                    "agent_id": member_agent_id,
                    "bench_version": 8,
                    "confirmation_datasets": _confirmation_pins(member_agent_id),
                    "deadline": datetime.now(UTC) + timedelta(hours=3),
                }
            )

        platform.request_top5_confirmation_job = AsyncMock(side_effect=request_job)
        config = _config()
        config.benchmark_capacity = 3
        worker = ValidatorWorker(
            config=config,
            platform=platform,
            dittobench=MagicMock(),
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )
        all_started = asyncio.Event()
        active = 0
        max_active = 0

        async def evaluate(
            _agent_id: UUID, _sha256: str, *, seed: int, **_: object
        ) -> ScoreReport:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            if active == len(entries):
                all_started.set()
            await asyncio.wait_for(all_started.wait(), timeout=1)
            active -= 1
            return _report(str(seed), 0.8).model_copy(
                update={"seed": seed, "bench_version": 8}
            )

        worker._evaluate = AsyncMock(side_effect=evaluate)  # type: ignore[method-assign]

        await worker._run_top5_confirmation_lane()

        assert max_active == len(entries)
        assert platform.request_top5_confirmation_job.await_count == len(entries)
        assert platform.submit_top5_confirmation_score.await_count == len(entries)
        assert all(slot.active_agent_id is None for slot in worker._slots.values())

    async def test_extended_cohort_waits_for_priority_claims_not_runs(self) -> None:
        """Spare ranks retry after top-five admission without waiting 90 minutes.

        Platform serializes the fairness decision. If all ranks are offered at
        once, an extended request can arrive before the top-five ticket writes
        commit, receive a priority 409, and remain idle until an accepted run
        completes. The worker must stage claims at that transaction boundary
        while still executing every accepted job concurrently.
        """
        entries = [
            _entry(f"5Miner{index}" + "x" * 40, 0.90 - index * 0.01).model_copy(
                update={"bench_version": 8}
            )
            for index in range(6)
        ]
        platform = _platform_with_ledger(jobs=[], ledger=entries)
        platform.get_ledger.return_value = platform.get_ledger.return_value.model_copy(
            update={"continual_retest_cohort_size": 6}
        )
        priority_ids = {entry.agent_id for entry in entries[:5]}
        priority_started = 0
        priority_resolved = 0
        release_priority = asyncio.Event()

        async def request_job(
            *, champion_agent_id: UUID, member_agent_id: UUID
        ) -> JobResponse:
            nonlocal priority_started, priority_resolved
            assert champion_agent_id == entries[0].agent_id
            if member_agent_id in priority_ids:
                priority_started += 1
                if priority_started == len(priority_ids):
                    release_priority.set()
                await release_priority.wait()
                priority_resolved += 1
            elif priority_resolved != len(priority_ids):
                raise PlatformError("emission members still need coverage")
            index = next(
                idx
                for idx, entry in enumerate(entries)
                if entry.agent_id == member_agent_id
            )
            return _job(
                entries[index].miner_hotkey, slot_id=f"slot-{index}"
            ).model_copy(
                update={
                    "agent_id": member_agent_id,
                    "bench_version": 8,
                    "confirmation_datasets": _confirmation_pins(member_agent_id),
                    "deadline": datetime.now(UTC) + timedelta(hours=3),
                }
            )

        platform.request_top5_confirmation_job = AsyncMock(side_effect=request_job)
        config = _config()
        config.benchmark_capacity = len(entries)
        worker = ValidatorWorker(
            config=config,
            platform=platform,
            dittobench=MagicMock(),
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )
        all_running = asyncio.Event()
        active = 0

        async def evaluate(
            _agent_id: UUID, _sha256: str, *, seed: int, **_: object
        ) -> ScoreReport:
            nonlocal active
            active += 1
            if active == len(entries):
                all_running.set()
            await asyncio.wait_for(all_running.wait(), timeout=1)
            return _report(str(seed), 0.8).model_copy(
                update={"seed": seed, "bench_version": 8}
            )

        worker._evaluate = AsyncMock(side_effect=evaluate)  # type: ignore[method-assign]

        await worker._run_top5_confirmation_lane()

        assert priority_resolved == len(priority_ids)
        assert platform.request_top5_confirmation_job.await_count == len(entries)
        assert platform.submit_top5_confirmation_score.await_count == len(entries)

    async def test_appends_multi_seed_receipt_without_replacing_score(self) -> None:
        entry = _entry("5MinerA" + "x" * 41, 0.9).model_copy(
            update={"bench_version": 8}
        )
        platform = _platform_with_ledger(jobs=[], ledger=[entry])
        platform.request_top5_confirmation_job = AsyncMock(
            return_value=_job(entry.miner_hotkey).model_copy(
                update={
                    "agent_id": entry.agent_id,
                    "bench_version": 8,
                    "confirmation_datasets": _confirmation_pins(entry.agent_id),
                    "deadline": datetime.now(UTC) + timedelta(hours=3),
                }
            )
        )
        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=MagicMock(),
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )

        async def evaluate(
            _agent_id: UUID, _sha256: str, *, seed: int, **_: object
        ) -> ScoreReport:
            return _report(str(seed), 0.8).model_copy(
                update={"seed": seed, "bench_version": 8}
            )

        worker._evaluate = AsyncMock(side_effect=evaluate)  # type: ignore[method-assign]
        await worker._run_top5_confirmation_lane()

        platform.submit_score.assert_not_awaited()
        platform.submit_top5_confirmation_score.assert_awaited_once()
        report = platform.submit_top5_confirmation_score.await_args.kwargs["report"]
        assert len(report.confirmation_seeds) == 3
        assert report.confirmation_composites == [0.8, 0.8, 0.8]

    async def test_v8_confirmation_reuses_one_ticket_bound_inference_session(
        self,
    ) -> None:
        entry = _entry("5MinerA" + "x" * 41, 0.9).model_copy(
            update={"bench_version": 8}
        )
        deadline = datetime.now(UTC) + timedelta(hours=3)
        grant_id = uuid4()
        offer = InferenceGrantOffer(
            grant_id=grant_id,
            exchange_url="https://platform.example/exchange",
            proxy_url="https://platform.example/inference",
            allowed_models=["openai/gpt-oss-20b"],
            request_budget=1203,
            token_budget=3_000_000,
            expires_at=deadline,
            provider="amazon-bedrock",
            profile_revision="openrouter-amazon-bedrock-gpt-oss-20b-v1",
        )
        job = _job(entry.miner_hotkey, deadline=deadline).model_copy(
            update={
                "agent_id": entry.agent_id,
                "bench_version": 8,
                "inference": offer,
                "confirmation_datasets": [
                    ConfirmationDatasetPin(
                        seed=seed,
                        dataset_sha256=hashlib.sha256(str(seed).encode()).hexdigest(),
                        run_size="full",
                    )
                    for seed in worker_mod.confirmation_seeds(
                        [str(entry.agent_id)], version=8, count=1
                    )
                ],
            }
        )
        platform = _platform_with_ledger(jobs=[], ledger=[entry])
        platform.request_top5_confirmation_job = AsyncMock(return_value=job)
        dittobench = MagicMock()
        dittobench.cancel_inference_session = AsyncMock()
        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=dittobench,
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )
        broker = InferenceBrokerSession(
            session_id="top5-session",
            activation_secret="activation",
            broker_public_key="a" * 43,
        )
        worker._activate_ticket_inference = AsyncMock(  # type: ignore[method-assign]
            return_value=broker
        )

        async def evaluate(
            _agent_id: UUID, _sha256: str, *, seed: int, **kwargs: object
        ) -> ScoreReport:
            assert kwargs["inference_session_id"] == "top5-session"
            assert kwargs["inference_grant_id"] == grant_id
            assert kwargs["inference_slot_id"] == job.slot_id
            assert kwargs["inference_ticket_deadline"] == deadline
            assert (
                kwargs["dataset_sha256"]
                == hashlib.sha256(str(seed).encode()).hexdigest()
            )
            assert kwargs["run_size"] == "full"
            assert kwargs["progress_callback"] == worker._on_dittobench_progress
            return _report(str(seed), 0.8).model_copy(
                update={"seed": seed, "bench_version": 8}
            )

        worker._evaluate = AsyncMock(side_effect=evaluate)  # type: ignore[method-assign]
        await worker._run_top5_confirmation_lane()

        worker._activate_ticket_inference.assert_awaited_once_with(job)
        assert worker._evaluate.await_count == 1
        dittobench.cancel_inference_session.assert_awaited_once_with("top5-session")
        platform.submit_top5_confirmation_score.assert_awaited_once()

    async def test_v8_rejects_unpinned_confirmation_lease_before_scoring(
        self,
    ) -> None:
        entry = _entry("5MinerA" + "x" * 41, 0.9).model_copy(
            update={"bench_version": 8}
        )
        job = _job(entry.miner_hotkey).model_copy(
            update={"agent_id": entry.agent_id, "bench_version": 8}
        )
        platform = _platform_with_ledger(jobs=[], ledger=[entry])
        platform.request_top5_confirmation_job = AsyncMock(return_value=job)
        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=MagicMock(),
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )
        worker._evaluate = AsyncMock()  # type: ignore[method-assign]

        await worker._run_top5_confirmation_lane()

        worker._evaluate.assert_not_awaited()
        platform.report_ticket_failed.assert_awaited_once()
        handed_job, reason, _ = platform.report_ticket_failed.await_args.args
        assert (handed_job, reason) == (job, "infrastructure")
        platform.submit_top5_confirmation_score.assert_not_awaited()

    async def test_infrastructure_seed_failure_preserves_code_and_retires_slot(
        self,
    ) -> None:
        entry = _entry("5MinerA" + "x" * 41, 0.9).model_copy(
            update={"bench_version": 9}
        )
        job = _job(entry.miner_hotkey, slot_id="slot-1").model_copy(
            update={
                "agent_id": entry.agent_id,
                "bench_version": 9,
                "confirmation_datasets": _confirmation_pins(
                    entry.agent_id, bench_version=9, count=1
                ),
                "deadline": datetime.now(UTC) + timedelta(hours=3),
            }
        )
        platform = _platform_with_ledger(
            jobs=[], ledger=[entry], active_bench_version=9
        )
        platform.request_top5_confirmation_job = AsyncMock(return_value=job)
        config = _config()
        config.benchmark_capacity = 2
        worker = ValidatorWorker(
            config=config,
            platform=platform,
            dittobench=MagicMock(),
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )
        worker._evaluate = AsyncMock(  # type: ignore[method-assign]
            side_effect=ValidatorInfrastructureError(
                "relay failed after a healthy provider probe",
                code="model_relay_unavailable",
            )
        )

        await worker._run_top5_confirmation_lane()

        platform.report_ticket_failed.assert_awaited_once_with(
            job, "infrastructure", "model_relay_unavailable"
        )
        assert "slot-1" not in worker._healthy_slots
        platform.submit_top5_confirmation_score.assert_not_awaited()

    async def test_agent_seed_failure_remains_a_scoring_error(self) -> None:
        entry = _entry("5MinerA" + "x" * 41, 0.9).model_copy(
            update={"bench_version": 9}
        )
        job = _job(entry.miner_hotkey).model_copy(
            update={
                "agent_id": entry.agent_id,
                "bench_version": 9,
                "confirmation_datasets": _confirmation_pins(
                    entry.agent_id, bench_version=9, count=1
                ),
                "deadline": datetime.now(UTC) + timedelta(hours=3),
            }
        )
        platform = _platform_with_ledger(
            jobs=[], ledger=[entry], active_bench_version=9
        )
        platform.request_top5_confirmation_job = AsyncMock(return_value=job)
        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=MagicMock(),
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )
        worker._evaluate = AsyncMock(  # type: ignore[method-assign]
            side_effect=DittobenchError("harness returned invalid evidence")
        )

        await worker._run_top5_confirmation_lane()

        platform.report_ticket_failed.assert_awaited_once_with(
            job,
            "scoring_error",
            "DittobenchError: harness returned invalid evidence",
        )
        platform.submit_top5_confirmation_score.assert_not_awaited()


class TestTop5ConfirmationLaneSlotBinding:
    """The retest must report against the slot the platform actually leased.

    The lane runs in the sweep body, outside the per-slot context ``run_slot``
    installs, so every state write used to resolve to the default ``slot-0``
    while the platform issued the lease against ``job.slot_id``. The platform
    drops slot progress that does not match a live ticket on that exact slot,
    so a healthy retest on any slot but ``slot-0`` reported nothing at all and
    read as frozen for its whole lease.
    """

    @staticmethod
    def _lane_worker(platform: MagicMock, *, capacity: int) -> ValidatorWorker:
        config = _config()
        config.benchmark_capacity = capacity
        return ValidatorWorker(
            config=config,
            platform=platform,
            dittobench=MagicMock(),
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )

    async def test_progress_lands_on_the_leased_slot_not_slot_zero(self) -> None:
        entry = _entry("5MinerA" + "x" * 41, 0.9).model_copy(
            update={"bench_version": 8}
        )
        job = _job(entry.miner_hotkey, slot_id="slot-1").model_copy(
            update={
                "agent_id": entry.agent_id,
                "bench_version": 8,
                "confirmation_datasets": _confirmation_pins(entry.agent_id),
                "deadline": datetime.now(UTC) + timedelta(hours=3),
            }
        )
        platform = _platform_with_ledger(jobs=[], ledger=[entry])
        platform.request_top5_confirmation_job = AsyncMock(return_value=job)
        worker = self._lane_worker(platform, capacity=2)
        observed: list[tuple[str, UUID | None]] = []

        async def evaluate(
            _agent_id: UUID, _sha256: str, *, seed: int, **kwargs: object
        ) -> ScoreReport:
            # Drive the scorer's own progress callback so this covers the
            # published-progress path, not just the initial claim.
            callback = kwargs["progress_callback"]
            assert callable(callback)
            await callback(
                DittobenchProgressSnapshot(
                    stage="running_benchmark",
                    completed=7,
                    total=30,
                    run_token="abcdef01",
                )
            )
            observed.extend(
                (slot_id, slot.active_agent_id)
                for slot_id, slot in worker._slots.items()
            )
            return _report(str(seed), 0.8).model_copy(
                update={"seed": seed, "bench_version": 8}
            )

        worker._evaluate = AsyncMock(side_effect=evaluate)  # type: ignore[method-assign]
        await worker._run_top5_confirmation_lane()

        # The leased slot owns the run; the default slot is never touched.
        assert ("slot-1", entry.agent_id) in observed
        assert ("slot-0", None) in observed

        # ...and that is what the platform actually receives. A slot_id the
        # platform cannot match to a live ticket is dropped at ingest, which is
        # precisely how a running retest went out as "nothing active".
        reported = [
            slot
            for call in platform.submit_heartbeat.await_args_list
            if call.args[0].benchmark_capacity is not None
            for slot in call.args[0].benchmark_capacity.active
        ]
        assert reported, "the retest published no capacity at all"
        assert {slot.slot_id for slot in reported} == {"slot-1"}
        assert {slot.agent_id for slot in reported} == {entry.agent_id}
        running = [
            slot for slot in reported if slot.progress.stage == "running_benchmark"
        ]
        assert running and running[-1].progress.completed == 7

        # The slot is released once the lease is done, so it is reusable.
        assert worker._slots["slot-1"].active_agent_id is None
        platform.submit_top5_confirmation_score.assert_awaited_once()

    async def test_lease_for_an_unserved_slot_is_handed_back(self) -> None:
        # A slot this validator does not serve must not raise KeyError out of
        # the lane and abort the sweep; hand the lease back like the canonical
        # path does on a slot mismatch.
        entry = _entry("5MinerA" + "x" * 41, 0.9).model_copy(
            update={"bench_version": 8}
        )
        job = _job(entry.miner_hotkey, slot_id="slot-3").model_copy(
            update={"agent_id": entry.agent_id, "bench_version": 8}
        )
        platform = _platform_with_ledger(jobs=[], ledger=[entry])
        platform.request_top5_confirmation_job = AsyncMock(return_value=job)
        worker = self._lane_worker(platform, capacity=2)
        worker._evaluate = AsyncMock()  # type: ignore[method-assign]

        await worker._run_top5_confirmation_lane()

        worker._evaluate.assert_not_awaited()
        platform.report_ticket_failed.assert_awaited_once()
        handed_job, reason, _ = platform.report_ticket_failed.await_args.args
        assert (handed_job, reason) == (job, "infrastructure")
        platform.submit_top5_confirmation_score.assert_not_awaited()

    async def test_slot_is_released_when_the_lease_names_another_agent(self) -> None:
        # The claim is made from job.agent_id but release used to be gated on
        # entry.agent_id. A divergence left the slot occupied for the rest of
        # the lease with no operator signal and no way to revoke it.
        entry = _entry("5MinerA" + "x" * 41, 0.9).model_copy(
            update={"bench_version": 1}
        )
        job = _job(entry.miner_hotkey, slot_id="slot-1").model_copy(
            update={
                "agent_id": uuid4(),
                "bench_version": 1,
                "deadline": datetime.now(UTC) + timedelta(hours=3),
            }
        )
        platform = _platform_with_ledger(jobs=[], ledger=[entry])
        platform.request_top5_confirmation_job = AsyncMock(return_value=job)
        worker = self._lane_worker(platform, capacity=2)

        async def evaluate(
            _agent_id: UUID, _sha256: str, *, seed: int, **_: object
        ) -> ScoreReport:
            return _report(str(seed), 0.8).model_copy(
                update={"seed": seed, "bench_version": 1}
            )

        worker._evaluate = AsyncMock(side_effect=evaluate)  # type: ignore[method-assign]
        await worker._run_top5_confirmation_lane()

        # No slot may be left holding the lease -- not the leased one, and not
        # the default one the claim used to land on.
        assert all(slot.active_agent_id is None for slot in worker._slots.values())
        assert all(slot.progress is None for slot in worker._slots.values())


class TestRunOnce:
    async def test_capacity_mismatch_narrows_to_the_scorer_rather_than_stopping(
        self,
    ) -> None:
        """A worker ahead of its scorer runs the scorer's slots, not zero.

        The two capacities ride one Compose variable and disagree only while a
        targeted update has refreshed the worker but not yet the scorer.
        Refusing all work there would idle a validator the three-hotkey quorum
        cannot spare; claiming past the scorer would only earn a 429.
        """
        platform = _platform_with_ledger(jobs=[], ledger=[])
        platform.request_job = AsyncMock(return_value=None)
        config = _config()
        config.benchmark_capacity = 4
        dittobench = MagicMock()
        dittobench.full_run_capacity = 1
        worker = ValidatorWorker(
            config=config,
            platform=platform,
            dittobench=dittobench,
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )

        await worker.run_once(set_weights=False)

        # Exactly the scorer's capacity is polled, from the lowest ordinal up.
        assert platform.request_job.await_count == 1
        assert platform.request_job.await_args.kwargs["slot_id"] == "slot-0"
        advertised = [
            call.args[0].benchmark_capacity
            for call in platform.submit_heartbeat.await_args_list
        ]
        assert all(capacity is not None for capacity in advertised)
        # Capacity is still reported honestly, but no heartbeat ever offers more
        # healthy slots than the scorer can actually run.
        assert {capacity.configured_slots for capacity in advertised} == {4}
        assert max(len(capacity.healthy_slots) for capacity in advertised) == 1
        assert ["slot-0"] in [capacity.healthy_slots for capacity in advertised]

    async def test_scorer_capacity_never_raises_beyond_configured_slots(self) -> None:
        """A scorer offering more than the host configured changes nothing."""
        platform = _platform_with_ledger(jobs=[], ledger=[])
        platform.request_job = AsyncMock(return_value=None)
        config = _config()
        config.benchmark_capacity = 2
        dittobench = MagicMock()
        dittobench.full_run_capacity = 8
        worker = ValidatorWorker(
            config=config,
            platform=platform,
            dittobench=dittobench,
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )

        await worker.run_once(set_weights=False)

        final = platform.submit_heartbeat.await_args_list[-1].args[0]
        assert final.benchmark_capacity is not None
        assert final.benchmark_capacity.configured_slots == 2
        assert final.benchmark_capacity.healthy_slots == ["slot-0", "slot-1"]

    async def test_parallel_slots_overlap_and_isolate_sibling_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        jobs = {
            "slot-0": _job("5MinerA" + "x" * 41, slot_id="slot-0"),
            "slot-1": _job("5MinerB" + "x" * 41, slot_id="slot-1"),
        }
        claimed: set[str] = set()

        async def request_job(*, slot_id: str) -> JobResponse | None:
            if slot_id in claimed:
                return None
            claimed.add(slot_id)
            return jobs[slot_id]

        platform = _platform_with_ledger(jobs=[], ledger=[])
        platform.request_job = AsyncMock(side_effect=request_job)
        config = _config()
        config.benchmark_capacity = 2
        dittobench = MagicMock()
        dittobench.full_run_capacity = 2
        worker = ValidatorWorker(
            config=config,
            platform=platform,
            dittobench=dittobench,
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )
        both_started = asyncio.Event()
        release = asyncio.Event()
        active = 0
        max_active = 0

        async def score(job: JobResponse) -> ScoreReport:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            if active == 2:
                both_started.set()
            await release.wait()
            active -= 1
            if job.slot_id == "slot-0":
                raise ValidatorInfrastructureError("isolated sandbox failure")
            return _report("run-slot-1", 0.8)

        monkeypatch.setattr(worker, "_score_job", score)
        sweep = asyncio.create_task(worker.run_once(set_weights=False))
        await asyncio.wait_for(both_started.wait(), timeout=1)
        assert max_active == 2
        release.set()
        outcome = await sweep

        assert outcome.queue_depth == 2
        assert platform.report_ticket_failed.await_count == 1
        assert "slot-0" not in worker._healthy_slots
        assert "slot-1" in worker._healthy_slots

    async def test_idle_slot_repolls_while_a_sibling_holds_a_lease(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty poll must not retire a slot for the rest of the sweep.

        ``run_once`` gathers every slot and does not return until the last one
        does, while the slot holding a lease keeps re-claiming inside that same
        gather. So a slot that breaks out on its first empty queue does not poll
        again until the whole pool is idle -- up to a full ninety-minute lease,
        and indefinitely while any sibling keeps finding work. The host then
        serves exactly one benchmark no matter how many slots it advertises or
        how high the platform sets ``max_concurrent_slots``, which is the fleet
        being pinned at one busy slot per validator.
        """
        long_job = _job("5MinerA" + "x" * 41, slot_id="slot-0")
        late_job = _job("5MinerB" + "x" * 41, slot_id="slot-1")
        pending = {"slot-0": long_job, "slot-1": late_job}
        polls: list[str] = []
        queue_refilled = asyncio.Event()
        claimed_late = asyncio.Event()

        async def request_job(*, slot_id: str) -> JobResponse | None:
            polls.append(slot_id)
            # slot-1's work only exists after its first poll, exactly like a
            # quorum opening or a new submission arriving mid-lease.
            if slot_id == "slot-1" and not queue_refilled.is_set():
                return None
            job = pending.pop(slot_id, None)
            if job is not None and slot_id == "slot-1":
                claimed_late.set()
            return job

        platform = _platform_with_ledger(jobs=[], ledger=[])
        platform.request_job = AsyncMock(side_effect=request_job)
        config = _config()
        config.benchmark_capacity = 2
        dittobench = MagicMock()
        dittobench.full_run_capacity = 2
        worker = ValidatorWorker(
            config=config,
            platform=platform,
            dittobench=dittobench,
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )
        # Keep the idle wait short; the mechanism under test is that the slot
        # comes back at all, not how long it waits first.
        monkeypatch.setattr(
            worker_mod, "_IDLE_SLOT_REPOLL_SECONDS", 0.01, raising=False
        )
        started = asyncio.Event()
        release = asyncio.Event()

        async def score(job: JobResponse) -> ScoreReport:
            if job.slot_id == "slot-0":
                started.set()
                await release.wait()
                return _report("run-slot-0", 0.7)
            return _report("run-slot-1", 0.8)

        monkeypatch.setattr(worker, "_score_job", score)
        sweep = asyncio.create_task(worker.run_once(set_weights=False))
        await asyncio.wait_for(started.wait(), timeout=2)
        queue_refilled.set()
        stranded = False
        try:
            await asyncio.wait_for(claimed_late.wait(), timeout=2)
        except TimeoutError:
            stranded = True
        release.set()
        outcome = await asyncio.wait_for(sweep, timeout=5)

        assert not stranded, (
            "slot-1 never polled again after its first empty queue, so it sat "
            "out the whole sweep while slot-0 held a lease"
        )
        assert polls.count("slot-1") >= 2
        # Both leases were claimed inside the one sweep, and neither slot was
        # left holding work the sweep never handed out.
        assert outcome.queue_depth == 2
        assert pending == {}

    async def test_empty_initial_poll_waits_for_sibling_claim_to_resolve(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fast 204 must not race a slower successful sibling claim.

        Platform allocation serializes parts of concurrent claims. An empty
        slot can therefore receive its 204 before a sibling's successful
        response returns, even though both requests started together. Looking
        only at ``running`` in that window retires the empty slot for the whole
        sweep and leaves the validator at one active benchmark.
        """
        long_job = _job("5MinerA" + "x" * 41, slot_id="slot-0")
        late_job = _job("5MinerB" + "x" * 41, slot_id="slot-1")
        slot_zero_claim_started = asyncio.Event()
        release_slot_zero_claim = asyncio.Event()
        slot_one_returned_empty = asyncio.Event()
        late_job_claimed = asyncio.Event()
        slot_zero_claimed = False
        slot_one_polls = 0

        async def request_job(*, slot_id: str) -> JobResponse | None:
            nonlocal slot_one_polls, slot_zero_claimed
            if slot_id == "slot-0":
                if slot_zero_claimed:
                    return None
                slot_zero_claimed = True
                slot_zero_claim_started.set()
                await release_slot_zero_claim.wait()
                return long_job
            slot_one_polls += 1
            if slot_one_polls == 1:
                await slot_zero_claim_started.wait()
                slot_one_returned_empty.set()
                return None
            if slot_one_polls == 2:
                late_job_claimed.set()
                return late_job
            return None

        platform = _platform_with_ledger(jobs=[], ledger=[])
        platform.request_job = AsyncMock(side_effect=request_job)
        config = _config()
        config.benchmark_capacity = 2
        dittobench = MagicMock()
        dittobench.full_run_capacity = 2
        worker = ValidatorWorker(
            config=config,
            platform=platform,
            dittobench=dittobench,
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )
        monkeypatch.setattr(
            worker_mod, "_IDLE_SLOT_REPOLL_SECONDS", 0.01, raising=False
        )
        release_long_job = asyncio.Event()

        async def score(job: JobResponse) -> ScoreReport:
            if job.slot_id == "slot-0":
                await release_long_job.wait()
                return _report("run-slot-0", 0.7)
            return _report("run-slot-1", 0.8)

        monkeypatch.setattr(worker, "_score_job", score)
        sweep = asyncio.create_task(worker.run_once(set_weights=False))
        await asyncio.wait_for(slot_one_returned_empty.wait(), timeout=2)
        # Let the empty response reach the worker before the successful sibling
        # claim resolves. This is the production ordering that exposed the race.
        await asyncio.sleep(0)
        release_slot_zero_claim.set()
        await asyncio.wait_for(late_job_claimed.wait(), timeout=2)
        release_long_job.set()
        outcome = await asyncio.wait_for(sweep, timeout=5)

        assert slot_one_polls >= 2
        assert outcome.queue_depth == 2

    async def test_idle_slot_dispatches_retests_while_sibling_holds_canonical_lease(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Spare slots must not wait for the whole canonical sweep to drain."""
        long_job = _job("5MinerA" + "x" * 41, slot_id="slot-0")
        claimed = False

        async def request_job(*, slot_id: str) -> JobResponse | None:
            nonlocal claimed
            if slot_id == "slot-0" and not claimed:
                claimed = True
                return long_job
            return None

        platform = _platform_with_ledger(jobs=[], ledger=[])
        platform.request_job = AsyncMock(side_effect=request_job)
        config = _config()
        config.benchmark_capacity = 2
        dittobench = MagicMock()
        dittobench.full_run_capacity = 2
        worker = ValidatorWorker(
            config=config,
            platform=platform,
            dittobench=dittobench,
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )
        monkeypatch.setattr(
            worker_mod, "_IDLE_SLOT_REPOLL_SECONDS", 0.01, raising=False
        )
        canonical_started = asyncio.Event()
        release_canonical = asyncio.Event()
        retest_dispatched = asyncio.Event()
        retest_calls = 0

        async def score(_job: JobResponse) -> ScoreReport:
            canonical_started.set()
            await release_canonical.wait()
            return _report("run-slot-0", 0.7)

        async def run_retests(**_kwargs: object) -> None:
            nonlocal retest_calls
            retest_calls += 1
            # This must happen while slot-0 still owns its canonical lease.
            if retest_calls == 1:
                assert not release_canonical.is_set()
                retest_dispatched.set()

        monkeypatch.setattr(worker, "_score_job", score)
        monkeypatch.setattr(worker, "_run_top5_confirmation_lane", run_retests)

        sweep = asyncio.create_task(worker.run_once(set_weights=False))
        await asyncio.wait_for(canonical_started.wait(), timeout=2)
        await asyncio.wait_for(retest_dispatched.wait(), timeout=2)
        release_canonical.set()
        outcome = await asyncio.wait_for(sweep, timeout=5)

        assert outcome.queue_depth == 1
        # Every retest dispatch was preceded by an empty canonical poll; the
        # ordinary queue therefore keeps strict first refusal on spare slots.
        assert platform.request_job.await_count >= 2

    async def test_idle_slots_end_the_sweep_when_nothing_is_running(self) -> None:
        """With no lease in flight an empty poll still ends the sweep at once.

        The waiting above is bounded by a sibling actually holding a lease. An
        idle pool must not spin: it has to fall through to the ordinary sweep
        cadence exactly as before.
        """
        platform = _platform_with_ledger(jobs=[], ledger=[])
        platform.request_job = AsyncMock(return_value=None)
        config = _config()
        config.benchmark_capacity = 4
        dittobench = MagicMock()
        dittobench.full_run_capacity = 4
        worker = ValidatorWorker(
            config=config,
            platform=platform,
            dittobench=dittobench,
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )

        outcome = await asyncio.wait_for(worker.run_once(set_weights=False), timeout=5)

        assert outcome.queue_depth == 0
        # One poll per slot and no more: nothing was running, so nothing waited.
        assert platform.request_job.await_count == 4

    async def test_compose_default_capacity_allocates_dense_slot_ids(self) -> None:
        """The shipped compose default must produce that many dense slots.

        The platform addresses slots by ordinal (``slot-0``..``slot-N``) when it
        applies its operator cap, so a sparse or renamed slot set would silently
        lose capacity.
        """
        platform = _platform_with_ledger(jobs=[], ledger=[])
        # Every slot polls independently, so the empty queue must answer each of
        # them rather than a single scripted call.
        platform.request_job = AsyncMock(return_value=None)
        config = _config()
        config.benchmark_capacity = _COMPOSE_DEFAULT_BENCHMARK_CAPACITY
        dittobench = MagicMock()
        dittobench.full_run_capacity = _COMPOSE_DEFAULT_BENCHMARK_CAPACITY
        worker = ValidatorWorker(
            config=config,
            platform=platform,
            dittobench=dittobench,
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )

        await worker.run_once(set_weights=False)

        expected = [
            f"slot-{index}" for index in range(_COMPOSE_DEFAULT_BENCHMARK_CAPACITY)
        ]
        assert sorted(worker._slots) == sorted(expected)
        final = platform.submit_heartbeat.await_args_list[-1].args[0]
        assert final.benchmark_capacity is not None
        assert (
            final.benchmark_capacity.configured_slots
            == _COMPOSE_DEFAULT_BENCHMARK_CAPACITY
        )
        assert final.benchmark_capacity.healthy_slots == expected
        assert final.benchmark_capacity.admission == "accepting"
        # Every advertised slot polls, so a platform-side cap can decline the
        # ones it does not want used.
        assert platform.request_job.await_count == _COMPOSE_DEFAULT_BENCHMARK_CAPACITY

    async def test_platform_capping_slots_is_not_an_error(self) -> None:
        """A 204 on the capped slots must leave the worker healthy.

        The platform's operator cap declines high ordinals by returning no job.
        That is a normal empty queue, not a slot failure: the slot must stay
        healthy and advertised so raising the cap needs no validator restart.
        """
        served: JobResponse | None = _job("5MinerA" + "x" * 41, slot_id="slot-0")

        async def request_job(*, slot_id: str) -> JobResponse | None:
            # Platform cap of one: only the lowest ordinal is ever served, and
            # the queue drains after that one job.
            nonlocal served
            if slot_id != "slot-0":
                return None
            job, served = served, None
            return job

        platform = _platform_with_ledger(jobs=[], ledger=[])
        platform.request_job = AsyncMock(side_effect=request_job)
        config = _config()
        config.benchmark_capacity = 4
        dittobench = MagicMock()
        dittobench.full_run_capacity = 4
        dittobench.score_tarball = AsyncMock(return_value=_report("run-0", 0.7))
        worker = ValidatorWorker(
            config=config,
            platform=platform,
            dittobench=dittobench,
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )

        outcome = await worker.run_once(set_weights=False)

        assert outcome.queue_depth == 1
        assert worker._healthy_slots == {f"slot-{index}" for index in range(4)}
        platform.report_ticket_failed.assert_not_awaited()
        final = platform.submit_heartbeat.await_args_list[-1].args[0]
        assert final.benchmark_capacity is not None
        assert final.benchmark_capacity.configured_slots == 4

    async def test_concurrent_slots_keep_per_slot_state_isolated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Four overlapping runs must not leak agent identity between slots.

        Per-slot state is held in a ``contextvars`` slot map, so a regression to
        shared worker state would cross-assign agents to leases and corrupt the
        signed per-slot progress the platform stores.
        """
        jobs = {
            f"slot-{index}": _job(f"5Miner{index}" + "x" * 41, slot_id=f"slot-{index}")
            for index in range(4)
        }
        claimed: set[str] = set()

        async def request_job(*, slot_id: str) -> JobResponse | None:
            if slot_id in claimed:
                return None
            claimed.add(slot_id)
            return jobs[slot_id]

        platform = _platform_with_ledger(jobs=[], ledger=[])
        platform.request_job = AsyncMock(side_effect=request_job)
        config = _config()
        config.benchmark_capacity = 4
        dittobench = MagicMock()
        dittobench.full_run_capacity = 4
        worker = ValidatorWorker(
            config=config,
            platform=platform,
            dittobench=dittobench,
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )
        all_started = asyncio.Event()
        release = asyncio.Event()
        active = 0
        max_active = 0
        observed: dict[str, UUID | None] = {}

        async def score(job: JobResponse) -> ScoreReport:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            # Write through the contextvar-backed property from four overlapping
            # tasks, then read it back after every sibling has also written.
            worker._active_agent_id = job.agent_id
            worker._slot_state().bench_version = job.bench_version
            if active == 4:
                all_started.set()
            await release.wait()
            observed[job.slot_id] = worker._active_agent_id
            active -= 1
            return _report(f"run-{job.slot_id}", 0.8)

        monkeypatch.setattr(worker, "_score_job", score)
        sweep = asyncio.create_task(worker.run_once(set_weights=False))
        await asyncio.wait_for(all_started.wait(), timeout=1)
        assert max_active == 4
        release.set()
        outcome = await sweep

        assert outcome.queue_depth == 4
        # Each slot read back its own agent, not the last writer's.
        assert observed == {slot_id: jobs[slot_id].agent_id for slot_id in jobs}
        assert len(set(observed.values())) == 4
        platform.report_ticket_failed.assert_not_awaited()

    async def test_idle_sweep_does_not_create_unticketed_rescore_work(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        platform = _platform_with_ledger(
            jobs=[],
            # Production regression: a legacy row with no declared version
            # normalizes to baseline v1 and used to trigger CRN confirmation.
            ledger=[_entry("5MinerA" + "x" * 41, 0.9)],
        )
        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=MagicMock(),
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )
        confirm_and_submit = AsyncMock()
        monkeypatch.setattr(worker, "_confirm_and_submit", confirm_and_submit)

        outcome = await worker.run_once(set_weights=False)

        assert outcome.queue_depth == 0
        platform.request_job.assert_awaited_once()
        # The continual lane may inspect the ledger, but the platform claim is
        # still the authorization boundary; a declined claim does no work.
        platform.get_ledger.assert_awaited_once()
        platform.request_top5_confirmation_job.assert_not_awaited()
        confirm_and_submit.assert_not_awaited()

    async def test_canonical_queue_work_then_uses_spare_capacity_for_retests(
        self,
    ) -> None:
        job = _job("5MinerA" + "x" * 41)
        platform = _platform_with_ledger(
            jobs=[job], ledger=[_entry("5MinerB" + "x" * 41, 0.9)]
        )
        dittobench = MagicMock()
        dittobench.score_tarball = AsyncMock(return_value=_report("run", 0.7))
        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=dittobench,
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )

        outcome = await worker.run_once(set_weights=False)

        assert outcome.queue_depth == 1
        platform.submit_score.assert_awaited_once()
        platform.get_ledger.assert_awaited_once()
        platform.request_top5_confirmation_job.assert_not_awaited()

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

        outcome = await worker.run_once()
        assert outcome.queue_depth == 1
        assert outcome.weights_ran
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
        assert heartbeat.protocol_version == HEARTBEAT_PROTOCOL_VERSION
        assert heartbeat.capabilities.signed_score_quorum is True
        assert heartbeat.benchmark_capacity is not None
        assert heartbeat.benchmark_capacity.configured_slots == 1
        assert heartbeat.capabilities is not None
        assert heartbeat.capabilities.screened_images is True
        assert heartbeat.capabilities.require_screened_image is True
        assert heartbeat.capabilities.source_build_fallback is False
        assert heartbeat.stack is not None
        assert heartbeat.stack.mode == "source"
        # No collector injected: the v9 heartbeat still validates, with the
        # conservative fallback that claims only the reporting process itself.
        assert heartbeat.stack_health is not None
        assert heartbeat.stack_health.ditto_subnet.health == "healthy"
        assert heartbeat.stack_health.dittobench_api.health == "unknown"
        assert heartbeat.stack_health.ollama is None
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
        chain.put_weights.assert_awaited_once_with({"5MinerA" + "x" * 41: 1.0})

    async def test_pre_requested_drain_claims_no_work(self) -> None:
        platform = _platform_with_ledger(jobs=[_job("5Miner" + "x" * 42)], ledger=[])
        drain = asyncio.Event()
        drain.set()
        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=MagicMock(),
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )

        outcome = await worker.run_once(drain_requested=drain)
        assert outcome.queue_depth == 0
        assert not outcome.weights_ran

        platform.request_job.assert_not_awaited()
        platform.get_ledger.assert_not_awaited()
        worker._weight_setter.put_weights.assert_not_called()

    async def test_drain_during_ticket_submits_it_and_claims_no_second(self) -> None:
        first = _job("5MinerA" + "x" * 41)
        second = _job("5MinerB" + "x" * 41)
        platform = _platform_with_ledger(jobs=[first, second], ledger=[])
        drain = asyncio.Event()

        async def score_and_request_drain(**_: object) -> ScoreReport:
            drain.set()
            return _report("run", 0.9)

        dittobench = MagicMock()
        dittobench.score_tarball = AsyncMock(side_effect=score_and_request_drain)
        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=dittobench,
            chain=MagicMock(put_weights=AsyncMock()),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )

        outcome = await worker.run_once(drain_requested=drain)
        assert outcome.queue_depth == 1
        assert not outcome.weights_ran

        assert platform.request_job.await_count == 1
        platform.submit_score.assert_awaited_once()
        platform.get_ledger.assert_not_awaited()
        worker._weight_setter.put_weights.assert_not_awaited()

    async def test_drain_ack_waits_for_same_lease_monotonic_submission(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        job = _job("5MinerA" + "x" * 41)
        platform = _platform_with_ledger(jobs=[job], ledger=[])
        drain = asyncio.Event()
        submit_started = asyncio.Event()
        allow_submit = asyncio.Event()
        events: list[tuple[str, str]] = []

        async def score_and_request_drain(**_: object) -> ScoreReport:
            drain.set()
            return _report("run", 0.9)

        async def blocked_submit(
            *_args: object, **_kwargs: object
        ) -> SubmitScoreResponse:
            events.append(("submit", "started"))
            submit_started.set()
            await allow_submit.wait()
            events.append(("submit", "finished"))
            return SubmitScoreResponse(
                agent_id=job.agent_id,
                status=AgentStatus.SCORED,
                accepted=True,
            )

        platform.submit_score = AsyncMock(side_effect=blocked_submit)
        config = _config()
        config.sweep_seconds = 120
        chain = MagicMock()
        chain.get_weights_rate_limit = AsyncMock(return_value=None)
        chain.get_last_update_block = AsyncMock(return_value=None)
        chain.get_latest_block = AsyncMock()
        chain.put_weights = AsyncMock()
        worker = ValidatorWorker(
            config=config,
            platform=platform,
            dittobench=MagicMock(
                score_tarball=AsyncMock(side_effect=score_and_request_drain)
            ),
            chain=chain,
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )

        def capture_state(state: str, **_: object) -> None:
            events.append(("state", state))

        monkeypatch.setattr(worker_mod, "write_update_state", capture_state)
        stop = asyncio.Event()
        task = asyncio.create_task(worker.run_forever(stop, drain_requested=drain))

        await asyncio.wait_for(submit_started.wait(), timeout=1)
        assert ("state", "drained") not in events
        assert platform.request_job.await_count == 1

        running = [
            call.args[0]
            for call in platform.submit_heartbeat.await_args_list
            if call.args[0].state == "running_benchmark"
        ]
        progresses = [heartbeat.benchmark_progress for heartbeat in running]
        stages = [progress.stage for progress in progresses if progress is not None]
        assert stages[0] == "preparing"
        assert stages[-2:] == ["finalizing", "submitting_result"]
        assert stages == sorted(
            stages, key=worker_mod._PROGRESS_STAGE_ORDER.__getitem__
        )
        assert all(
            progress.ticket_deadline == job.deadline
            for progress in progresses
            if progress is not None
        )

        allow_submit.set()
        for _ in range(100):
            if ("state", "drained") in events:
                break
            await asyncio.sleep(0.001)
        assert ("state", "drained") in events
        assert events.index(("submit", "finished")) < events.index(("state", "drained"))
        platform.submit_score.assert_awaited_once()
        assert platform.request_job.await_count == 1

        drain.clear()
        for _ in range(100):
            resumed = ("state", "ready") in events[
                events.index(("state", "drained")) + 1 :
            ]
            if resumed and chain.put_weights.await_count:
                break
            await asyncio.sleep(0.001)
        assert chain.put_weights.await_count == 1
        stop.set()
        await asyncio.wait_for(task, timeout=1)
        assert ("state", "ready") in events[events.index(("state", "drained")) + 1 :]

    async def test_quiescent_bootstrap_authenticates_before_claiming_work(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        platform = _platform_with_ledger(jobs=[], ledger=[])
        config = _config()
        config.sweep_seconds = 0.001
        chain = MagicMock()
        chain.get_weights_rate_limit = AsyncMock(return_value=None)
        worker = ValidatorWorker(
            config=config,
            platform=platform,
            dittobench=MagicMock(),
            chain=chain,
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )
        states: list[tuple[str, bool]] = []
        monkeypatch.setattr(
            worker_mod,
            "write_update_state",
            lambda state, **kwargs: states.append(
                (state, bool(kwargs.get("platform_accepted")))
            ),
        )
        stop = asyncio.Event()
        drain = asyncio.Event()
        drain.set()
        task = asyncio.create_task(worker.run_forever(stop, drain_requested=drain))

        for _ in range(100):
            if ("drained", True) in states:
                break
            await asyncio.sleep(0.001)
        assert ("drained", True) in states
        platform.submit_heartbeat.assert_awaited()
        platform.request_job.assert_not_awaited()

        drain.clear()
        for _ in range(100):
            if platform.request_job.await_count:
                break
            await asyncio.sleep(0.001)
        stop.set()
        await asyncio.wait_for(task, timeout=1)
        platform.request_job.assert_awaited_once()

    async def test_drained_worker_keeps_accepted_heartbeat_fresh(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worker = ValidatorWorker(
            config=_config(),
            platform=MagicMock(),
            dittobench=MagicMock(),
            chain=MagicMock(),
            keypair=MagicMock(),
        )
        worker._platform_accepted = True
        worker._report_heartbeat = AsyncMock(return_value=True)  # type: ignore[method-assign]
        monkeypatch.setattr(worker_mod, "_DRAIN_HEARTBEAT_SECONDS", 0.0)
        stop = asyncio.Event()
        drain = asyncio.Event()
        drain.set()

        task = asyncio.create_task(worker._wait_for_resume_or_stop(stop, drain))
        for _ in range(100):
            if worker._report_heartbeat.await_count >= 2:
                break
            await asyncio.sleep(0.001)
        stop.set()
        await asyncio.wait_for(task, timeout=1)

        assert worker._report_heartbeat.await_count >= 2
        assert drain.is_set()

    async def test_healthy_bootstrap_drain_self_resumes_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worker = ValidatorWorker(
            config=_config(),
            platform=MagicMock(),
            dittobench=MagicMock(),
            chain=MagicMock(),
            keypair=MagicMock(),
        )
        worker._platform_accepted = True
        worker._bootstrap_resume_ready = True
        worker._report_heartbeat = AsyncMock(return_value=True)  # type: ignore[method-assign]
        monkeypatch.setattr(worker_mod, "_BOOTSTRAP_SELF_RESUME_SECONDS", 0.0)
        persist_resume = MagicMock(return_value=True)
        stop = asyncio.Event()
        drain = asyncio.Event()
        drain.set()

        await worker._wait_for_resume_or_stop(
            stop,
            drain,
            bootstrap_resume=persist_resume,
        )

        persist_resume.assert_called_once_with()
        assert not drain.is_set()

    async def test_operator_drain_never_self_resumes(self) -> None:
        worker = ValidatorWorker(
            config=_config(),
            platform=MagicMock(),
            dittobench=MagicMock(),
            chain=MagicMock(),
            keypair=MagicMock(),
        )
        worker._platform_accepted = True
        worker._bootstrap_resume_ready = True
        stop = asyncio.Event()
        drain = asyncio.Event()
        drain.set()

        task = asyncio.create_task(worker._wait_for_resume_or_stop(stop, drain))
        await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task, timeout=1)

        assert drain.is_set()

    def test_bootstrap_self_resume_requires_fully_serving_stack(self) -> None:
        healthy = ValidatorComponentHealth(
            health="healthy", required=True, observed_at=1, ready=True
        )
        stack = ValidatorStackHealth(
            ditto_subnet=healthy,
            dittobench_api=healthy,
            sandbox_docker=healthy,
            pylon=healthy,
        )
        scorer = ScorerBenchmarkCapability(
            status="fresh_verified",
            supported_bench_versions=(8,),
            observed_at=1,
            software_version="0.43.14",
            source_revision="ab" * 20,
            probe=ScorerLivenessProbe(
                outcome="served",
                observed_at=1,
                http_status=200,
                last_served_at=1,
            ),
        )

        assert worker_mod._stack_can_self_resume(scorer, stack)
        assert not worker_mod._stack_can_self_resume(
            scorer,
            stack.model_copy(
                update={
                    "pylon": ValidatorComponentHealth(
                        health="unreachable", required=True, observed_at=1
                    )
                }
            ),
        )
        assert not worker_mod._stack_can_self_resume(
            scorer.model_copy(
                update={
                    "probe": ScorerLivenessProbe(
                        outcome="served_degraded",
                        observed_at=1,
                        http_status=200,
                        reason="identity_mismatch",
                        last_served_at=1,
                    )
                }
            ),
            stack,
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

        assert (await worker.run_once(set_weights=False)).queue_depth == 0
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

        outcome = await worker.run_once()
        assert outcome.queue_depth == 0
        assert outcome.weights_ran is True

        platform.request_job.assert_not_awaited()
        platform.get_artifact.assert_not_awaited()
        chain.put_weights.assert_awaited_once_with({"5MinerA" + "x" * 41: 1.0})
        final = platform.submit_heartbeat.await_args_list[-1].args[0]
        assert final.benchmark_capacity is not None
        assert final.benchmark_capacity.healthy_slots == []

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

        first = await worker.run_once(set_weights=False)
        assert first.queue_depth == 0
        assert first.weights_ran is False
        platform.request_job.assert_not_awaited()
        second = await worker.run_once(set_weights=False)
        assert second.queue_depth == 1
        assert second.weights_ran is False
        platform.submit_score.assert_awaited_once()
        final = platform.submit_heartbeat.await_args_list[-1].args[0]
        assert final.benchmark_capacity is not None
        assert final.benchmark_capacity.healthy_slots == ["slot-0"]

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

        outcome = await worker.run_once()
        assert outcome.queue_depth == 1
        assert outcome.weights_ran is True

        assert platform.request_job.await_count == 1
        platform.submit_score.assert_not_awaited()
        # The failed lease is handed back for immediate reissue rather than left
        # to expire; the sweep still ends and weights still proceed.
        platform.report_ticket_failed.assert_awaited_once()
        handed_job, reason, _ = platform.report_ticket_failed.await_args.args
        assert handed_job is jobs[0]
        assert reason == "infrastructure"
        chain.put_weights.assert_awaited_once_with({_BURN_HOTKEY: 1.0})

    async def test_seed_store_timeout_hands_back_exact_infrastructure_code(
        self,
    ) -> None:
        job = _job("5MinerA" + "x" * 41)
        platform = _platform_with_ledger(jobs=[job], ledger=[])
        dittobench = MagicMock()
        dittobench.preflight = AsyncMock()
        dittobench.score_tarball = AsyncMock(
            side_effect=ValidatorInfrastructureError(
                "seed store lock wait exhausted", code="seed_store_lock_timeout"
            )
        )
        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=dittobench,
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )

        outcome = await worker.run_once(set_weights=False)

        assert outcome.queue_depth == 1
        platform.report_ticket_failed.assert_awaited_once_with(
            job, "infrastructure", "seed_store_lock_timeout"
        )
        assert worker._healthy_slots == set()

    async def test_platform_infrastructure_failure_keeps_miner_attempt(self) -> None:
        jobs = [_job("5MinerA" + "x" * 41), _job("5MinerB" + "x" * 41)]
        platform = _platform_with_ledger(jobs=jobs, ledger=[])
        dittobench = MagicMock()
        dittobench.preflight = AsyncMock()
        dittobench.score_tarball = AsyncMock(
            side_effect=PlatformInfrastructureError(
                "inference exchange rejected (503) after 3 attempts"
            )
        )
        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=dittobench,
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )

        outcome = await worker.run_once(set_weights=False)

        assert outcome.queue_depth == 1
        assert platform.request_job.await_count == 1
        platform.report_ticket_failed.assert_awaited_once()
        failed_job, reason, detail = platform.report_ticket_failed.await_args.args
        assert failed_job is jobs[0]
        assert reason == "infrastructure"
        assert "inference exchange rejected (503)" in detail

    async def test_scoring_failure_hands_ticket_back_and_continues(self) -> None:
        jobs = [_job("5MinerA" + "x" * 41), _job("5MinerB" + "x" * 41)]
        platform = _platform_with_ledger(jobs=jobs, ledger=[])
        dittobench = MagicMock()
        dittobench.preflight = AsyncMock()
        # First job fails with an ordinary bench error, second scores cleanly.
        dittobench.score_tarball = AsyncMock(
            side_effect=[DittobenchError("harness crashed"), _report("run-b", 0.7)]
        )
        dittobench.last_details = {}
        chain = MagicMock(put_weights=AsyncMock())
        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=dittobench,
            chain=chain,
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )

        outcome = await worker.run_once(set_weights=False)

        # Both tickets were pulled (the scoring-error branch continues), and the
        # failed one was handed back as a scoring_error for fresh reissue.
        assert outcome.queue_depth == 2
        assert platform.request_job.await_count == 3  # two jobs + terminating 204
        platform.report_ticket_failed.assert_awaited_once()
        failed_job, reason, _ = platform.report_ticket_failed.await_args.args
        assert failed_job is jobs[0]
        assert reason == "scoring_error"

    async def test_zero_inference_hands_back_exact_agent_fault_without_a_grant(
        self,
    ) -> None:
        job = _job("5MinerA" + "x" * 41)
        platform = _platform_with_ledger(jobs=[job], ledger=[])
        dittobench = MagicMock()
        dittobench.preflight = AsyncMock()
        dittobench.score_tarball = AsyncMock(
            side_effect=DittobenchError(
                "run made no authoritative model call",
                code="model_inference_required",
            )
        )
        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=dittobench,
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )

        outcome = await worker.run_once(set_weights=False)

        assert outcome.queue_depth == 1
        assert platform.request_job.await_count == 2  # failed job + empty queue
        platform.report_ticket_failed.assert_awaited_once_with(
            job, "scoring_error", "model_inference_required"
        )
        platform.submit_score.assert_not_awaited()

    async def test_sandbox_oom_defers_harness_without_disabling_slot(self) -> None:
        jobs = [_job("5MinerA" + "x" * 41), _job("5MinerB" + "x" * 41)]
        platform = _platform_with_ledger(jobs=jobs, ledger=[])
        dittobench = MagicMock()
        dittobench.preflight = AsyncMock()
        dittobench.score_tarball = AsyncMock(
            side_effect=[SandboxOomError("memory allowance"), _report("run-b", 0.7)]
        )
        dittobench.last_details = {}
        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=dittobench,
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )

        outcome = await worker.run_once(set_weights=False)

        assert outcome.queue_depth == 2
        assert platform.request_job.await_count == 3
        platform.report_ticket_failed.assert_awaited_once_with(
            jobs[0], "sandbox_oom", "SandboxOomError: memory allowance"
        )
        assert "slot-0" in worker._healthy_slots
        assert "slot-0" not in worker._resource_blocked_until
        platform.submit_score.assert_awaited_once()

    async def test_ticket_hand_back_failure_never_crashes_the_sweep(self) -> None:
        jobs = [_job("5MinerA" + "x" * 41)]
        platform = _platform_with_ledger(jobs=jobs, ledger=[])
        # An old platform (or transport failure) makes the hand-back itself fail;
        # the sweep must swallow it exactly like the old expire-on-its-own path.
        platform.report_ticket_failed = AsyncMock(
            side_effect=PlatformError("no /job/fail endpoint")
        )
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

        outcome = await worker.run_once()

        assert outcome.queue_depth == 1
        assert outcome.weights_ran is True
        platform.report_ticket_failed.assert_awaited_once()
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

    async def test_platform_acceptance_is_revoked_by_rejection_or_failure(self) -> None:
        platform = _platform_with_ledger(jobs=[], ledger=[])
        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=MagicMock(),
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )

        assert await worker._report_heartbeat("idle") is True
        assert worker._platform_accepted is True
        platform.submit_heartbeat.return_value = ValidatorHeartbeatResponse(
            accepted=False, seen_at=datetime.now(UTC)
        )
        assert await worker._report_heartbeat("idle") is False
        assert worker._platform_accepted is False

        platform.submit_heartbeat.side_effect = PlatformError("offline")
        assert await worker._report_heartbeat("idle") is False
        assert worker._platform_accepted is False

    async def test_heartbeat_carries_collected_stack_health(self) -> None:
        platform = _platform_with_ledger(jobs=[], ledger=[])
        collected = fallback_stack_health().model_copy(
            update={
                "ollama": ValidatorComponentHealth(
                    health="unreachable", required=True, observed_at=5
                )
            }
        )
        collector = MagicMock()
        collector.collect = AsyncMock(return_value=collected)
        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=MagicMock(),
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
            stack_health=collector,
        )

        assert await worker._report_heartbeat("idle") is True
        heartbeat = platform.submit_heartbeat.await_args.args[0]
        assert heartbeat.stack_health == collected
        collector.collect.assert_awaited_once()
        # The collector observes the same stack identity and scorer capability
        # the signed heartbeat itself claims.
        kwargs = collector.collect.await_args.kwargs
        assert kwargs["stack"] == heartbeat.stack
        assert heartbeat.capabilities is not None
        assert kwargs["scorer"] == heartbeat.capabilities.scorer_benchmarks

    async def test_source_heartbeat_binds_observed_dittobench_version(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        revision = "ab" * 20
        monkeypatch.setenv("VALIDATOR_STACK_MODE", "source")
        monkeypatch.setenv(
            "VALIDATOR_STACK_COMPONENT_DITTOBENCH_API", f"source:{revision}"
        )
        scorer = ScorerBenchmarkCapability(
            status="fresh_verified",
            supported_bench_versions=(2, 3, 4, 5, 6, 7),
            observed_at=1,
            software_version="source-build",
            source_revision=revision,
            v7_calibration=V7InferenceCalibration(
                manifest_sha256="12" * 32,
                supported_routes=(
                    InferenceCalibrationRoute(
                        provider="amazon-bedrock",
                        profile_revision="openrouter-amazon-bedrock-gpt-oss-20b-v1",
                        model="openai/gpt-oss-20b",
                    ),
                ),
            ),
        )
        dittobench = MagicMock()
        dittobench.scorer_benchmark_capability = AsyncMock(return_value=scorer)
        platform = _platform_with_ledger(jobs=[], ledger=[])
        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=dittobench,
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )

        assert await worker._report_heartbeat("idle") is True
        heartbeat = platform.submit_heartbeat.await_args.args[0]
        assert heartbeat.stack is not None
        assert heartbeat.stack.components.dittobench_api.version == "source-build"
        assert heartbeat.stack.components.dittobench_api.source_revision == revision
        assert heartbeat.capabilities is not None
        assert heartbeat.protocol_version == HEARTBEAT_PROTOCOL_VERSION
        assert heartbeat.capabilities.signed_score_quorum is True
        assert heartbeat.capabilities.scorer_benchmarks == scorer

    async def test_heartbeat_carries_probe_evidence_or_says_it_has_none(self) -> None:
        """A v15 heartbeat never leaves the scorer's liveness simply unstated.

        A worker with no scorer client observes nothing, which is a real and
        reportable finding. Silently omitting the field would make it
        indistinguishable from an older validator that cannot report one.
        """
        platform = _platform_with_ledger(jobs=[], ledger=[])
        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=cast(Any, SimpleNamespace()),
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )

        assert await worker._report_heartbeat("idle") is True
        heartbeat = platform.submit_heartbeat.await_args.args[0]
        assert heartbeat.protocol_version == HEARTBEAT_PROTOCOL_VERSION
        assert heartbeat.capabilities is not None
        scorer = heartbeat.capabilities.scorer_benchmarks
        assert scorer is not None and scorer.probe is not None
        assert scorer.probe.outcome == "not_probed"
        assert scorer.probe.last_served_at is None

    async def test_collector_failure_degrades_to_unknown_stack_health(self) -> None:
        platform = _platform_with_ledger(jobs=[], ledger=[])
        collector = MagicMock()
        collector.collect = AsyncMock(side_effect=RuntimeError("probe sweep died"))
        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=MagicMock(),
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
            stack_health=collector,
        )

        assert await worker._report_heartbeat("idle") is True
        heartbeat = platform.submit_heartbeat.await_args.args[0]
        assert heartbeat.stack_health is not None
        assert heartbeat.stack_health.ditto_subnet.health == "healthy"
        assert heartbeat.stack_health.ollama is None

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

    async def test_required_ticket_inference_fails_as_validator_infrastructure(
        self,
    ) -> None:
        config = _config()
        config.dittobench_mock = False
        worker = ValidatorWorker(
            config=config,
            platform=_platform_with_ledger(jobs=[], ledger=[]),
            dittobench=MagicMock(),
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )

        with pytest.raises(
            ValidatorInfrastructureError,
            match="benchmark v8 requires platform inference",
        ):
            await worker._score_job(_job("5MinerA" + "x" * 41))

    @pytest.mark.parametrize("missing_identity", ["ticket", "exchange"])
    async def test_v8_rejects_incomplete_inference_route_identity(
        self, missing_identity: str
    ) -> None:
        deadline = datetime.now(UTC) + timedelta(hours=1)
        grant_id = uuid4()
        provider = None if missing_identity == "ticket" else "amazon-bedrock"
        profile = (
            None
            if missing_identity == "ticket"
            else "openrouter-amazon-bedrock-gpt-oss-20b-v1"
        )
        inference = InferenceGrantOffer(
            grant_id=grant_id,
            exchange_url="https://platform.test/exchange",
            proxy_url="https://platform.test/inference",
            allowed_models=["openai/gpt-oss-20b"],
            request_budget=401,
            token_budget=1_000_000,
            expires_at=deadline,
            provider=provider,
            profile_revision=profile,
        )
        exchange_provider = None if missing_identity == "exchange" else "amazon-bedrock"
        exchange_profile = (
            None
            if missing_identity == "exchange"
            else "openrouter-amazon-bedrock-gpt-oss-20b-v1"
        )
        exchange_model = (
            None if missing_identity == "exchange" else "openai/gpt-oss-20b"
        )
        platform = _platform_with_ledger(jobs=[], ledger=[])
        platform.exchange_inference_grant = AsyncMock(
            return_value=InferenceExchangeResponse(
                grant_id=grant_id,
                bearer="x" * 32,
                proxy_url=inference.proxy_url,
                expires_at=deadline,
                generation=1,
                provider=exchange_provider,
                profile_revision=exchange_profile,
                model=exchange_model,
            )
        )
        dittobench = MagicMock()
        dittobench.prepare_inference_session = AsyncMock(
            return_value=InferenceBrokerSession(
                session_id="session",
                activation_secret="activation",
                broker_public_key="a" * 43,
            )
        )
        dittobench.cancel_inference_session = AsyncMock()
        dittobench.activate_inference_session = AsyncMock()
        worker = ValidatorWorker(
            config=(_live_config := _config()),
            platform=platform,
            dittobench=dittobench,
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )
        _live_config.dittobench_mock = False
        job = _job("5MinerA" + "x" * 41, deadline=deadline).model_copy(
            update={
                "bench_version": 8,
                "minimum_screening_policy_version": 9,
                "requires_screened_image": True,
                "inference": inference,
            }
        )

        with pytest.raises(PlatformError, match="escaped ticket bounds"):
            await worker._score_job(job)
        dittobench.activate_inference_session.assert_not_awaited()
        dittobench.cancel_inference_session.assert_awaited_once_with("session")

    async def test_v8_binds_ticket_identity_through_activation_and_score(self) -> None:
        deadline = datetime.now(UTC) + timedelta(hours=1)
        grant_id = uuid4()
        profile_revision = "openrouter-amazon-bedrock-gpt-oss-20b-v1"
        inference = InferenceGrantOffer(
            grant_id=grant_id,
            exchange_url="https://platform.test/exchange",
            proxy_url="https://platform.test/inference",
            allowed_models=["openai/gpt-oss-20b"],
            request_budget=401,
            token_budget=1_000_000,
            expires_at=deadline,
            provider="amazon-bedrock",
            profile_revision=profile_revision,
        )
        job = _job(
            "5MinerA" + "x" * 41,
            slot_id="slot-3",
            deadline=deadline,
        ).model_copy(
            update={
                "bench_version": 8,
                "minimum_screening_policy_version": 9,
                "requires_screened_image": True,
                "inference": inference,
            }
        )
        platform = _platform_with_ledger(jobs=[], ledger=[])
        platform.exchange_inference_grant = AsyncMock(
            return_value=InferenceExchangeResponse(
                grant_id=grant_id,
                bearer="x" * 32,
                proxy_url=inference.proxy_url,
                expires_at=deadline,
                generation=1,
                provider="amazon-bedrock",
                profile_revision=profile_revision,
                model="openai/gpt-oss-20b",
            )
        )
        artifact = platform.get_artifact.return_value.model_copy(
            update={
                "agent_id": job.agent_id,
                "bench_version": 8,
                "screening_policy_version": 9,
            }
        )
        platform.get_artifact = AsyncMock(return_value=artifact)
        dittobench = MagicMock()
        dittobench.prepare_inference_session = AsyncMock(
            return_value=InferenceBrokerSession(
                session_id="session-v8",
                activation_secret="activation",
                broker_public_key="a" * 43,
            )
        )
        dittobench.activate_inference_session = AsyncMock()
        dittobench.cancel_inference_session = AsyncMock()
        dittobench.score_tarball = AsyncMock(
            return_value=_report("run-v8", 0.9).model_copy(update={"bench_version": 8})
        )
        worker = ValidatorWorker(
            config=(_live_config := _config()),
            platform=platform,
            dittobench=dittobench,
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )
        _live_config.dittobench_mock = False

        await worker._score_job(job)

        activation = dittobench.activate_inference_session.await_args.kwargs
        assert activation["grant_id"] == grant_id
        assert activation["agent_id"] == job.agent_id
        assert activation["slot_id"] == "slot-3"
        assert activation["ticket_deadline"] == deadline
        score = dittobench.score_tarball.await_args.kwargs
        assert score["inference_session_id"] == "session-v8"
        assert score["inference_grant_id"] == grant_id
        assert score["inference_agent_id"] == job.agent_id
        assert score["inference_slot_id"] == "slot-3"
        assert score["inference_ticket_deadline"] == deadline
        dittobench.cancel_inference_session.assert_awaited_once_with("session-v8")

    async def test_run_token_from_scorer_rides_every_progress_heartbeat(self) -> None:
        job = _job("5MinerA" + "x" * 41)
        platform = _platform_with_ledger(jobs=[], ledger=[])
        token = "abc123def4560000"

        async def score_with_token(  # type: ignore[no-untyped-def]
            *, progress_callback, **_
        ) -> ScoreReport:
            # preparing was already published (before the run id existed) with no
            # token; the scorer's first snapshot introduces the run identity.
            await progress_callback(
                DittobenchProgressSnapshot(
                    stage="running_benchmark",
                    completed=51,
                    total=114,
                    run_token=token,
                )
            )
            return _report("run", 0.9).model_copy(update={"n": 114})

        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=MagicMock(score_tarball=AsyncMock(side_effect=score_with_token)),
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )

        await worker._score_job(job)

        progress = [
            call.args[0].benchmark_progress
            for call in platform.submit_heartbeat.await_args_list
            if call.args[0].benchmark_progress is not None
        ]
        by_stage = {item.stage: item.run_token for item in progress}
        # preparing precedes the run id, so it legitimately carries no token.
        assert by_stage["preparing"] is None
        # Once learned, the token rides running/finalizing/submitting_result.
        assert by_stage["running_benchmark"] == token
        assert by_stage["finalizing"] == token
        assert by_stage["submitting_result"] == token
        # The active token is reset once the ticket clears.
        assert worker._active_run_token is None

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

    def test_progress_stage_order_is_exhaustive_over_the_wire_stages(self) -> None:
        # A stage missing here raises KeyError inside the monotonicity guard,
        # which the fail-open handler swallows: every update for that stage is
        # dropped silently and the dashboard freezes on the previous stage.
        declared = list(get_args(BenchmarkProgressStage))
        assert set(worker_mod._PROGRESS_STAGE_ORDER) == set(declared)
        # The Literal is declared in lifecycle order. Relay waiting is a
        # reversible running sub-state, so those two intentionally share rank.
        assert list(worker_mod._PROGRESS_STAGE_ORDER) == declared
        ranks = list(worker_mod._PROGRESS_STAGE_ORDER.values())
        assert ranks == sorted(ranks)
        assert (
            worker_mod._PROGRESS_STAGE_ORDER["waiting_for_relay"]
            == (worker_mod._PROGRESS_STAGE_ORDER["running_benchmark"])
        )

    async def test_generating_dataset_advances_past_preparing(self) -> None:
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
        assert worker._benchmark_progress is not None
        assert worker._benchmark_progress.stage == "preparing"
        sent = platform.submit_heartbeat.await_count

        assert await worker._publish_benchmark_progress("generating_dataset") is True
        assert platform.submit_heartbeat.await_count == sent + 1
        published = platform.submit_heartbeat.await_args_list[-1].args[0]
        assert published.benchmark_progress is not None
        assert published.benchmark_progress.stage == "generating_dataset"

        # It stays between building_harness and starting_harness, so neither the
        # deprecated earlier stage nor a re-poll of it can regress the lifecycle.
        assert await worker._publish_benchmark_progress("building_harness") is False
        assert worker._benchmark_progress.stage == "generating_dataset"
        assert await worker._publish_benchmark_progress("starting_harness") is True
        assert worker._benchmark_progress.stage == "starting_harness"

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

    async def test_parallel_heartbeats_coalesce_without_future_timestamp(self) -> None:
        platform = _platform_with_ledger(jobs=[], ledger=[])
        clock = _ControlledHeartbeatClock()
        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=MagicMock(),
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
            heartbeat_clock=clock,
        )

        assert await worker._report_heartbeat("polling") is True
        first = asyncio.create_task(worker._report_heartbeat("updating_weights"))
        await clock.sleep_started.wait()
        second = asyncio.create_task(worker._report_heartbeat("polling"))
        await asyncio.sleep(0)

        assert platform.submit_heartbeat.await_count == 1
        clock.advance_to_next_second()
        assert await asyncio.gather(first, second) == [True, True]

        heartbeats = [
            call.args[0] for call in platform.submit_heartbeat.await_args_list
        ]
        assert len(heartbeats) == 2
        assert [heartbeat.timestamp for heartbeat in heartbeats] == [
            1_000_000,
            1_000_001,
        ]
        assert heartbeats[-1].timestamp <= int(clock.time())
        assert heartbeats[-1].state == "polling"

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
            seed=12345,
            dataset_sha256="cd" * 32,
            run_size="full",
            bench_version=8,
            ticket_deadline=ticket_deadline,
        )

        assert result.n == n
        platform.submit_score.assert_awaited_once()

    async def test_hanging_progress_heartbeat_cannot_block_terminal_submit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(worker_mod, "_ACTIVE_TELEMETRY_TIMEOUT_SECONDS", 0.001)
        monkeypatch.setattr(worker_mod, "_ACTIVE_TELEMETRY_HARD_TIMEOUT_SECONDS", 0.01)
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
        # Detached sends remain hard-bounded even when the transport never
        # returns, so the worker does not accumulate telemetry tasks.
        detached = tuple(worker._background_heartbeat_tasks)
        if detached:
            await asyncio.gather(*detached, return_exceptions=True)
            # Give each task's synchronous done callback one event-loop turn to
            # consume its result and release the worker's strong reference.
            await asyncio.sleep(0)
        assert not worker._background_heartbeat_tasks

    async def test_slow_preparing_heartbeat_finishes_after_caller_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A busy heartbeat lane must not erase the first slot snapshot."""
        monkeypatch.setattr(worker_mod, "_ACTIVE_TELEMETRY_TIMEOUT_SECONDS", 0.001)
        monkeypatch.setattr(worker_mod, "_ACTIVE_TELEMETRY_HARD_TIMEOUT_SECONDS", 1.0)
        platform = _platform_with_ledger(jobs=[], ledger=[])
        send_started = asyncio.Event()
        allow_send = asyncio.Event()
        accepted = ValidatorHeartbeatResponse(accepted=True, seen_at=datetime.now(UTC))

        async def slow_send(_: object) -> ValidatorHeartbeatResponse:
            send_started.set()
            await allow_send.wait()
            return accepted

        platform.submit_heartbeat = AsyncMock(side_effect=slow_send)
        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=MagicMock(),
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )
        agent_id = uuid4()
        deadline = datetime.now(UTC) + timedelta(hours=1)

        await worker._begin_active_ticket(agent_id, deadline)

        await send_started.wait()
        assert worker._background_heartbeat_tasks
        request = platform.submit_heartbeat.await_args.args[0]
        assert request.active_agent_id == agent_id
        assert request.benchmark_progress is not None
        assert request.benchmark_progress.stage == "preparing"

        allow_send.set()
        for _ in range(100):
            if not worker._background_heartbeat_tasks:
                break
            await asyncio.sleep(0)
        assert not worker._background_heartbeat_tasks
        assert worker._last_progress_heartbeat_monotonic is not None

    async def test_unticketed_rescore_is_rejected(self) -> None:
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

        with pytest.raises(PlatformError, match="supported versions are"):
            await worker._evaluate(uuid4(), "ab" * 32, seed=7)
        dittobench.score_tarball.assert_not_awaited()

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
        assert kwargs["screened_image_url"].startswith("https://")
        assert kwargs["screened_image_sha256"] == "12" * 32
        assert kwargs["screened_image_size_bytes"] == 123
        assert kwargs["screened_image_id"] == "sha256:" + "34" * 32
        assert kwargs["screened_image_ref"] == (
            "ditto-screen/550e8400-e29b-41d4-a716-446655440000:latest"
        )

    @pytest.mark.parametrize("bench_version", [8, 9])
    async def test_ticket_artifact_scorer_and_signature_are_version_bound(
        self, bench_version: int
    ) -> None:
        job = _job("5MinerA" + "x" * 41).model_copy(
            update={
                "bench_version": bench_version,
                "minimum_screening_policy_version": 9,
                "requires_screened_image": True,
            }
        )
        platform = _platform_with_ledger(jobs=[job], ledger=[])
        artifact = platform.get_artifact.return_value.model_copy(
            update={
                "bench_version": bench_version,
                "screening_policy_version": 9,
                "screened_image_url": "https://signed.example/image.tar",
                "screened_image_sha256": "12" * 32,
                "screened_image_size_bytes": 123,
                "screened_image_id": "sha256:" + "34" * 32,
                "screened_image_ref": (
                    "ditto-screen/550e8400-e29b-41d4-a716-446655440000:latest"
                ),
            }
        )
        platform.get_artifact = AsyncMock(return_value=artifact)
        report_update: dict[str, object] = {"bench_version": bench_version}
        transcript = b'{"v9":"transcript"}'
        if bench_version == 9:
            report_update.update(
                {
                    "details": {
                        "bench_version": 9,
                        "transcript_sha256": hashlib.sha256(transcript).hexdigest(),
                    },
                    "base_evidence_sha256": "56" * 32,
                }
            )
            platform.submit_transcript = AsyncMock()
        report = _report(f"run-v{bench_version}", 0.9).model_copy(update=report_update)
        dittobench = MagicMock(
            score_tarball=AsyncMock(return_value=report),
            last_details={"bench_version": bench_version},
            take_transcript=MagicMock(return_value=transcript),
        )
        keypair = MagicMock(sign=MagicMock(return_value=b"\x01" * 64))
        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=dittobench,
            chain=MagicMock(),
            keypair=keypair,
        )

        await worker._score_job(job)

        kwargs = dittobench.score_tarball.await_args.kwargs
        assert kwargs["bench_version"] == bench_version
        domain = f":{bench_version}".encode()
        assert any(domain in call.args[0] for call in keypair.sign.call_args_list)
        assert (
            platform.submit_score.await_args.kwargs["report"].bench_version
            == bench_version
        )
        if bench_version == 9:
            platform.submit_transcript.assert_awaited_once()

    async def test_ticket_artifact_benchmark_version_mismatch_is_terminal(self) -> None:
        job = _job("5MinerA" + "x" * 41).model_copy(
            update={
                "bench_version": 8,
                "minimum_screening_policy_version": 9,
                "requires_screened_image": True,
            }
        )
        platform = _platform_with_ledger(jobs=[], ledger=[])
        artifact = platform.get_artifact.return_value.model_copy(
            update={"bench_version": 7}
        )
        platform.get_artifact = AsyncMock(return_value=artifact)
        dittobench = MagicMock(score_tarball=AsyncMock())
        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=dittobench,
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )

        with pytest.raises(PlatformError, match="benchmark version mismatch"):
            await worker._score_job(job)
        dittobench.score_tarball.assert_not_awaited()

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

        assert n.queue_depth == 1  # the item was pulled...
        dittobench.score_tarball.assert_not_awaited()  # ...but never scored
        platform.submit_score.assert_not_awaited()
        chain.put_weights.assert_awaited_once_with({"5Champ" + "x" * 42: 1.0})

    async def test_invalid_artifact_skips_only_its_ticket(self) -> None:
        first = _job("5MinerA" + "x" * 41)
        second = _job("5MinerB" + "x" * 41)
        platform = _platform_with_ledger(jobs=[first, second], ledger=[])
        valid_artifact = ArtifactResponse(
            agent_id=second.agent_id,
            sha256=second.sha256,
            download_url="https://signed.example/second.tar.gz?sig=1",
            expires_at=datetime.now(UTC),
            bench_version=8,
            screening_policy_version=9,
            screened_image_url="https://signed.example/image.tar",
            screened_image_sha256="12" * 32,
            screened_image_size_bytes=123,
            screened_image_id="sha256:" + "34" * 32,
            screened_image_ref=(
                "ditto-screen/550e8400-e29b-41d4-a716-446655440000:latest"
            ),
        )
        platform.get_artifact = AsyncMock(
            side_effect=[PlatformError("artifact response was invalid"), valid_artifact]
        )
        dittobench = MagicMock(
            score_tarball=AsyncMock(return_value=_report("second", 0.9))
        )
        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=dittobench,
            chain=MagicMock(put_weights=AsyncMock()),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )

        outcome = await worker.run_once()

        assert outcome.queue_depth == 2
        dittobench.score_tarball.assert_awaited_once()
        platform.submit_score.assert_awaited_once()

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

        assert n.queue_depth == 1  # counted against the sweep cap...
        platform.get_artifact.assert_not_awaited()  # ...but refused unscored
        dittobench.score_tarball.assert_not_awaited()
        platform.submit_score.assert_not_awaited()
        chain.put_weights.assert_awaited_once_with({"5Champ" + "x" * 42: 1.0})

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

    async def test_validator_scoped_seed_ticket_is_scored(self) -> None:
        agent_id = uuid4()
        block_hash = "0x" + "56" * 32
        validator_hotkey = _config().validator_hotkey
        job = _job("5MinerA" + "x" * 41).model_copy(
            update={
                "agent_id": agent_id,
                "dataset_seed_block_hash": block_hash,
                "seed_scope": "validator",
                "seed": derive_seed(block_hash, agent_id, validator_hotkey),
            }
        )
        platform = _platform_with_ledger(
            jobs=[job], ledger=[_entry("5MinerA" + "x" * 41, 0.9)]
        )
        dittobench = MagicMock()
        dittobench.score_tarball = AsyncMock(return_value=_report("run", 0.9))
        keypair = MagicMock(sign=MagicMock(return_value=b"\x01" * 64))
        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=dittobench,
            chain=MagicMock(put_weights=AsyncMock()),
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

        assert n.queue_depth == 1  # counted against the sweep cap...
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

        assert (await worker.run_once()).queue_depth == 0
        chain.put_weights.assert_awaited_once_with({"5Champion" + "x" * 39: 1.0})

    async def test_deregistered_champion_is_filtered_before_koth_fold(self) -> None:
        absent = "5Absent" + "x" * 40
        registered = "5Registered" + "x" * 36
        ledger = [_entry(absent, 0.90), _entry(registered, 0.80)]
        platform = _platform_with_ledger(jobs=[], ledger=ledger)
        chain = MagicMock()
        chain.get_recent_neurons = AsyncMock(
            return_value=[SimpleNamespace(hotkey=registered)]
        )
        chain.put_weights = AsyncMock()

        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=MagicMock(),
            chain=chain,
            keypair=MagicMock(),
        )

        await worker.run_once()
        chain.put_weights.assert_awaited_once_with({registered: 1.0})

    async def test_chain_registration_read_failure_leaves_weights_unchanged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        miner = "5Miner" + "x" * 42
        platform = _platform_with_ledger(jobs=[], ledger=[_entry(miner, 0.90)])
        chain = MagicMock()
        chain.get_recent_neurons = AsyncMock(side_effect=ChainError("pylon down"))
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
        chain.put_weights.assert_not_awaited()
        assert "weights unchanged" in caplog.text

    async def test_only_same_hotkey_reregistration_restores_eligibility(self) -> None:
        original = "5Original" + "x" * 39
        replacement = "5Replacement" + "x" * 36
        platform = _platform_with_ledger(jobs=[], ledger=[_entry(original, 0.90)])
        platform.request_job = AsyncMock(side_effect=[None, None])
        chain = MagicMock()
        chain.get_recent_neurons = AsyncMock(
            side_effect=[
                [SimpleNamespace(hotkey=replacement)],
                [SimpleNamespace(hotkey=original), SimpleNamespace(hotkey=replacement)],
            ]
        )
        chain.put_weights = AsyncMock()

        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=MagicMock(),
            chain=chain,
            keypair=MagicMock(),
        )

        await worker.run_once()
        await worker.run_once()
        assert chain.put_weights.await_args_list[0].args == ({_BURN_HOTKEY: 1.0},)
        assert chain.put_weights.await_args_list[1].args == ({original: 1.0},)
        assert all(
            replacement not in call.args[0]
            for call in chain.put_weights.await_args_list
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
        assert (await worker.run_once()).queue_depth == 0
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

        assert (await worker.run_once()).queue_depth == 0
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

        assert (await worker.run_once()).queue_depth == 0
        chain.put_weights.assert_awaited_once_with({"5Champion" + "x" * 39: 1.0})

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
        chain.put_weights.assert_awaited_once_with({"5Champion" + "x" * 39: 1.0})
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
        assert (await worker.run_once()).queue_depth == 0
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
        assert (await worker.run_once()).queue_depth == 0
        chain.put_weights.assert_awaited_once_with({"5Champion" + "x" * 39: 1.0})

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
        assert (await worker.run_once()).queue_depth == 0
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
        assert (await worker.run_once()).queue_depth == 0
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
        assert (await worker.run_once()).queue_depth == 0
        chain.put_weights.assert_awaited_once_with({"5Champion" + "x" * 39: 1.0})

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
        assert (await worker.run_once()).queue_depth == 0
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
        assert (await worker.run_once()).queue_depth == 0
        chain.get_stake_tao.assert_not_awaited()
        chain.put_weights.assert_awaited_once()

    async def test_set_weights_false_scores_without_touching_weights(self) -> None:
        # Canonical work is claimed first. Once every slot has observed that
        # queue empty, spare capacity may inspect retests in the same sweep.
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
        assert n.queue_depth == 1
        assert platform.submit_score.await_count == 1
        platform.get_ledger.assert_awaited_once()
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
        assert n.queue_depth == 2
        # The lone eligible miner takes the whole miner emission; nothing burns.
        chain.put_weights.assert_awaited_once_with({"5MinerG" + "x" * 41: 1.0})

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
        assert (await worker.run_once()).queue_depth == 0
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

    async def test_floor_uses_full_commit_reveal_tempo(self) -> None:
        chain = MagicMock()
        chain.get_weights_rate_limit = AsyncMock(return_value=100)
        chain.get_tempo = AsyncMock(return_value=360)
        assert await self._worker(chain)._chain_min_epoch_seconds() == 4320.0

    async def test_missing_tempo_keeps_rate_limit_floor(self) -> None:
        chain = MagicMock()
        chain.get_weights_rate_limit = AsyncMock(return_value=100)
        chain.get_tempo = AsyncMock(return_value=None)
        assert await self._worker(chain)._chain_min_epoch_seconds() == 1200.0

    async def test_startup_waits_until_last_update_is_tempo_old(self) -> None:
        chain = MagicMock()
        chain.get_last_update_block = AsyncMock(return_value=1_000)
        chain.get_latest_block = AsyncMock(return_value=SimpleNamespace(number=1_300))
        worker = self._worker(chain)

        # A 360-block cadence has 60 blocks left; add one safety block.
        assert await worker._seconds_until_weight_window(4320.0) == 732.0

    async def test_due_or_unobservable_window_never_blocks_liveness(self) -> None:
        due_chain = MagicMock()
        due_chain.get_last_update_block = AsyncMock(return_value=1_000)
        due_chain.get_latest_block = AsyncMock(
            return_value=SimpleNamespace(number=1_360)
        )
        assert await self._worker(due_chain)._seconds_until_weight_window(4320.0) == 0.0

        unavailable_chain = MagicMock()
        unavailable_chain.get_last_update_block = AsyncMock(
            side_effect=ChainError("pylon down")
        )
        unavailable_chain.get_latest_block = AsyncMock()
        assert (
            await self._worker(unavailable_chain)._seconds_until_weight_window(4320.0)
            == 0.0
        )

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


class TestIndependentWeightLoop:
    @staticmethod
    def _worker_observing(
        entries: list[LedgerEntry], *, enforce: bool = False
    ) -> ValidatorWorker:
        platform = MagicMock()
        platform.get_ledger = AsyncMock(
            return_value=LedgerResponse(
                entries=entries,
                count=len(entries),
                v9_confirmation_mode="enforce" if enforce else None,
            )
        )
        return ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=MagicMock(),
            chain=MagicMock(),
            keypair=MagicMock(),
        )

    async def test_king_observer_ignores_unconfirmed_v9_in_mixed_ledger(
        self,
    ) -> None:
        v8 = _entry("5V8" + "x" * 44, 0.70, bench_version=8)
        unconfirmed_v9 = _entry("5V9" + "x" * 44, 0.99, bench_version=9)
        worker = self._worker_observing([unconfirmed_v9, v8], enforce=True)

        available, fingerprint = await worker._observe_platform_king()

        assert available
        assert fingerprint == worker._king_fingerprint(v8)

    async def test_king_observer_reports_no_champion_for_v9_only_ledger(
        self,
    ) -> None:
        unconfirmed_v9 = _entry("5V9" + "x" * 44, 0.99, bench_version=9)
        worker = self._worker_observing([unconfirmed_v9], enforce=True)

        available, fingerprint = await worker._observe_platform_king()

        assert available
        assert fingerprint is None

    async def test_king_observer_accepts_ordinary_v9_while_shadowing(self) -> None:
        ordinary_v9 = _entry("5V9" + "x" * 44, 0.99, bench_version=9)
        worker = self._worker_observing([ordinary_v9])

        available, fingerprint = await worker._observe_platform_king()

        assert available
        assert fingerprint == worker._king_fingerprint(ordinary_v9)

    async def test_king_observer_accepts_confirmed_v9(self) -> None:
        confirmed_v9 = _entry("5V9" + "x" * 44, 0.99, bench_version=9).model_copy(
            update={"v9_confirmation": SimpleNamespace(full_effective_micros=990_000)}
        )
        worker = self._worker_observing([confirmed_v9])

        available, fingerprint = await worker._observe_platform_king()

        assert available
        assert fingerprint == worker._king_fingerprint(confirmed_v9)

    async def test_king_event_never_bypasses_local_commit_reveal_floor(self) -> None:
        config = _config()
        config.sweep_seconds = 0.005
        worker = ValidatorWorker(
            config=config,
            platform=MagicMock(),
            dittobench=MagicMock(),
            chain=MagicMock(),
            keypair=MagicMock(),
        )
        worker._ledger_changed.set()
        worker._seconds_until_weight_window = AsyncMock(return_value=0.0)  # type: ignore[method-assign]
        worker._observe_platform_king = AsyncMock(  # type: ignore[method-assign]
            return_value=(
                True,
                (
                    "5Miner" + "x" * 41,
                    UUID("550e8400-e29b-41d4-a716-446655440000"),
                    0.9,
                    4,
                ),
            )
        )

        started = time.monotonic()
        await worker._wait_for_king_or_weight_window(
            asyncio.Event(),
            epoch_seconds=0.03,
            baseline=None,
            drain_requested=None,
        )

        assert time.monotonic() - started >= 0.02
        worker._observe_platform_king.assert_awaited()

    async def test_weights_run_while_scoring_sweep_is_still_busy(self) -> None:
        scoring_started = asyncio.Event()
        release_scoring = asyncio.Event()
        stop = asyncio.Event()
        telemetry = MagicMock()
        worker = ValidatorWorker(
            config=_config(),
            platform=MagicMock(),
            dittobench=MagicMock(),
            chain=MagicMock(),
            keypair=MagicMock(),
            telemetry=telemetry,
        )

        async def _busy_scoring(*, set_weights: bool) -> int:
            assert set_weights is False
            scoring_started.set()
            await release_scoring.wait()
            return 1

        worker.run_once = AsyncMock(side_effect=_busy_scoring)  # type: ignore[method-assign]
        worker._chain_min_epoch_seconds = AsyncMock(return_value=0.0)  # type: ignore[method-assign]
        worker._seconds_until_weight_window = AsyncMock(return_value=0.0)  # type: ignore[method-assign]
        worker._update_weights = AsyncMock(  # type: ignore[method-assign]
            return_value=worker_mod._WeightOutcome(
                weights={_BURN_HOTKEY: 1.0},
                submitted=True,
            )
        )
        worker._observe_onchain_weight_state = AsyncMock(  # type: ignore[method-assign]
            return_value=(100, 110)
        )
        worker._report_heartbeat = AsyncMock()  # type: ignore[method-assign]

        task = asyncio.create_task(worker.run_forever(stop))
        await asyncio.wait_for(scoring_started.wait(), timeout=1.0)
        # The weight update completes even though the scoring sweep remains
        # deliberately blocked.
        for _ in range(20):
            if worker._update_weights.await_count:
                break
            await asyncio.sleep(0)
        assert worker._update_weights.await_count == 1
        release_scoring.set()
        stop.set()
        await asyncio.wait_for(task, timeout=1.0)

        stats = telemetry.record_sweep.call_args.args[0]
        assert stats.weights_due is True
        assert stats.weights_submitted is True
        assert stats.onchain_last_update_block == 100
        assert stats.onchain_observed_block == 110
        assert stats.scoring_sweep is False

    async def test_drain_ack_waits_for_active_weight_update(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        weight_started = asyncio.Event()
        release_weight = asyncio.Event()
        stop = asyncio.Event()
        drain = asyncio.Event()
        config = _config()
        config.sweep_seconds = 0.001
        worker = ValidatorWorker(
            config=config,
            platform=MagicMock(),
            dittobench=MagicMock(),
            chain=MagicMock(),
            keypair=MagicMock(),
        )
        worker.run_once = AsyncMock(return_value=0)  # type: ignore[method-assign]
        worker._chain_min_epoch_seconds = AsyncMock(return_value=0.0)  # type: ignore[method-assign]
        worker._seconds_until_weight_window = AsyncMock(return_value=0.0)  # type: ignore[method-assign]

        async def blocked_weight_update() -> worker_mod._WeightOutcome:
            weight_started.set()
            await release_weight.wait()
            return worker_mod._WeightOutcome(submitted=True)

        worker._update_weights = blocked_weight_update  # type: ignore[method-assign]
        worker._observe_onchain_weight_state = AsyncMock(return_value=(1, 2))  # type: ignore[method-assign]
        worker._report_heartbeat = AsyncMock()  # type: ignore[method-assign]
        states: list[str] = []
        monkeypatch.setattr(
            worker_mod,
            "write_update_state",
            lambda state, **_kwargs: states.append(state),
        )

        task = asyncio.create_task(worker.run_forever(stop, drain_requested=drain))
        await asyncio.wait_for(weight_started.wait(), timeout=1)
        drain.set()
        for _ in range(10):
            await asyncio.sleep(0)
        assert "drained" not in states

        release_weight.set()
        for _ in range(100):
            if "drained" in states:
                break
            await asyncio.sleep(0.001)
        assert "drained" in states
        drain.clear()
        stop.set()
        await asyncio.wait_for(task, timeout=1)

    async def test_failed_sweep_rewrites_stale_platform_acceptance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stop = asyncio.Event()
        worker = ValidatorWorker(
            config=_config(),
            platform=MagicMock(),
            dittobench=MagicMock(),
            chain=MagicMock(),
            keypair=MagicMock(),
        )
        worker._platform_accepted = True
        worker.run_once = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
        worker._chain_min_epoch_seconds = AsyncMock(return_value=0.0)  # type: ignore[method-assign]
        worker._seconds_until_weight_window = AsyncMock(return_value=0.0)  # type: ignore[method-assign]

        async def report_heartbeat(state: str, **_: object) -> bool:
            if state == "error":
                worker._platform_accepted = False
                stop.set()
            return worker._platform_accepted

        worker._report_heartbeat = report_heartbeat  # type: ignore[assignment]
        states: list[tuple[str, bool]] = []
        monkeypatch.setattr(
            worker_mod,
            "write_update_state",
            lambda state, **kwargs: states.append(
                (state, bool(kwargs.get("platform_accepted")))
            ),
        )

        await asyncio.wait_for(worker.run_forever(stop), timeout=1)

        assert ("ready", True) in states
        assert ("working", False) in states

    async def test_onchain_observation_reports_real_block_age_inputs(self) -> None:
        chain = MagicMock()
        chain.get_last_update_block = AsyncMock(return_value=123)
        head = MagicMock()
        head.number = 150
        chain.get_latest_block = AsyncMock(return_value=head)
        worker = ValidatorWorker(
            config=_config(),
            platform=MagicMock(),
            dittobench=MagicMock(),
            chain=chain,
            keypair=MagicMock(),
        )

        assert await worker._observe_onchain_weight_state() == (123, 150)


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
        # The median representative (seed 10) is deliberately not evaluated
        # last, proving transcript publication is keyed by run id.
        composites = {10: 0.80, 20: 0.90, 30: 0.70}
        transcripts = {seed: f'{{"seed":{seed}}}'.encode() for seed in composites}

        async def _score(**kw: Any) -> ScoreReport:
            seed = kw["seed"]
            return self._seeded_report(seed, composites[seed]).model_copy(
                update={
                    "bench_version": 8,
                    "details": {
                        "bench_version": 8,
                        "transcript_sha256": hashlib.sha256(
                            transcripts[seed]
                        ).hexdigest(),
                    },
                }
            )

        dittobench = MagicMock()
        dittobench.score_tarball = AsyncMock(side_effect=_score)
        dittobench.take_transcript = MagicMock(
            side_effect=lambda run_id: transcripts[int(run_id.removeprefix("run-"))]
        )
        dittobench.last_details = {"bench_version": 8}
        platform = _platform_with_ledger(jobs=[], ledger=[])
        platform.submit_transcript = AsyncMock()
        platform.get_artifact = AsyncMock(
            return_value=ArtifactResponse(
                agent_id=agent_id,
                sha256="ab" * 32,
                download_url="https://signed.example/x.tar.gz",
                expires_at=datetime.now(UTC),
                bench_version=8,
                screening_policy_version=9,
                screened_image_url="https://signed.example/image.tar",
                screened_image_sha256="12" * 32,
                screened_image_size_bytes=123,
                screened_image_id="sha256:" + "34" * 32,
                screened_image_ref=(
                    "ditto-screen/550e8400-e29b-41d4-a716-446655440000:latest"
                ),
            )
        )
        w = await self._worker(dittobench, platform)
        out = await w._confirm_and_submit(
            agent_id,
            "ab" * 32,
            "5Miner" + "x" * 42,
            bench_version=8,
            seeds=[10, 20, 30],
        )

        # One evaluation per seed, but exactly one submitted score.
        assert dittobench.score_tarball.await_count == 3
        assert platform.submit_score.await_count == 1
        report = platform.submit_score.await_args.kwargs["report"]
        # The representative is a real run (the median composite/seed), and it
        # carries the sorted per-seed composites for the fold's median.
        assert report.composite == 0.80
        assert report.seed == 10
        # Seed-aligned and seed-sorted: seeds [10, 20, 30] -> composites in that
        # seed order, so a later paired dethrone can intersect on shared seeds.
        assert report.confirmation_seeds == [10, 20, 30]
        assert report.confirmation_composites == [0.80, 0.90, 0.70]
        # composite_stderr is pooled over the seeds: the per-seed reports carry
        # no single-run stderr, so it is the between-seed SEM
        # stdev([.7,.8,.9]) / sqrt(3) = 0.1 / sqrt(3).
        assert report.composite_stderr == pytest.approx(0.1 / 3**0.5)
        dittobench.take_transcript.assert_called_once_with("run-10")
        platform.submit_transcript.assert_awaited_once()
        assert platform.submit_transcript.await_args.kwargs["run_id"] == "run-10"
        assert platform.submit_transcript.await_args.kwargs["body"] == transcripts[10]
        assert out is report

    async def test_single_surviving_seed_has_no_confirmations(self) -> None:
        # If all but one seed fail, this degrades to a plain single-seed submit
        # (no confirmation_composites, so the fold uses the raw composite).
        agent_id = uuid4()

        async def _score(**kw: Any) -> ScoreReport:
            if kw["seed"] == 20:
                return self._seeded_report(20, 0.75).model_copy(
                    update={"bench_version": 8, "details": {"bench_version": 8}}
                )
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
                bench_version=8,
                screening_policy_version=9,
                screened_image_url="https://signed.example/image.tar",
                screened_image_sha256="12" * 32,
                screened_image_size_bytes=123,
                screened_image_id="sha256:" + "34" * 32,
                screened_image_ref=(
                    "ditto-screen/550e8400-e29b-41d4-a716-446655440000:latest"
                ),
            )
        )
        w = await self._worker(dittobench, platform)
        report = await w._confirm_and_submit(
            agent_id,
            "ab" * 32,
            "5Miner" + "x" * 42,
            bench_version=8,
            seeds=[10, 20, 30],
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
                bench_version=7,
                screening_policy_version=9,
                screened_image_url="https://signed.example/image.tar",
                screened_image_sha256="12" * 32,
                screened_image_size_bytes=123,
                screened_image_id="sha256:" + "34" * 32,
                screened_image_ref=(
                    "ditto-screen/550e8400-e29b-41d4-a716-446655440000:latest"
                ),
            )
        )
        w = await self._worker(dittobench, platform)
        out = await w._confirm_and_submit(
            agent_id,
            "ab" * 32,
            "5Miner" + "x" * 42,
            bench_version=7,
            seeds=[10, 20, 30],
        )
        assert out is None
        platform.submit_score.assert_not_awaited()


class TestPooledConfirmationStderr:
    def test_none_below_two_seeds(self) -> None:
        assert worker_mod._pooled_confirmation_stderr([], None) is None
        assert worker_mod._pooled_confirmation_stderr([0.8], 0.03) is None

    def test_between_seed_sem_when_no_single_run_stderr(self) -> None:
        # stdev([0.7, 0.8, 0.9]) = 0.1, SEM = 0.1 / sqrt(3).
        got = worker_mod._pooled_confirmation_stderr([0.7, 0.8, 0.9], None)
        assert got == pytest.approx(0.1 / 3**0.5)

    def test_sampling_floor_dominates_when_seeds_agree(self) -> None:
        # Seeds agree exactly (between-seed SEM = 0), so the pooled value falls
        # back to the single-run floor stderr / sqrt(K) instead of collapsing.
        got = worker_mod._pooled_confirmation_stderr([0.8, 0.8, 0.8], 0.03)
        assert got == pytest.approx(0.03 / 3**0.5)

    def test_between_seed_sem_dominates_when_seeds_disagree(self) -> None:
        # Wide between-seed spread beats the sampling floor: the band widens.
        got = worker_mod._pooled_confirmation_stderr([0.6, 0.8, 1.0], 0.03)
        assert got is not None
        between = (0.2**2 + 0.0 + 0.2**2) / 2  # sample var, ddof=1
        assert got == pytest.approx((between**0.5) / 3**0.5)
        assert got > 0.03 / 3**0.5

    def test_shrinks_by_root_k_vs_one_run(self) -> None:
        # Three agreeing seeds pool the one-run stderr down by ~sqrt(3): the
        # exact free win that lets the flat margin drop under the z-band floor.
        one_run = 0.0293
        got = worker_mod._pooled_confirmation_stderr([0.88, 0.88, 0.88], one_run)
        assert got == pytest.approx(one_run / 3**0.5, rel=1e-6)


class TestTranscriptPublication:
    """Offline reproducibility (v3 finding 3): the worker binds a declared
    transcript digest into the score signature and publishes the held bytes
    after the score is accepted — best-effort, never unwinding the score."""

    _DIGEST = "cd" * 32

    def _report_with_transcript(self) -> ScoreReport:
        report = _report("run_t", 0.9)
        return report.model_copy(
            update={
                "bench_version": 3,
                "details": {
                    "bench_version": 3,
                    "transcript_sha256": self._DIGEST,
                },
            }
        )

    async def test_submit_signs_digest_and_publishes_transcript(self) -> None:
        platform = _platform_with_ledger(jobs=[], ledger=[])
        platform.submit_transcript = AsyncMock()
        dittobench = MagicMock()
        dittobench.take_transcript = MagicMock(return_value=b'{"cases":[]}')
        signed_messages: list[bytes] = []

        def _capture_sign(message: bytes) -> bytes:
            signed_messages.append(message)
            return b"\x01" * 64

        keypair = MagicMock(sign=MagicMock(side_effect=_capture_sign))
        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=dittobench,
            chain=MagicMock(),
            keypair=keypair,
        )

        await worker._submit_report(
            uuid4(), "5MinerA" + "x" * 41, self._report_with_transcript()
        )

        assert len(signed_messages) == 1
        assert signed_messages[0].endswith(f":3:{self._DIGEST}".encode())
        dittobench.take_transcript.assert_called_once_with("run_t")
        platform.submit_score.assert_awaited_once()
        platform.submit_transcript.assert_awaited_once()
        kwargs = platform.submit_transcript.await_args.kwargs
        assert kwargs["run_id"] == "run_t"
        assert kwargs["body"] == b'{"cases":[]}'

    async def test_transcript_publication_failure_never_unwinds_score(self) -> None:
        platform = _platform_with_ledger(jobs=[], ledger=[])
        platform.submit_transcript = AsyncMock(
            side_effect=PlatformError("digest mismatch")
        )
        dittobench = MagicMock()
        dittobench.take_transcript = MagicMock(return_value=b'{"cases":[]}')
        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=dittobench,
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )

        report = await worker._submit_report(
            uuid4(), "5MinerA" + "x" * 41, self._report_with_transcript()
        )
        assert report.composite == 0.9
        platform.submit_score.assert_awaited_once()

    async def test_report_without_transcript_keeps_legacy_signing(self) -> None:
        platform = _platform_with_ledger(jobs=[], ledger=[])
        platform.submit_transcript = AsyncMock()
        signed_messages: list[bytes] = []

        def _capture_sign(message: bytes) -> bytes:
            signed_messages.append(message)
            return b"\x01" * 64

        keypair = MagicMock(sign=MagicMock(side_effect=_capture_sign))
        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=MagicMock(take_transcript=MagicMock(return_value=None)),
            chain=MagicMock(),
            keypair=keypair,
        )

        await worker._submit_report(uuid4(), "5MinerA" + "x" * 41, _report("run", 0.9))

        assert len(signed_messages) == 1
        assert b"transcript" not in signed_messages[0]
        assert not signed_messages[0].endswith(f":{self._DIGEST}".encode())
        platform.submit_transcript.assert_not_awaited()


class TestContestedDethroneConfirmation:
    """Near-band crown contests re-score the whole contested set on common CRN
    seeds so the fold decides on the paired statistic instead of seed luck."""

    def _seeded_report(self, seed: int, composite: float) -> ScoreReport:
        r = _report(f"run-{seed}", composite)
        return r.model_copy(update={"seed": seed})

    def _worker(self, dittobench: MagicMock, platform: MagicMock) -> ValidatorWorker:
        cfg = _config()
        cfg.koth_confirmation_seeds = 3
        return ValidatorWorker(
            config=cfg,
            platform=platform,
            dittobench=dittobench,
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )

    async def test_in_band_contest_confirms_both_on_common_seeds(self) -> None:
        # Deficit 0.005 sits inside the flat band (koth_margin 1% of 0.80).
        champ = _entry("5A" + "a" * 44, 0.80).model_copy(update={"bench_version": 8})
        chall = _entry(
            "5B" + "b" * 44, 0.795, first_seen=_T0 + timedelta(minutes=1)
        ).model_copy(update={"bench_version": 8})

        async def _score(**kw: Any) -> ScoreReport:
            return self._seeded_report(kw["seed"], 0.8)

        dittobench = MagicMock()
        dittobench.score_tarball = AsyncMock(side_effect=_score)
        dittobench.last_details = {}
        platform = _platform_with_ledger(jobs=[], ledger=[])
        platform.get_artifact = AsyncMock(
            return_value=ArtifactResponse(
                agent_id=champ.agent_id,
                sha256="ab" * 32,
                download_url="https://signed.example/x.tar.gz",
                expires_at=datetime.now(UTC),
                bench_version=8,
                screening_policy_version=9,
                screened_image_url="https://signed.example/image.tar",
                screened_image_sha256="12" * 32,
                screened_image_size_bytes=123,
                screened_image_id="sha256:" + "34" * 32,
                screened_image_ref=(
                    "ditto-screen/550e8400-e29b-41d4-a716-446655440000:latest"
                ),
            )
        )
        w = self._worker(dittobench, platform)

        await w._confirm_contested_dethrone(
            LedgerResponse(entries=[champ, chall], active_bench_version=8, count=2)
        )

        # Both agents run all three common seeds; one signed score each.
        assert dittobench.score_tarball.await_count == 6
        assert platform.submit_score.await_count == 2
        reports = [c.kwargs["report"] for c in platform.submit_score.await_args_list]
        assert reports[0].confirmation_seeds == reports[1].confirmation_seeds

        # Seeds are anchored to the CHAMPION's agent id alone, not the cohort,
        # so they are stable across sweeps as challengers come and go.
        from ditto.validator.crn import confirmation_seeds

        expected = confirmation_seeds([str(champ.agent_id)], version=8, count=3)
        # The report seed-sorts its pairs; compare as sets.
        assert set(reports[0].confirmation_seeds) == set(expected)

    async def test_settled_champion_not_rescored_for_fresh_entrant(self) -> None:
        # The champion already carries its anchored seeds; a fresh in-band
        # entrant must be confirmed WITHOUT re-running the champion (the
        # O(1)-per-entrant guard against confirmation amplification).
        from ditto.validator.crn import confirmation_seeds

        champ_id = _entry("5A" + "a" * 44, 0.80).agent_id
        seeds = confirmation_seeds([str(champ_id)], version=8, count=3)
        champ = _entry("5A" + "a" * 44, 0.80, agent_id=champ_id).model_copy(
            update={
                "bench_version": 8,
                "confirmation_seeds": seeds,
                "confirmation_composites": [0.80, 0.80, 0.80],
            }
        )
        entrant = _entry(
            "5C" + "c" * 44, 0.795, first_seen=_T0 + timedelta(minutes=2)
        ).model_copy(update={"bench_version": 8})

        async def _score(**kw: Any) -> ScoreReport:
            return self._seeded_report(kw["seed"], 0.79)

        dittobench = MagicMock()
        dittobench.score_tarball = AsyncMock(side_effect=_score)
        dittobench.last_details = {}
        platform = _platform_with_ledger(jobs=[], ledger=[])
        platform.get_artifact = AsyncMock(
            return_value=ArtifactResponse(
                agent_id=entrant.agent_id,
                sha256="ab" * 32,
                download_url="https://signed.example/x.tar.gz",
                expires_at=datetime.now(UTC),
                bench_version=8,
                screening_policy_version=9,
                screened_image_url="https://signed.example/image.tar",
                screened_image_sha256="12" * 32,
                screened_image_size_bytes=123,
                screened_image_id="sha256:" + "34" * 32,
                screened_image_ref=(
                    "ditto-screen/550e8400-e29b-41d4-a716-446655440000:latest"
                ),
            )
        )
        w = self._worker(dittobench, platform)

        await w._confirm_contested_dethrone(
            LedgerResponse(entries=[champ, entrant], active_bench_version=8, count=2)
        )

        # Only the entrant runs (3 seeds, 1 submission); the champion is untouched.
        assert dittobench.score_tarball.await_count == 3
        assert platform.submit_score.await_count == 1
        submitted_agent = platform.submit_score.await_args.args[0]
        assert submitted_agent == entrant.agent_id

    async def test_settled_contest_runs_nothing(self) -> None:
        # Medians 0.80 vs 0.795: still inside the band, but the pair already
        # shares three confirmation seeds, so the paired statistic decides it.
        shared = {
            "confirmation_composites": [0.79, 0.80, 0.81],
            "confirmation_seeds": [7, 8, 9],
        }
        champ = _entry("5A" + "a" * 44, 0.80).model_copy(update=shared)
        chall = _entry(
            "5B" + "b" * 44, 0.795, first_seen=_T0 + timedelta(minutes=1)
        ).model_copy(update={**shared, "confirmation_composites": [0.78, 0.795, 0.80]})

        dittobench = MagicMock()
        dittobench.score_tarball = AsyncMock()
        platform = _platform_with_ledger(jobs=[], ledger=[])
        w = self._worker(dittobench, platform)

        await w._confirm_contested_dethrone(
            LedgerResponse(entries=[champ, chall], active_bench_version=8, count=2)
        )

        dittobench.score_tarball.assert_not_awaited()
        platform.submit_score.assert_not_awaited()

    async def test_clear_lead_runs_nothing(self) -> None:
        champ = _entry("5A" + "a" * 44, 0.80)
        laggard = _entry("5B" + "b" * 44, 0.60, first_seen=_T0 + timedelta(minutes=1))

        dittobench = MagicMock()
        dittobench.score_tarball = AsyncMock()
        platform = _platform_with_ledger(jobs=[], ledger=[])
        w = self._worker(dittobench, platform)

        await w._confirm_contested_dethrone(
            LedgerResponse(entries=[champ, laggard], active_bench_version=8, count=2)
        )

        dittobench.score_tarball.assert_not_awaited()
        platform.submit_score.assert_not_awaited()


class TestSlotIsClaimedBeforeInferenceActivation:
    """A claimed slot must announce itself before the inference hand-off.

    Exchanging the grant and standing up the broker session is unbounded work,
    and with several slots contending it can hold a slot for minutes. Until the
    slot published something, the platform had a live lease with no progress
    against it: the fleet view rendered "Benchmark progress not reported" and
    the lease-liveness gate could read the same silence as an idle slot.
    """

    async def test_preparing_is_published_before_inference_activation(self) -> None:
        job = _job("5MinerA" + "x" * 41)
        platform = _platform_with_ledger(jobs=[], ledger=[])
        seen_before_activation: list[ActiveBenchmarkSlot] = []

        async def activate(_job: JobResponse) -> None:
            for call in platform.submit_heartbeat.await_args_list:
                capacity = call.args[0].benchmark_capacity
                if capacity is not None:
                    seen_before_activation.extend(capacity.active)
            return None

        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=MagicMock(
                score_tarball=AsyncMock(return_value=_report("run", 0.9))
            ),
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )
        worker._activate_ticket_inference = AsyncMock(  # type: ignore[method-assign]
            side_effect=activate
        )

        await worker._score_job(job)

        assert seen_before_activation, (
            "the slot published nothing before inference activation"
        )
        first = seen_before_activation[0]
        assert first.slot_id == "slot-0"
        assert first.agent_id == job.agent_id
        assert first.progress is not None
        assert first.progress.stage == "preparing"
        assert first.progress.ticket_deadline == job.deadline

    async def test_a_claimed_slot_is_reported_before_it_has_any_progress(
        self,
    ) -> None:
        """Protocol 16: occupancy is reported even with nothing to say yet.

        Through v15 the snapshot dropped any slot whose ``progress`` was None,
        so a slot that was claimed but still seeding was indistinguishable from
        a free one. The platform derives every lease-revocation safeguard from
        this list, so the omission is what let a live run's lease be revoked.
        """
        worker = ValidatorWorker(
            config=_config(),
            platform=_platform_with_ledger(jobs=[], ledger=[]),
            dittobench=MagicMock(),
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )
        agent_id = uuid4()
        slot = worker._slots["slot-0"]
        slot.active_agent_id = agent_id
        slot.bench_version = 9
        slot.progress = None

        active = worker._capacity_snapshot().active

        assert [entry.slot_id for entry in active] == ["slot-0"]
        assert active[0].agent_id == agent_id
        assert active[0].progress is None

    async def test_an_unclaimed_slot_is_still_omitted(self) -> None:
        """The claim, not the progress, is what makes a slot occupied."""
        worker = ValidatorWorker(
            config=_config(),
            platform=_platform_with_ledger(jobs=[], ledger=[]),
            dittobench=MagicMock(),
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )

        assert worker._capacity_snapshot().active == []

    async def test_failed_activation_releases_the_claimed_slot(self) -> None:
        job = _job("5MinerA" + "x" * 41)
        platform = _platform_with_ledger(jobs=[], ledger=[])
        worker = ValidatorWorker(
            config=_config(),
            platform=platform,
            dittobench=MagicMock(),
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )
        worker._activate_ticket_inference = AsyncMock(  # type: ignore[method-assign]
            side_effect=PlatformError("inference exchange escaped ticket bounds")
        )

        with pytest.raises(PlatformError):
            await worker._score_job(job)

        # Nothing downstream clears a slot claimed this early, so the failure
        # path must, or the slot stays advertised as busy forever.
        assert worker._capacity_snapshot().active == []
        assert worker._active_agent_id is None


class TestResourceConstrainedAdmission:
    """A host past its own ceilings declines work -- visibly, not silently.

    The failure mode this guards against is the one that already cost a day on
    this subnet (#274 / platform #496): a validator that simply goes quiet is
    indistinguishable from one that is free. So a constrained worker keeps
    heartbeating, keeps advertising how many slots it has, and says *why* it is
    idle -- it just offers none of them.
    """

    @staticmethod
    def _collector(*, cpu: int = 0, memory: int = 0, disk: int = 0) -> MagicMock:
        collector = MagicMock()
        collector.collect = MagicMock(
            return_value=SystemMetrics(
                collected_at=int(datetime.now(UTC).timestamp()),
                cpu_percent=cpu,
                memory_percent=memory,
                disk_percent=disk,
                docker=DockerHealth(
                    status="healthy", running_containers=1, unhealthy_containers=0
                ),
            )
        )
        return collector

    def _worker(
        self, *, collector: MagicMock, ceilings: ResourceCeilings | None = None
    ) -> tuple[ValidatorWorker, MagicMock]:
        platform = _platform_with_ledger(jobs=[], ledger=[])
        platform.request_job = AsyncMock(return_value=None)
        config = _config()
        config.benchmark_capacity = 4
        config.resource_ceilings = ceilings or DEFAULT_RESOURCE_CEILINGS
        dittobench = MagicMock()
        dittobench.full_run_capacity = 4
        dittobench.preflight = AsyncMock()
        worker = ValidatorWorker(
            config=config,
            platform=platform,
            dittobench=dittobench,
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
            system_metrics=collector,
        )
        return worker, platform

    async def test_a_nearly_full_disk_claims_nothing_but_still_heartbeats(
        self,
    ) -> None:
        worker, platform = self._worker(collector=self._collector(disk=95))

        outcome = await worker.run_once(set_weights=False)

        assert outcome.queue_depth == 0
        platform.request_job.assert_not_awaited()
        platform.request_top5_confirmation_job.assert_not_awaited()
        # Still visible, and the reason is on the wire.
        assert platform.submit_heartbeat.await_count >= 1
        final = platform.submit_heartbeat.await_args_list[-1].args[0]
        assert final.benchmark_capacity is not None
        assert final.benchmark_capacity.admission == "resource_constrained"
        assert final.benchmark_capacity.healthy_slots == []
        # The host still says how big it is, so recovery needs no rediscovery.
        assert final.benchmark_capacity.configured_slots == 4
        # And the metrics that justify the decline ride the same heartbeat.
        assert final.system_metrics is not None
        assert final.system_metrics.disk_percent == 95

    async def test_nearly_exhausted_memory_declines_the_same_way(self) -> None:
        worker, platform = self._worker(collector=self._collector(memory=95))

        await worker.run_once(set_weights=False)

        platform.request_job.assert_not_awaited()
        final = platform.submit_heartbeat.await_args_list[-1].args[0]
        assert final.benchmark_capacity is not None
        assert final.benchmark_capacity.admission == "resource_constrained"

    async def test_a_busy_healthy_host_is_completely_unaffected(self) -> None:
        """65% memory and a pinned CPU is a working benchmark host."""
        worker, platform = self._worker(
            collector=self._collector(cpu=100, memory=65, disk=45)
        )

        await worker.run_once(set_weights=False)

        assert platform.request_job.await_count == 4
        final = platform.submit_heartbeat.await_args_list[-1].args[0]
        assert final.benchmark_capacity is not None
        assert final.benchmark_capacity.admission == "accepting"
        assert final.benchmark_capacity.healthy_slots == [
            f"slot-{index}" for index in range(4)
        ]

    async def test_the_ceilings_are_operator_configurable(self) -> None:
        """The same reading declines or not, purely on the configured ceiling."""
        worker, platform = self._worker(
            collector=self._collector(disk=80),
            ceilings=ResourceCeilings(disk_percent=80),
        )

        await worker.run_once(set_weights=False)

        platform.request_job.assert_not_awaited()
        final = platform.submit_heartbeat.await_args_list[-1].args[0]
        assert final.benchmark_capacity is not None
        assert final.benchmark_capacity.admission == "resource_constrained"

    async def test_recovery_needs_no_restart(self) -> None:
        collector = self._collector(disk=95)
        worker, platform = self._worker(collector=collector)

        await worker.run_once(set_weights=False)
        platform.request_job.assert_not_awaited()

        collector.collect.return_value = SystemMetrics(
            collected_at=int(datetime.now(UTC).timestamp()),
            cpu_percent=0,
            memory_percent=20,
            disk_percent=45,
            docker=DockerHealth(
                status="healthy", running_containers=1, unhealthy_containers=0
            ),
        )
        await worker.run_once(set_weights=False)

        assert platform.request_job.await_count == 4
        final = platform.submit_heartbeat.await_args_list[-1].args[0]
        assert final.benchmark_capacity is not None
        assert final.benchmark_capacity.admission == "accepting"

    async def test_a_worker_with_no_collector_behaves_as_before(self) -> None:
        """Unobserved is not constrained; the gate opens only on evidence."""
        platform = _platform_with_ledger(jobs=[], ledger=[])
        platform.request_job = AsyncMock(return_value=None)
        config = _config()
        config.benchmark_capacity = 2
        dittobench = MagicMock()
        dittobench.full_run_capacity = 2
        dittobench.preflight = AsyncMock()
        worker = ValidatorWorker(
            config=config,
            platform=platform,
            dittobench=dittobench,
            chain=MagicMock(),
            keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
        )

        await worker.run_once(set_weights=False)

        assert platform.request_job.await_count == 2
        final = platform.submit_heartbeat.await_args_list[-1].args[0]
        assert final.benchmark_capacity is not None
        assert final.benchmark_capacity.admission == "accepting"

    async def test_a_collector_failure_never_stops_the_validator(self) -> None:
        """psutil hiccupping must not be read as a full disk."""
        collector = MagicMock()
        collector.collect = MagicMock(side_effect=OSError("/proc unavailable"))
        worker, platform = self._worker(collector=collector)

        await worker.run_once(set_weights=False)

        assert platform.request_job.await_count == 4
        final = platform.submit_heartbeat.await_args_list[-1].args[0]
        assert final.benchmark_capacity is not None
        assert final.benchmark_capacity.admission == "accepting"

    async def test_a_drain_outranks_a_resource_decline(self) -> None:
        """Both stop work; the deliberate one must be what the fleet view says."""
        worker, platform = self._worker(collector=self._collector(disk=100))
        drain = asyncio.Event()
        drain.set()

        await worker.run_once(set_weights=False, drain_requested=drain)

        final = platform.submit_heartbeat.await_args_list[-1].args[0]
        assert final.benchmark_capacity is not None
        assert final.benchmark_capacity.admission == "draining"

    async def test_a_live_lease_is_still_reported_while_constrained(self) -> None:
        """Withholding new work must never look like abandoning current work."""
        worker, _ = self._worker(collector=self._collector(disk=95))
        worker._admission = "resource_constrained"
        worker._slots["slot-1"].active_agent_id = uuid4()
        worker._slots["slot-1"].bench_version = 9

        capacity = worker._capacity_snapshot()

        assert capacity.admission == "resource_constrained"
        assert capacity.healthy_slots == []
        assert [slot.slot_id for slot in capacity.active] == ["slot-1"]
