"""Protocol 28: a held incumbent keeps the crown and its share burns.

The Platform's terminal-review emission gate withholds an artifact whose own
source review is unresolved. When that artifact is the crown incumbent, the
epoch pin serves it as ``provisional_incumbent`` instead of dropping it: the
fold must crown it exactly as if it were payable, then burn every share it
holds instead of renormalizing that share onto the miners who are paid.
Without the field every fold here must be byte-identical to protocol 27.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from ditto.api_models.router_ledger import RouterLedgerResponse
from ditto.api_models.validator import LedgerEntry, LedgerResponse
from ditto.tests.validator.test_track_blend import _router
from ditto.tests.validator.test_worker import _BURN_HOTKEY, _T0, _config, _entry
from ditto.validator.tracks import MemoryFoldParams, TrackFoldInputs, memory_fold
from ditto.validator.weights import (
    UNPAID_SHARE_KEY,
    apply_miner_emission_cap,
    blend_track_weights,
    compute_weights,
    select_champion,
    split_unpaid_share,
    track_allocated_share,
)
from ditto.validator.worker import ValidatorWorker

# Production consensus constants, so the fold here is the fleet's fold.
_KOTH: dict[str, Any] = {
    "margin": 0.007,
    "tail_size": 4,
    "rank_shares": (0.65, 0.14, 0.10, 0.07, 0.04),
    "dethrone_z": 1.64,
}
_GOLDEN = (
    Path(__file__).resolve().parents[1] / "fixtures/provisional_incumbent_pin.json"
)
"""The pinned ledger the Platform serves for a held champion and an eligible
runner-up. ``apps/platform/.../test_emission_eligibility_ledger.py`` asserts it
serves exactly these bytes from a real materialize -> pin -> ``GET
/scoring/scores``, and records the miner shares its own projection of the pin
prescribes (``pin_expected_shares``)."""


def _held_and_runner_up() -> tuple[LedgerEntry, LedgerEntry]:
    held = _entry("5Held" + "x" * 43, 0.95, first_seen=_T0, bench_version=12)
    runner_up = _entry(
        "5Runner" + "x" * 41,
        0.80,
        first_seen=_T0 + timedelta(days=1),
        bench_version=12,
    )
    return held, runner_up


def _ledger(
    entries: list[LedgerEntry],
    provisional: LedgerEntry | None,
    **overrides: Any,
) -> LedgerResponse:
    values: dict[str, Any] = {
        "entries": entries,
        "count": len(entries),
        "crown_mode": "incumbent",
        "crown_incumbent_agent_id": (
            provisional.agent_id if provisional is not None else None
        ),
        "provisional_incumbent": provisional,
        "reward_eligibility_mode": "enforce",
    }
    values.update(overrides)
    return LedgerResponse(**values)


def _worker(ledger: LedgerResponse, config: Any | None = None) -> ValidatorWorker:
    cfg = config or _config()
    cfg.koth_margin = _KOTH["margin"]
    platform = MagicMock()
    platform.get_ledger = AsyncMock(return_value=ledger)
    worker = ValidatorWorker(
        config=cfg,
        platform=platform,
        dittobench=MagicMock(),
        chain=MagicMock(),
        keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
    )
    worker._registered_ledger_entries = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda entries: list(entries)
    )
    worker._validator_permitted = AsyncMock(return_value=True)  # type: ignore[method-assign]
    worker._stake_sufficient = AsyncMock(return_value=True)  # type: ignore[method-assign]
    worker._log_commit_reveal_mode = AsyncMock()  # type: ignore[method-assign]
    worker._put_weights_with_retry = AsyncMock(return_value=True)  # type: ignore[method-assign]
    return worker


async def _fold(ledger: LedgerResponse, config: Any | None = None) -> Any:
    outcome = await _worker(ledger, config)._update_weights()
    assert outcome.submitted
    return outcome


def _legacy_vector(ledger: LedgerResponse, config: Any) -> dict[str, float]:
    """The protocol-27 worker composition, restated: fold, blend, then cap
    ``miner_share * allocated``. Single memory track at the shipped split."""
    vectors = {
        "memory": compute_weights(
            ledger.entries,
            margin=config.koth_margin,
            tail_size=config.koth_tail_size,
            rank_shares=config.koth_rank_shares,
            dethrone_z=config.koth_dethrone_z,
            tie_pooling=ledger.tie_weighting_mode == "pool",
            ceiling_band_clamp=ledger.dethrone_band_mode == "headroom_capped",
            incumbent_agent_id=(
                ledger.crown_incumbent_agent_id
                if ledger.crown_mode == "incumbent"
                else None
            ),
        )
    }
    shares = {"memory": 10_000}
    return apply_miner_emission_cap(
        blend_track_weights(vectors, shares),
        miner_share=(1.0 - ledger.burn_share) * track_allocated_share(shares, vectors),
        burn_hotkey=_BURN_HOTKEY,
    )


class TestPlatformServedPin:
    """Peyton's P1 regression, through the real worker, on the Platform's bytes."""

    async def test_held_champion_keeps_the_crown_and_its_slot_burns(self) -> None:
        golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))
        ledger = LedgerResponse.model_validate(golden["ledger"])
        held = ledger.provisional_incumbent
        assert held is not None
        (runner_up,) = ledger.entries
        expected = golden["expected_miner_shares"]

        outcome = await _fold(ledger)

        # The runner-up is paid exactly the share the Platform's pin projection
        # prescribes -- its tail share, not the crown -- and the held champion
        # gets nothing: the rest of the miner pool burns.
        assert set(outcome.weights) == {runner_up.miner_hotkey, _BURN_HOTKEY}
        assert held.miner_hotkey not in outcome.weights
        assert outcome.weights[runner_up.miner_hotkey] == pytest.approx(
            expected[runner_up.miner_hotkey], abs=1e-12
        )
        assert outcome.weights[runner_up.miner_hotkey] == pytest.approx(0.14 / 0.79)
        assert outcome.weights[_BURN_HOTKEY] == pytest.approx(0.65 / 0.79)
        # The crown is the held champion's, as the pin recorded it.
        assert outcome.fold.champion_agent_id == held.agent_id
        assert str(held.agent_id) == golden["champion_agent_id"]

    async def test_policy_burn_scales_the_paid_share_and_adds_the_unpaid_one(
        self,
    ) -> None:
        golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))
        ledger = LedgerResponse.model_validate({**golden["ledger"], "burn_share": 0.2})
        (runner_up,) = ledger.entries
        outcome = await _fold(ledger)
        paid = 0.8 * 0.14 / 0.79
        assert outcome.weights[runner_up.miner_hotkey] == pytest.approx(paid)
        assert outcome.weights[_BURN_HOTKEY] == pytest.approx(1.0 - paid)

    async def test_a_protocol_27_reading_of_the_same_pin_would_pay_the_runner_up(
        self,
    ) -> None:
        """Why the fleet gate exists: without the field the runner-up inherits
        the crown and the whole miner pool."""
        golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))
        ledger = LedgerResponse.model_validate(
            {k: v for k, v in golden["ledger"].items() if k != "provisional_incumbent"}
        )
        (runner_up,) = ledger.entries
        outcome = await _fold(ledger)
        assert outcome.weights == {runner_up.miner_hotkey: pytest.approx(1.0)}


class TestFold:
    def test_held_champion_keeps_the_crown_and_its_share_is_unpaid(self) -> None:
        held, runner_up = _held_and_runner_up()
        pool = [runner_up, held]
        assert (
            select_champion(pool, **_without_shares(), incumbent_agent_id=held.agent_id)
            == held
        )
        weights = compute_weights(
            pool,
            **_KOTH,
            incumbent_agent_id=held.agent_id,
            unpaid_agent_id=held.agent_id,
        )
        assert weights == {UNPAID_SHARE_KEY: 0.65, runner_up.miner_hotkey: 0.14}

    def test_a_challenger_that_clears_the_band_is_crowned_and_paid(self) -> None:
        held = _entry("5Held" + "x" * 43, 0.80, first_seen=_T0, bench_version=12)
        challenger = _entry(
            "5Chall" + "x" * 42,
            0.95,
            first_seen=_T0 + timedelta(days=2),
            bench_version=12,
        )
        pool = [challenger, held]
        assert (
            select_champion(pool, **_without_shares(), incumbent_agent_id=held.agent_id)
            == challenger
        )
        weights = compute_weights(
            pool,
            **_KOTH,
            incumbent_agent_id=held.agent_id,
            unpaid_agent_id=held.agent_id,
        )
        # The provisional incumbent lands in the tail, and that slot is unpaid.
        assert weights == {challenger.miner_hotkey: 0.65, UNPAID_SHARE_KEY: 0.14}

    def test_tie_pooling_counts_the_provisional_incumbent_as_a_member(self) -> None:
        held = _entry("5Held" + "x" * 43, 0.90, first_seen=_T0, bench_version=12)
        tied = _entry(
            "5Tied" + "x" * 43,
            0.90,
            first_seen=_T0 + timedelta(days=1),
            bench_version=12,
        )
        third = _entry(
            "5Third" + "x" * 42,
            0.70,
            first_seen=_T0 + timedelta(days=2),
            bench_version=12,
        )
        pool = [tied, held, third]
        paid = compute_weights(
            pool, **_KOTH, tie_pooling=True, incumbent_agent_id=held.agent_id
        )
        weights = compute_weights(
            pool,
            **_KOTH,
            tie_pooling=True,
            incumbent_agent_id=held.agent_id,
            unpaid_agent_id=held.agent_id,
        )
        # Pooled exactly as if it were paid; only its slot's destination moves.
        assert paid[held.miner_hotkey] == pytest.approx(0.395)
        assert weights == {
            UNPAID_SHARE_KEY: paid[held.miner_hotkey],
            tied.miner_hotkey: paid[tied.miner_hotkey],
            third.miner_hotkey: paid[third.miner_hotkey],
        }

    def test_score_ceiling_cohort_counts_the_provisional_incumbent(self) -> None:
        held = _entry("5Held" + "x" * 43, 0.997012, first_seen=_T0)
        others = [
            _entry(
                f"tied-{index}", 0.997012, first_seen=_T0 + timedelta(minutes=index + 1)
            )
            for index in range(5)
        ]
        pool = [*others, held]
        weights = compute_weights(
            pool,
            **_KOTH,
            tie_pooling=True,
            incumbent_agent_id=held.agent_id,
            unpaid_agent_id=held.agent_id,
        )
        # Six members is beyond the five ranked slots, so this is the uncapped
        # joint crown: one sixth each, and the held member's sixth is unpaid.
        assert len(weights) == 6
        assert weights == pytest.approx(
            {UNPAID_SHARE_KEY: 1 / 6, **{f"tied-{index}": 1 / 6 for index in range(5)}}
        )

    def test_the_memory_track_threads_the_unpaid_agent(self) -> None:
        held, runner_up = _held_and_runner_up()
        params = MemoryFoldParams(
            margin=_KOTH["margin"],
            tail_size=_KOTH["tail_size"],
            rank_shares=_KOTH["rank_shares"],
            dethrone_z=_KOTH["dethrone_z"],
            incumbent_agent_id=held.agent_id,
            unpaid_agent_id=held.agent_id,
        )
        vector = memory_fold(
            TrackFoldInputs(memory_entries=[runner_up, held], memory_params=params)
        )
        assert vector == {UNPAID_SHARE_KEY: 0.65, runner_up.miner_hotkey: 0.14}


def _without_shares() -> dict[str, Any]:
    return {"margin": _KOTH["margin"], "dethrone_z": _KOTH["dethrone_z"]}


class TestSplitUnpaidShare:
    def test_without_the_key_the_vector_and_share_are_untouched(self) -> None:
        vector = {"a": 0.65, "b": 0.14}
        paid, fraction = split_unpaid_share(vector)
        assert paid == vector
        assert fraction == 1.0

    def test_the_unpaid_share_becomes_a_payable_fraction(self) -> None:
        paid, fraction = split_unpaid_share({UNPAID_SHARE_KEY: 0.65, "b": 0.14})
        assert paid == {"b": 0.14}
        assert fraction == pytest.approx(0.14 / 0.79)

    def test_an_unpaid_only_vector_pays_nobody(self) -> None:
        assert split_unpaid_share({UNPAID_SHARE_KEY: 0.65}) == ({}, 0.0)


class TestWorkerComposition:
    async def test_only_the_held_champion_burns_the_whole_miner_pool(self) -> None:
        held, _ = _held_and_runner_up()
        outcome = await _fold(_ledger([], held))
        assert outcome.weights == {_BURN_HOTKEY: 1.0}
        assert outcome.fold.champion_agent_id == held.agent_id

    async def test_a_second_track_is_not_topped_up_by_the_unpaid_crown(self) -> None:
        held, runner_up = _held_and_runner_up()
        cfg = _config()
        cfg.track_shares_bps = {"memory": 8000, "coding": 0, "router": 2000}
        cfg.router_track_state = "active"
        cfg.router_weight_eligible = True
        router = RouterLedgerResponse(entries=[_router("5Router" + "x" * 41, 0.9)])

        async def fold(ledger: LedgerResponse) -> dict[str, float]:
            worker = _worker(ledger, cfg)
            worker._get_router_ledger = AsyncMock(return_value=router)  # type: ignore[method-assign]
            outcome = await worker._update_weights()
            return outcome.weights

        # The same pool with the held champion paid, as a reference.
        reference = await fold(
            _ledger([runner_up, held], None, crown_incumbent_agent_id=held.agent_id)
        )
        weights = await fold(_ledger([runner_up], held))
        router_hotkey = "5Router" + "x" * 41
        assert weights[runner_up.miner_hotkey] == pytest.approx(
            reference[runner_up.miner_hotkey]
        )
        assert weights[router_hotkey] == pytest.approx(reference[router_hotkey])
        assert held.miner_hotkey not in weights
        assert weights[_BURN_HOTKEY] == pytest.approx(reference[held.miner_hotkey])
        assert weights[_BURN_HOTKEY] == pytest.approx(0.8 * 0.65 / 0.79)

    async def test_a_deregistered_held_champion_falls_back_to_the_classic_walk(
        self,
    ) -> None:
        held, runner_up = _held_and_runner_up()
        worker = _worker(_ledger([runner_up], held))
        worker._registered_ledger_entries = AsyncMock(  # type: ignore[method-assign]
            side_effect=lambda entries: [e for e in entries if e != held]
        )
        outcome = await worker._update_weights()
        # Exactly what protocol 27 does when a payable incumbent deregisters.
        assert outcome.weights == {runner_up.miner_hotkey: pytest.approx(1.0)}


class TestByteIdentityWithoutAProvisionalIncumbent:
    """Every ledger that does not serve a provisional incumbent -- or serves
    one the fold must not honour -- folds exactly as protocol 27 did."""

    @staticmethod
    def _representative() -> list[tuple[str, LedgerResponse]]:
        def entries(*scores: float) -> list[LedgerEntry]:
            return [
                _entry(
                    f"5M{index}" + "x" * 44,
                    score,
                    first_seen=_T0 + timedelta(minutes=index),
                    bench_version=12,
                )
                for index, score in enumerate(scores)
            ]

        five = entries(0.90, 0.88, 0.87, 0.80, 0.70, 0.60)
        tied = entries(0.90, 0.90, 0.85)
        ceiling = entries(0.996348, *([0.997012] * 5))
        flap = entries(0.7981, 0.8010)
        return [
            ("ranked", LedgerResponse(entries=five, count=6)),
            ("burn", LedgerResponse(entries=five, count=6, burn_share=0.3)),
            (
                "pooled",
                LedgerResponse(entries=tied, count=3, tie_weighting_mode="pool"),
            ),
            (
                "ceiling",
                LedgerResponse(entries=ceiling, count=6, tie_weighting_mode="pool"),
            ),
            (
                "incumbent",
                LedgerResponse(
                    entries=flap,
                    count=2,
                    crown_mode="incumbent",
                    crown_incumbent_agent_id=flap[1].agent_id,
                    dethrone_band_mode="headroom_capped",
                    reward_eligibility_mode="enforce",
                ),
            ),
        ]

    async def test_worker_vectors_equal_the_protocol_27_composition(self) -> None:
        for name, ledger in self._representative():
            cfg = _config()
            cfg.koth_margin = _KOTH["margin"]
            outcome = await _fold(ledger, cfg)
            assert outcome.weights == _legacy_vector(ledger, cfg), name
            assert UNPAID_SHARE_KEY not in outcome.weights

    @pytest.mark.parametrize(
        "mismatch",
        ["no-crown-mode", "other-incumbent", "also-payable"],
    )
    async def test_an_unhonourable_provisional_incumbent_is_ignored(
        self, mismatch: str
    ) -> None:
        held, runner_up = _held_and_runner_up()
        entries = [runner_up]
        overrides: dict[str, Any] = {}
        if mismatch == "no-crown-mode":
            overrides = {"crown_mode": None, "crown_incumbent_agent_id": None}
        elif mismatch == "other-incumbent":
            overrides = {"crown_incumbent_agent_id": uuid4()}
        else:
            entries = [runner_up, held]
        ledger = _ledger(entries, held, **overrides)
        stripped = ledger.model_copy(update={"provisional_incumbent": None})
        assert (await _fold(ledger)).weights == (await _fold(stripped)).weights

    def test_the_wire_omits_the_field_when_absent(self) -> None:
        held, runner_up = _held_and_runner_up()
        dumped = LedgerResponse(entries=[runner_up], count=1).model_dump(mode="json")
        assert "provisional_incumbent" not in dumped
        served = _ledger([runner_up], held).model_dump(mode="json")
        assert served["provisional_incumbent"]["agent_id"] == str(held.agent_id)
