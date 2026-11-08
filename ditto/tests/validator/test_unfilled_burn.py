"""Protocol 36: rank slots no eligible agent fills burn instead of renormalising.

The boards live in ``ditto/tests/fixtures/unfilled_burn_cases.json`` and are
folded here by the validator and by the Platform projection in
``apps/platform/ditto/tests/api_server/test_unfilled_burn.py``. A divergence
between the two folds fails every weight receipt with ``ledger_pin_mismatch``,
so the expectations are exact fractions of the frozen curve, not the output of
either fold.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from ditto.api_models.validator import LedgerEntry, LedgerResponse
from ditto.validator.config import KOTH_DETHRONE_Z, KOTH_MARGIN, KOTH_RANK_SHARES
from ditto.validator.tracks import MemoryFoldParams, TrackFoldInputs, memory_fold
from ditto.validator.weights import (
    UNPAID_SHARE_KEY,
    apply_miner_emission_cap,
    blend_track_weights,
    compute_weights,
    split_unpaid_share,
    track_allocated_share,
)
from ditto.validator.worker import _ledger_unfilled_burn

_CASES = json.loads(
    (
        Path(__file__).resolve().parents[1] / "fixtures/unfilled_burn_cases.json"
    ).read_text()
)["cases"]
_T0 = datetime(2026, 11, 8, tzinfo=UTC)
BURN = "5Burn" + "B" * 43


def _entries(case: dict[str, Any]) -> list[LedgerEntry]:
    return [
        LedgerEntry(
            miner_hotkey=f"hotkey-{spec['name']}",
            agent_id=UUID(int=index),
            composite=spec["composite"],
            composite_stderr=spec["stderr"],
            n=350,
            first_seen=_T0 + timedelta(minutes=spec["minutes"]),
            sha256="ab" * 32,
            size_bytes=None,
            score_proofs=[],
            confirmation_composites=None,
            confirmation_seeds=None,
            run_id=f"run-{index}",
            seed=index,
            validator_hotkey="hotkey-validator",
            bench_version=14,
            signature=None,
            status="scored",
        )
        for index, spec in enumerate(case["entries"], start=1)
    ]


def _fold(case: dict[str, Any], *, burn_unfilled: bool, **extra: Any) -> dict[str, float]:
    return compute_weights(
        _entries(case),
        margin=KOTH_MARGIN,
        tail_size=4,
        rank_shares=case["rank_shares"] or KOTH_RANK_SHARES,
        dethrone_z=KOTH_DETHRONE_Z,
        tie_pooling=case["tie_pooling"],
        burn_unfilled=burn_unfilled,
        **extra,
    )


def _pool_shares(weights: dict[str, float]) -> tuple[dict[str, float], float]:
    """Each recipient's share of the miner pool, and the share that burns.

    The denominator is every positive weight including the unpaid key, which is
    how ``split_unpaid_share`` measures the paid fraction in the worker.
    """
    total = math.fsum(weight for weight in weights.values() if weight > 0.0)
    paid = {
        hotkey: weight / total
        for hotkey, weight in weights.items()
        if hotkey != UNPAID_SHARE_KEY
    }
    return paid, weights.get(UNPAID_SHARE_KEY, 0.0) / total


@pytest.mark.parametrize("case", _CASES, ids=[case["name"] for case in _CASES])
def test_validator_fold_matches_shared_unfilled_burn_cases(case: dict[str, Any]) -> None:
    off, off_burned = _pool_shares(_fold(case, burn_unfilled=False))
    assert off == pytest.approx(case["off"], abs=1e-9)
    assert off_burned == 0.0

    on, on_burned = _pool_shares(_fold(case, burn_unfilled=True))
    assert on == pytest.approx(case["on"], abs=1e-9)
    assert on_burned == pytest.approx(case["burned_on"], abs=1e-9)


@pytest.mark.parametrize("case", _CASES, ids=[case["name"] for case in _CASES])
def test_marker_off_is_the_protocol_35_vector_byte_for_byte(
    case: dict[str, Any],
) -> None:
    """Off adds no key and moves no weight, however the flag is spelled."""
    legacy = compute_weights(
        _entries(case),
        margin=KOTH_MARGIN,
        tail_size=4,
        rank_shares=case["rank_shares"] or KOTH_RANK_SHARES,
        dethrone_z=KOTH_DETHRONE_Z,
        tie_pooling=case["tie_pooling"],
    )
    assert _fold(case, burn_unfilled=False) == legacy
    assert UNPAID_SHARE_KEY not in legacy


def test_negative_control_a_fold_that_ignores_the_marker_fails_the_cases() -> None:
    """Every short board must distinguish on from off, or the fixture is inert."""
    short = [case for case in _CASES if case["burned_on"] > 0.0]
    assert len(short) >= 6
    for case in short:
        assert case["on"] != pytest.approx(case["off"], abs=1e-6), case["name"]


def test_four_agents_burn_exactly_rank_five() -> None:
    case = next(c for c in _CASES if c["name"].startswith("four agents"))
    weights = _fold(case, burn_unfilled=True)
    assert weights[UNPAID_SHARE_KEY] == pytest.approx(0.04, abs=1e-12)
    assert [weights[f"hotkey-m{i}"] for i in range(1, 5)] == pytest.approx(
        [0.65, 0.14, 0.10, 0.07]
    )


def test_one_agent_takes_only_the_champion_share() -> None:
    case = next(c for c in _CASES if c["name"].startswith("one agent"))
    weights = _fold(case, burn_unfilled=True)
    assert weights["hotkey-m1"] == pytest.approx(0.65)
    assert weights[UNPAID_SHARE_KEY] == pytest.approx(0.35)
    # Off, the lone champion renormalises onto the whole pool.
    assert _fold(case, burn_unfilled=False) == {"hotkey-m1": 0.65}


def test_no_scored_agent_still_folds_to_nothing() -> None:
    """Zero miners keep burning 100% through the existing idle vector."""
    case = {**_CASES[0], "entries": []}
    assert _fold(case, burn_unfilled=True) == {}
    assert apply_miner_emission_cap({}, miner_share=1.0, burn_hotkey=BURN) == {
        BURN: 1.0
    }


def _final_vector(
    memory: dict[str, float],
    *,
    burn_share: float,
    generator: dict[str, float] | None = None,
    shares_bps: dict[str, int] | None = None,
) -> dict[str, float]:
    """The worker's blend, unpaid split and cap for fixed track vectors."""
    vectors = {"memory": memory}
    bps = shares_bps or {"memory": 10_000}
    if generator is not None:
        vectors["generator"] = generator
    miner_weights, paid_fraction = split_unpaid_share(blend_track_weights(vectors, bps))
    allocated = track_allocated_share(bps, vectors)
    return apply_miner_emission_cap(
        miner_weights,
        miner_share=(1.0 - burn_share) * allocated * paid_fraction,
        burn_hotkey=BURN,
    )


def test_burn_share_and_unfilled_slots_do_not_double_count() -> None:
    """``burn_share`` burns off the whole pool; the unfilled slot burns off the rest.

    Four agents, 10% burn_share: miners hold 0.9 of the vector at their fixed
    fractions (0.65, 0.14, 0.10, 0.07 of 0.9) and the burn hotkey holds the
    10% plus rank 5's 4% of the remaining 90%.
    """
    case = next(c for c in _CASES if c["name"].startswith("four agents"))
    vector = _final_vector(_fold(case, burn_unfilled=True), burn_share=0.1)
    assert vector["hotkey-m1"] == pytest.approx(0.9 * 0.65)
    assert vector["hotkey-m4"] == pytest.approx(0.9 * 0.07)
    assert vector[BURN] == pytest.approx(0.1 + 0.9 * 0.04)
    assert math.fsum(vector.values()) == pytest.approx(1.0)


def test_track_split_scales_only_the_memory_curve() -> None:
    """Memory 70% / generator 30%: rank 5's burn is 4% of the memory 70% only."""
    case = next(c for c in _CASES if c["name"].startswith("four agents"))
    generator = {"gen-1": 0.5, "gen-2": 0.5}
    vector = _final_vector(
        _fold(case, burn_unfilled=True),
        burn_share=0.0,
        generator=generator,
        shares_bps={"memory": 7_000, "generator": 3_000},
    )
    assert vector["gen-1"] == pytest.approx(0.15)
    assert vector["hotkey-m1"] == pytest.approx(0.7 * 0.65)
    assert vector[BURN] == pytest.approx(0.7 * 0.04)
    assert math.fsum(vector.values()) == pytest.approx(1.0)


def test_unpaid_incumbent_and_unfilled_slot_share_one_key() -> None:
    """A provisional incumbent (protocol 28) and the burn add up, never overwrite."""
    case = next(c for c in _CASES if c["name"].startswith("four agents"))
    weights = _fold(case, burn_unfilled=True, unpaid_agent_id=UUID(int=1))
    assert weights[UNPAID_SHARE_KEY] == pytest.approx(0.65 + 0.04)
    assert "hotkey-m1" not in weights
    vector = _final_vector(weights, burn_share=0.0)
    assert vector["hotkey-m2"] == pytest.approx(0.14)
    assert vector[BURN] == pytest.approx(0.69)


def test_a_lone_unpaid_incumbent_leaves_everything_burning() -> None:
    case = next(c for c in _CASES if c["name"].startswith("one agent"))
    weights = _fold(case, burn_unfilled=True, unpaid_agent_id=UUID(int=1))
    assert weights == {UNPAID_SHARE_KEY: pytest.approx(1.0)}
    assert _final_vector(weights, burn_share=0.0) == {BURN: 1.0}


def test_memory_fold_threads_the_marker() -> None:
    case = next(c for c in _CASES if c["name"].startswith("four agents"))
    params = MemoryFoldParams(
        margin=KOTH_MARGIN,
        tail_size=4,
        rank_shares=KOTH_RANK_SHARES,
        dethrone_z=KOTH_DETHRONE_Z,
        burn_unfilled=True,
    )
    inputs = TrackFoldInputs(memory_entries=_entries(case), memory_params=params)
    assert memory_fold(inputs)[UNPAID_SHARE_KEY] == pytest.approx(0.04)


@pytest.mark.parametrize(
    ("served", "expected"),
    [("burn", True), (None, False), ("off", False), ("", False), (True, False)],
)
def test_only_the_exact_marker_turns_the_burn_on(served: Any, expected: bool) -> None:
    ledger = LedgerResponse.model_construct(entries=[], unfilled_share_mode=served)
    assert _ledger_unfilled_burn(ledger) is expected


def test_wire_marker_is_absent_unless_set() -> None:
    """Off adds no byte to the ledger JSON, so older readers see no change."""
    payload = LedgerResponse.model_validate(
        {"entries": [], "active_bench_version": 14, "count": 0}
    ).model_dump(mode="json")
    assert "unfilled_share_mode" not in payload
    on = LedgerResponse.model_validate(
        {
            "entries": [],
            "active_bench_version": 14,
            "count": 0,
            "unfilled_share_mode": "burn",
        }
    )
    assert on.model_dump(mode="json")["unfilled_share_mode"] == "burn"
    with pytest.raises(ValueError):
        LedgerResponse.model_validate(
            {
                "entries": [],
                "active_bench_version": 14,
                "count": 0,
                "unfilled_share_mode": "renormalise",
            }
        )
