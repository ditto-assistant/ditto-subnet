"""Protocol 36 (#2911): an unattested paid slot pays only part of its share.

Platform serves ``unpaid_fractions`` (``agent_id -> f``) for ledger entries
whose agent took a paid slot without passing operator board review inside its
grace window. The fold picks champion, tail, tie pools and the score-ceiling
cohort exactly as without it; a named recipient then pays ``share * (1 - f)``
and the rest burns, after the service bucket, like the protocol-28 provisional
incumbent's whole slot. An empty map must fold byte-identically to protocol 35.
"""

from __future__ import annotations

import math
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest

from ditto.api_models.validator import LedgerEntry, LedgerResponse
from ditto.tests.validator.test_provisional_incumbent import (
    _KOTH,
    _held_and_runner_up,
    _ledger,
)
from ditto.tests.validator.test_provisional_incumbent import _fold as _fold_worker
from ditto.tests.validator.test_worker import _BURN_HOTKEY, _T0, _config, _entry
from ditto.treasury.service_allocation import service_first_weights
from ditto.validator.tracks import MemoryFoldParams, TrackFoldInputs, memory_fold
from ditto.validator.weights import (
    UNPAID_SHARE_KEY,
    compute_weights,
    owner_burn_destination_required,
    select_champion,
    split_unpaid_share,
)
from ditto.validator.worker import _ledger_unpaid_fractions

_SHARES = _KOTH["rank_shares"]


def _five() -> list[LedgerEntry]:
    """Five recipients and one tail-less sixth, distinct hotkeys, one version."""
    return [
        _entry(
            f"5R{index}" + "x" * 44,
            score,
            first_seen=_T0 + timedelta(minutes=index),
            bench_version=12,
        )
        for index, score in enumerate((0.90, 0.85, 0.80, 0.75, 0.70, 0.60))
    ]


def _fold(entries: list[LedgerEntry], **kwargs: Any) -> dict[str, float]:
    return compute_weights(entries, **_KOTH, **kwargs)


class TestFold:
    def test_an_empty_map_is_byte_identical(self) -> None:
        entries = _five()
        assert _fold(entries, unpaid_fractions={}) == _fold(entries)
        assert _fold(entries, unpaid_fractions=None) == _fold(entries)

    def test_one_unattested_recipient_burns_its_fraction(self) -> None:
        entries = _five()
        paid = _fold(entries)
        third = entries[2]
        weights = _fold(entries, unpaid_fractions={third.agent_id: 0.5})
        assert weights[third.miner_hotkey] == pytest.approx(_SHARES[2] * 0.5)
        assert weights[UNPAID_SHARE_KEY] == pytest.approx(_SHARES[2] * 0.5)
        for entry in (entries[0], entries[1], entries[3], entries[4]):
            # Nobody else's share moves: the unpaid part is never renormalized.
            assert weights[entry.miner_hotkey] == paid[entry.miner_hotkey]
        assert math.fsum(weights.values()) == pytest.approx(1.0)

    def test_all_five_unattested(self) -> None:
        entries = _five()
        fractions = {entry.agent_id: 0.5 for entry in entries}
        weights = _fold(entries, unpaid_fractions=fractions)
        for index, entry in enumerate(entries[:5]):
            assert weights[entry.miner_hotkey] == pytest.approx(_SHARES[index] * 0.5)
        assert weights[UNPAID_SHARE_KEY] == pytest.approx(0.5)
        payable, paid_fraction = split_unpaid_share(weights)
        assert UNPAID_SHARE_KEY not in payable
        assert paid_fraction == pytest.approx(0.5)

    def test_a_full_fraction_is_the_provisional_treatment(self) -> None:
        entries = _five()
        champion = entries[0]
        by_fraction = _fold(entries, unpaid_fractions={champion.agent_id: 1.0})
        by_agent = _fold(entries, unpaid_agent_id=champion.agent_id)
        assert by_fraction == by_agent
        assert champion.miner_hotkey not in by_fraction

    def test_an_unpaid_agent_and_fractions_together(self) -> None:
        held, runner_up = _held_and_runner_up()
        pool = [runner_up, held]
        weights = _fold(
            pool,
            incumbent_agent_id=held.agent_id,
            unpaid_agent_id=held.agent_id,
            # f = 1 wins for the provisional incumbent; the runner-up's half
            # still burns beside it.
            unpaid_fractions={held.agent_id: 0.25, runner_up.agent_id: 0.5},
        )
        assert held.miner_hotkey not in weights
        assert weights[runner_up.miner_hotkey] == pytest.approx(_SHARES[1] * 0.5)
        assert weights[UNPAID_SHARE_KEY] == pytest.approx(_SHARES[0] + _SHARES[1] * 0.5)

    def test_a_named_entry_outside_the_paid_slots_changes_nothing(self) -> None:
        entries = _five()
        sixth = entries[5]
        assert _fold(entries, unpaid_fractions={sixth.agent_id: 0.5}) == _fold(entries)

    @pytest.mark.parametrize("tie_pooling", [False, True])
    def test_crown_and_recipients_are_unchanged(self, tie_pooling: bool) -> None:
        entries = _five()
        fractions = {entries[0].agent_id: 0.5, entries[3].agent_id: 1.0}
        plain = _fold(entries, tie_pooling=tie_pooling)
        burned = _fold(entries, tie_pooling=tie_pooling, unpaid_fractions=fractions)
        # The same slots, the same shares, only split between hotkey and burn.
        paid_hotkeys = set(plain)
        assert {h for h in burned if h != UNPAID_SHARE_KEY} == paid_hotkeys - {
            entries[3].miner_hotkey
        }
        assert math.fsum(burned.values()) == pytest.approx(math.fsum(plain.values()))
        champion = select_champion(
            entries,
            margin=_KOTH["margin"],
            dethrone_z=_KOTH["dethrone_z"],
        )
        assert champion == entries[0]

    def test_the_memory_track_threads_the_map(self) -> None:
        entries = _five()
        fractions = {entries[1].agent_id: 0.5}
        params = MemoryFoldParams(
            margin=_KOTH["margin"],
            tail_size=_KOTH["tail_size"],
            rank_shares=_SHARES,
            dethrone_z=_KOTH["dethrone_z"],
            unpaid_fractions=fractions,
        )
        vector = memory_fold(
            TrackFoldInputs(memory_entries=entries, memory_params=params)
        )
        assert vector == _fold(entries, unpaid_fractions=fractions)


class TestTreasury:
    @pytest.mark.parametrize("service_bps", [0, 2500, 10000])
    @pytest.mark.parametrize("burn", [0.0, 0.3])
    def test_the_service_bucket_never_moves(
        self, service_bps: int, burn: float
    ) -> None:
        entries = _five()
        base, base_paid = split_unpaid_share(_fold(entries))
        payable, paid = split_unpaid_share(
            _fold(entries, unpaid_fractions={e.agent_id: 0.5 for e in entries[:2]})
        )
        assert base_paid == 1.0
        assert paid < 1.0

        def vector(miners: dict[str, float], fraction: float) -> dict[str, float]:
            return service_first_weights(
                miners,
                service_bps=service_bps,
                burn_share=burn,
                paid_miner_fraction=fraction,
                collector_hotkey="collector",
                collector_verified=True,
                burn_hotkey=_BURN_HOTKEY,
            )

        before = vector(base, base_paid)
        after = vector(payable, paid)
        assert after.get("collector", 0.0) == before.get("collector", 0.0)
        assert math.fsum(after.values()) == pytest.approx(1.0)
        if service_bps < 10000:
            assert after[_BURN_HOTKEY] > before.get(_BURN_HOTKEY, 0.0)

    def test_the_burn_destination_is_required_whenever_a_fraction_burns(self) -> None:
        entries = _five()
        payable, paid = split_unpaid_share(
            _fold(entries, unpaid_fractions={entries[4].agent_id: 0.1})
        )
        assert owner_burn_destination_required(
            payable, miner_share=1.0, paid_miner_fraction=paid
        )
        unchanged, full = split_unpaid_share(_fold(entries))
        assert not owner_burn_destination_required(
            unchanged, miner_share=1.0, paid_miner_fraction=full
        )


class TestLedgerResolver:
    def test_absent_or_empty_is_empty(self) -> None:
        entries = _five()
        assert _ledger_unpaid_fractions(LedgerResponse(entries=entries, count=6)) == {}

    def test_a_coherent_map_is_served_through(self) -> None:
        entries = _five()
        ledger = LedgerResponse(
            entries=entries,
            count=6,
            unpaid_fractions={entries[0].agent_id: 0.5, entries[1].agent_id: 1},
        )
        assert _ledger_unpaid_fractions(ledger) == {
            entries[0].agent_id: 0.5,
            entries[1].agent_id: 1.0,
        }

    @pytest.mark.parametrize(
        "bad",
        ["unknown-agent", 0.0, -0.1, 1.5, math.nan, math.inf],
    )
    def test_any_unusable_value_drops_the_whole_map(self, bad: object) -> None:
        entries = _five()
        fractions: dict[Any, float] = {entries[0].agent_id: 0.5}
        if bad == "unknown-agent":
            fractions[uuid4()] = 0.5
        else:
            assert isinstance(bad, float)
            fractions[entries[1].agent_id] = bad
        ledger = LedgerResponse(entries=entries, count=6, unpaid_fractions=fractions)
        assert _ledger_unpaid_fractions(ledger) == {}

    def test_the_provisional_incumbent_is_not_a_payable_key(self) -> None:
        held, runner_up = _held_and_runner_up()
        ledger = _ledger([runner_up], held, unpaid_fractions={held.agent_id: 0.5})
        assert _ledger_unpaid_fractions(ledger) == {}

    def test_the_wire_omits_an_empty_map(self) -> None:
        entries = _five()
        dumped = LedgerResponse(entries=entries, count=6).model_dump(mode="json")
        assert "unpaid_fractions" not in dumped
        served = LedgerResponse(
            entries=entries, count=6, unpaid_fractions={entries[0].agent_id: 0.5}
        ).model_dump(mode="json")
        assert served["unpaid_fractions"] == {str(entries[0].agent_id): 0.5}

    @pytest.mark.parametrize(
        "served",
        [
            {"VALID": True},
            {"VALID": "0.5"},
            {"VALID": None},
            {"VALID": [0.5]},
            {"not-a-uuid": 0.5},
            [0.5],
        ],
    )
    def test_a_coercible_malformed_map_is_dropped_whole(self, served: Any) -> None:
        """``true`` must never parse as ``1.0`` and burn a whole slot."""
        entries = _five()
        if isinstance(served, dict) and "VALID" in served:
            served = {str(entries[0].agent_id): served["VALID"]}
        raw = LedgerResponse(entries=entries, count=6).model_dump(mode="json")
        raw["unpaid_fractions"] = served
        ledger = LedgerResponse.model_validate(raw)
        assert ledger.unpaid_fractions == {}
        assert _ledger_unpaid_fractions(ledger) == {}
        # The rest of the ledger still parses and folds in full.
        assert len(ledger.entries) == len(entries)

    def test_a_well_formed_wire_map_still_parses(self) -> None:
        entries = _five()
        raw = LedgerResponse(entries=entries, count=6).model_dump(mode="json")
        raw["unpaid_fractions"] = {str(entries[0].agent_id): 0.5}
        ledger = LedgerResponse.model_validate(raw)
        assert _ledger_unpaid_fractions(ledger) == {entries[0].agent_id: 0.5}


class TestWorker:
    async def test_a_half_unattested_champion_through_the_worker(self) -> None:
        entries = _five()
        champion = entries[0]
        cfg = _config()
        cfg.koth_margin = _KOTH["margin"]
        plain = await _fold_worker(LedgerResponse(entries=entries, count=6), cfg)
        cfg = _config()
        cfg.koth_margin = _KOTH["margin"]
        burned = await _fold_worker(
            LedgerResponse(
                entries=entries, count=6, unpaid_fractions={champion.agent_id: 0.5}
            ),
            cfg,
        )
        assert burned.fold.champion_agent_id == plain.fold.champion_agent_id
        assert burned.weights[champion.miner_hotkey] == pytest.approx(
            plain.weights[champion.miner_hotkey] * 0.5
        )
        for entry in entries[1:5]:
            assert burned.weights[entry.miner_hotkey] == pytest.approx(
                plain.weights[entry.miner_hotkey]
            )
        assert burned.weights[_BURN_HOTKEY] == pytest.approx(
            plain.weights.get(_BURN_HOTKEY, 0.0)
            + plain.weights[champion.miner_hotkey] * 0.5
        )

    async def test_an_unusable_map_folds_as_protocol_35(self) -> None:
        entries = _five()
        cfg = _config()
        cfg.koth_margin = _KOTH["margin"]
        plain = await _fold_worker(LedgerResponse(entries=entries, count=6), cfg)
        cfg = _config()
        cfg.koth_margin = _KOTH["margin"]
        ignored = await _fold_worker(
            LedgerResponse(entries=entries, count=6, unpaid_fractions={uuid4(): 0.5}),
            cfg,
        )
        assert ignored.weights == plain.weights
