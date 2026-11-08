"""Protocol 36 on the Platform side: the pin prescribes what validators fold.

Folds the same ``ditto/tests/fixtures/unfilled_burn_cases.json`` boards as
``ditto/tests/validator/test_unfilled_burn.py``. Platform's projection and the
validator's weight fold must agree on every board, or every weight receipt
fails with ``ledger_pin_mismatch``. The pin checks below drive the real vector
classifier with the vector the shared fixture prescribes for a validator.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from ditto.api_models.validator import LedgerEntry
from ditto.api_server.koth import (
    emission_allocation,
    emission_shares,
    koth_entries_from_ledger,
    project_koth,
)
from ditto.api_server.ledger_pin import (
    _pin_competitive_shares,
    classify_vector_against_pins,
    pin_expected_burn,
    served_unfilled_burn,
)

_FIXTURE = (
    Path(__file__).resolve().parents[5] / "ditto/tests/fixtures/unfilled_burn_cases.json"
)
_CASES = json.loads(_FIXTURE.read_text())["cases"]
_T0 = datetime(2026, 11, 8, tzinfo=UTC)
BURN = "5Burn" + "B" * 43


def _ledger_entries(case: dict[str, Any]) -> list[LedgerEntry]:
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


def _allocate(case: dict[str, Any], *, burn_unfilled: bool):
    fold = koth_entries_from_ledger(_ledger_entries(case))
    projection = project_koth(fold, distinct_hotkeys=case["tie_pooling"])
    assert projection is not None
    return emission_allocation(
        fold,
        projection,
        tie_pooling=case["tie_pooling"],
        rank_shares=case["rank_shares"],
        burn_unfilled=burn_unfilled,
    )


def _pool(allocation: Any) -> tuple[dict[str, float], float]:
    total = sum(allocation.shares) + allocation.unfilled_share
    return (
        {
            member.miner_hotkey: share / total
            for member, share in zip(allocation.members, allocation.shares, strict=True)
        },
        allocation.unfilled_share / total,
    )


@pytest.mark.parametrize("case", _CASES, ids=[case["name"] for case in _CASES])
def test_projection_matches_shared_unfilled_burn_cases(case: dict[str, Any]) -> None:
    off, off_burned = _pool(_allocate(case, burn_unfilled=False))
    assert off == pytest.approx(case["off"], abs=1e-9)
    assert off_burned == 0.0

    on, on_burned = _pool(_allocate(case, burn_unfilled=True))
    assert on == pytest.approx(case["on"], abs=1e-9)
    assert on_burned == pytest.approx(case["burned_on"], abs=1e-9)


def test_off_is_todays_allocation_exactly() -> None:
    """Negative control: without the marker the allocation is the protocol-35 one."""
    case = next(c for c in _CASES if c["name"].startswith("four agents"))
    fold = koth_entries_from_ledger(_ledger_entries(case))
    projection = project_koth(fold)
    assert projection is not None
    legacy = emission_allocation(fold, projection)
    assert legacy == _allocate(case, burn_unfilled=False)
    assert legacy.unfilled_share == 0.0
    assert legacy.shares == emission_shares(projection)


def _pin(case: dict[str, Any], *, unfilled_mode: str | None) -> SimpleNamespace:
    served: dict[str, Any] = {"burn_share": 0.0}
    if unfilled_mode is not None:
        served["unfilled_share_mode"] = unfilled_mode
    if case["rank_shares"] is not None:
        served["rank_shares"] = case["rank_shares"]
    if case["tie_pooling"]:
        served["tie_weighting_mode"] = "pool"
    return SimpleNamespace(
        entries=[
            entry.model_dump(mode="json") for entry in _ledger_entries(case)
        ],
        context={"served": served},
        incumbent_agent_id=None,
    )


def _expected_vector(
    case: dict[str, Any], *, burn_unfilled: bool, burn_share: float
) -> dict[str, float]:
    """The vector a validator submits, from the fixture's independent shares.

    ``burn_share`` burns off the whole pool; the unfilled slots burn their fixed
    fraction of the rest. (``ditto.validator`` is a different top-level package
    from this one, so the validator's own functions run in its test; both read
    this fixture.)
    """
    shares = case["on"] if burn_unfilled else case["off"]
    burned = case["burned_on"] if burn_unfilled else 0.0
    vector = {hotkey: (1.0 - burn_share) * share for hotkey, share in shares.items()}
    owner_burn = burn_share + (1.0 - burn_share) * burned
    if owner_burn > 0.0:
        vector[BURN] = owner_burn
    return vector


@pytest.mark.parametrize("case", _CASES, ids=[case["name"] for case in _CASES])
def test_pin_prescribes_exactly_the_vector_validators_submit(
    case: dict[str, Any],
) -> None:
    for burn_share in (0.0, 0.25):
        pin = _pin(case, unfilled_mode="burn")
        expected = _pin_competitive_shares(pin)
        assert expected is not None
        vector = _expected_vector(case, burn_unfilled=True, burn_share=burn_share)
        total = sum(vector.values())
        assert (
            classify_vector_against_pins(
                vector,
                expected_current=expected,
                expected_previous=None,
                burn_hotkey=BURN,
            )
            == "current"
        )
        # The burn hotkey holds exactly what the pin says burns.
        assert vector.get(BURN, 0.0) / total == pytest.approx(
            pin_expected_burn(burn_share, expected), abs=1e-9
        )


def test_pin_shares_sum_below_one_exactly_when_a_slot_is_unfilled() -> None:
    four = next(c for c in _CASES if c["name"].startswith("four agents"))
    shares = _pin_competitive_shares(_pin(four, unfilled_mode="burn"))
    assert shares is not None
    assert sum(shares.values()) == pytest.approx(0.96)
    assert pin_expected_burn(0.0, shares) == pytest.approx(0.04)
    # Off renormalises to one and burns nothing: the protocol-35 pin.
    legacy = _pin_competitive_shares(_pin(four, unfilled_mode=None))
    assert legacy is not None
    assert sum(legacy.values()) == pytest.approx(1.0)
    assert pin_expected_burn(0.0, legacy) == pytest.approx(0.0)

    five = next(c for c in _CASES if c["name"].startswith("a full board"))
    full = _pin_competitive_shares(_pin(five, unfilled_mode="burn"))
    assert full is not None
    assert sum(full.values()) == pytest.approx(1.0, abs=1e-12)


def test_mixed_fleet_is_visible_as_a_burn_mismatch() -> None:
    """Negative control: a v35 validator on a v36 pin submits the wrong burn.

    The classifier compares recipients after removing the burn hotkey, so the
    mismatch shows in the burn share the collector checks against
    ``pin_expected_burn`` -- which is why the marker is fleet-gated.
    """
    four = next(c for c in _CASES if c["name"].startswith("four agents"))
    expected = _pin_competitive_shares(_pin(four, unfilled_mode="burn"))
    assert expected is not None
    stale_fold = _expected_vector(four, burn_unfilled=False, burn_share=0.0)
    assert stale_fold.get(BURN, 0.0) == pytest.approx(0.0)
    assert pin_expected_burn(0.0, expected) == pytest.approx(0.04)
    assert abs(stale_fold.get(BURN, 0.0) - pin_expected_burn(0.0, expected)) > 0.002


@pytest.mark.parametrize(
    ("served", "expected"),
    [({"unfilled_share_mode": "burn"}, True), ({}, False), ({"unfilled_share_mode": 1}, False)],
)
def test_pin_only_honours_the_exact_marker(served: dict[str, Any], expected: bool) -> None:
    assert served_unfilled_burn(served) is expected
