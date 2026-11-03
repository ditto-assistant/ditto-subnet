"""Golden: an SN-350 emission allocation folds to the same vector as today.

Platform derives ``burn_share``, ``track_shares_bps`` and the Gamma buckets
from one typed-weights allocation
(``apps/platform/ditto/api_models/emission_allocation_math.py``). Validators
never see the allocation; they fold those three fields. This test loads the
derivation by path (standard library only) and runs its output through the
validator's real fold functions, so no validator release is needed and the
live production split is proven byte-identical.
"""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path
from types import ModuleType

import pytest

from ditto.api_models.validator import LedgerResponse
from ditto.validator.tracks import TRACK_GENERATOR, TrackState, build_default_registry
from ditto.validator.weights import (
    apply_miner_emission_cap,
    blend_track_weights,
    resolve_miner_emission_share,
    resolve_track_shares,
    track_allocated_share,
)
from ditto_screening_protocol.treasury_weight_math import service_first_weights

_MATH_PATH = (
    Path(__file__).resolve().parents[3]
    / "apps/platform/ditto/api_models/emission_allocation_math.py"
)
BURN = "5Burn" + "B" * 43
COLLECTOR = "5Coll" + "C" * 43
MEMORY = {"5Mem1" + "M" * 43: 0.65, "5Mem2" + "M" * 43: 0.14, "5Mem3" + "M" * 43: 0.1}
GENERATOR = {"5Gen1" + "G" * 43: 0.5, "5Gen2" + "G" * 43: 0.2}


def _math() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_allocation_math", _MATH_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fold(ledger: LedgerResponse, *, service_bps: int = 0) -> dict[str, float]:
    """The worker's fold (``ditto/validator/worker.py``) for fixed track vectors."""
    shares = resolve_track_shares(ledger, default={"memory": 10_000})
    registry = build_default_registry(
        track_shares_bps=shares,
        generator_state=TrackState.ACTIVE,
        generator_weight_eligible=True,
    )
    vectors = {"memory": dict(MEMORY), TRACK_GENERATOR: dict(GENERATOR)}
    paying = registry.claimed_shares_bps(vectors)
    eligible = {track: vectors[track] for track in paying if track in vectors}
    miner_weights = blend_track_weights(eligible, paying)
    miner_share = resolve_miner_emission_share(ledger, default_share=1.0)
    allocated = track_allocated_share(paying, eligible)
    if service_bps:
        return service_first_weights(
            miner_weights,
            service_bps=service_bps,
            burn_share=1 - miner_share,
            paid_miner_fraction=allocated,
            collector_hotkey=COLLECTOR,
            collector_verified=True,
            burn_hotkey=BURN,
        )
    return apply_miner_emission_cap(
        miner_weights, miner_share=miner_share * allocated, burn_hotkey=BURN
    )


def _ledger(
    burn_share: float, track_shares_bps: dict[str, int] | None
) -> LedgerResponse:
    payload: dict[str, object] = {
        "entries": [],
        "active_bench_version": 13,
        "count": 0,
        "burn_share": burn_share,
    }
    if track_shares_bps is not None:
        payload["track_shares_bps"] = track_shares_bps
    return LedgerResponse.model_validate(payload)


def _totals(vector: dict[str, float]) -> dict[str, float]:
    return {
        "burn": vector.get(BURN, 0.0),
        "gamma": vector.get(COLLECTOR, 0.0),
        "memory": math.fsum(vector.get(h, 0.0) for h in MEMORY),
        "generator": math.fsum(vector.get(h, 0.0) for h in GENERATOR),
    }


def test_live_split_folds_byte_identically_to_todays_three_settings() -> None:
    m = _math()
    E = m.AllocationEntry
    derived = m.derive_served(
        [
            E("burn", None, 5000),
            E("track", "memory", 3500),
            E("track", "generator", 1500),
        ]
    )
    # Production today: burn rev 18 burn_share=0.5, track rev 1 7000/3000, Gamma 0.
    today = _fold(_ledger(0.5, {"memory": 7000, "generator": 3000}))
    allocated = _fold(_ledger(derived.burn_share, derived.track_shares_bps))
    assert allocated == today
    totals = _totals(allocated)
    assert totals["burn"] == pytest.approx(0.5, abs=1e-12)
    assert totals["memory"] == pytest.approx(0.35, abs=1e-12)
    assert totals["generator"] == pytest.approx(0.15, abs=1e-12)


@pytest.mark.parametrize(
    "burn, memory, generator, gamma",
    [
        (5000, 3500, 1500, 0),
        (6000, 3000, 1000, 0),
        (2000, 8000, 0, 0),
        (4500, 3500, 1500, 500),
        (2000, 6000, 1500, 500),
        (7500, 2000, 500, 0),
    ],
)
def test_every_representable_allocation_folds_to_its_entries(
    burn: int, memory: int, generator: int, gamma: int
) -> None:
    m = _math()
    E = m.AllocationEntry
    entries = [
        E("burn", None, burn),
        E("track", "memory", memory),
        E("track", "generator", generator),
    ]
    if gamma:
        entries.append(E("gamma", "gm", gamma))
    derived = m.derive_served(entries)
    vector = _fold(
        _ledger(derived.burn_share, derived.track_shares_bps),
        service_bps=sum(derived.gamma_bps.values()),
    )
    totals = _totals(vector)
    assert math.fsum(vector.values()) == pytest.approx(1.0, abs=1e-12)
    assert totals["gamma"] == pytest.approx(gamma / 10_000, abs=1e-12)
    assert totals["burn"] == pytest.approx(burn / 10_000, abs=1e-12)
    assert totals["memory"] == pytest.approx(memory / 10_000, abs=1e-12)
    assert totals["generator"] == pytest.approx(generator / 10_000, abs=1e-12)


def test_a_withheld_track_map_still_burns_exactly_the_allocated_burn() -> None:
    """Before every weight setter reports protocol 33 the map is not served.

    The canonical derivation keeps the burn in ``burn_share``, so the
    memory-only fallback still burns exactly the allocated burn.
    """
    m = _math()
    E = m.AllocationEntry
    derived = m.derive_served(
        [
            E("burn", None, 6000),
            E("track", "memory", 3000),
            E("track", "generator", 1000),
        ]
    )
    totals = _totals(_fold(_ledger(derived.burn_share, None)))
    assert totals["burn"] == pytest.approx(0.6, abs=1e-12)
    assert totals["memory"] == pytest.approx(0.4, abs=1e-12)
    assert totals["generator"] == 0.0


def test_unrepresentable_split_is_refused_not_rounded() -> None:
    m = _math()
    E = m.AllocationEntry
    with pytest.raises(m.AllocationError, match="not exactly representable"):
        m.derive_served(
            [
                E("burn", None, 4000),
                E("track", "memory", 4000),
                E("track", "generator", 2000),
            ]
        )
