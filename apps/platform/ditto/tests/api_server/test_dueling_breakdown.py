"""Bench v16 dueling and priced-cost breakdown on the public score reads.

``public_dueling_breakdown`` projects the stored ``scores.dueling`` aggregates
and the ``priced_cost`` gate evidence: the dueling formula, datagen half and
gate, the applied priced-cost factor and window, and per column ``n``/``sum``/
mean, substitution, public default and self-mask. It must reproduce the signed
composite, mask exactly as the Backroom canary read (no owner key, no generator
submission id, no ``sum_sq``, no slice digest), and leave pre-16 rows'
published JSON unchanged.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from ditto.api_models.public import (
    DUELING_BREAKDOWN_FORMULA,
    DUELING_HELD_OUT_BREAKDOWN_FORMULA,
)
from ditto.api_server.dueling_breakdown import public_dueling_breakdown
from ditto.api_server.endpoints import public as public_endpoint
from ditto_screening_protocol.dueling import DuelingReport, dueling_datagen_qualifier

OWNER = "5OwnerOfTheScoredAgentXXXXXXXXXXXXXXXXXXXXXXXX"
RIVAL = "5RivalGeneratorOwnerXXXXXXXXXXXXXXXXXXXXXXXXXX"
OTHER = "5AnotherGeneratorOwnerXXXXXXXXXXXXXXXXXXXXXXXX"
_VALIDATOR = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
_SEASON = "8f2b6a52-3b7f-4a55-9d0c-0d6c7d2a9e11"


def dueling_report(*, owner: str = OWNER) -> dict[str, Any]:
    """c0 public default, c1 the agent's own (masked), c3 substituted."""
    return {
        "season_id": _SEASON,
        "season_index": 3,
        "slice_sha256": "ab" * 32,
        "owner_key": owner,
        "datagen_raw": 0.9,
        "datagen_gate": 0.79832,
        "column_weight": 0.1,
        "composite_stderr": 0.0125,
        "columns": [
            {
                "column_id": "c0",
                "owner_key": "",
                "submission_id": "default-gen",
                "grading_bench_version": 16,
                "n": 35,
                "sum": 28.0,
                "sum_sq": 24.0,
            },
            {
                "column_id": "c1",
                "owner_key": owner,
                "submission_id": "own-gen",
                "grading_bench_version": 16,
                "n": 35,
                "sum": 35.0,
                "sum_sq": 35.0,
            },
            {
                "column_id": "c2",
                "owner_key": RIVAL,
                "submission_id": "rival-gen",
                "grading_bench_version": 15,
                "n": 35,
                "sum": 21.0,
                "sum_sq": 15.0,
            },
            {
                "column_id": "c3",
                "owner_key": "",
                "submission_id": "default-gen",
                "grading_bench_version": 16,
                "n": 35,
                "sum": 17.5,
                "sum_sq": 10.0,
                "substituted": True,
            },
            {
                "column_id": "c4",
                "owner_key": OTHER,
                "submission_id": "other-gen",
                "grading_bench_version": 16,
                "n": 35,
                "sum": 14.0,
                "sum_sq": 7.0,
            },
        ],
    }


def priced_gate_evidence(*, applied_bps: int = 9392) -> dict[str, Any]:
    return {
        "contract_version": 1,
        "bench_version": 16,
        "posture": "enforce",
        "priced_cost": {
            "contract_id": "priced-cost/v1",
            "posture": "enforce",
            "applied": applied_bps != 10_000,
            "result": "priced",
            "window": "whole_run",
            "factor_bps": 9392,
            "applied_factor_bps": applied_bps,
            "cost_nano_usd": 1_200_000_000,
        },
    }


def test_v16_breakdown_reproduces_the_signed_composite_with_self_mask() -> None:
    raw = dueling_report()
    breakdown = public_dueling_breakdown(
        bench_version=16, dueling=raw, gate_evidence=priced_gate_evidence()
    )
    assert breakdown is not None
    assert breakdown.formula == DUELING_BREAKDOWN_FORMULA
    assert breakdown.season_id == _SEASON
    assert breakdown.datagen_raw == 0.9
    assert breakdown.datagen_gate == 0.79832
    assert breakdown.column_weight == 0.1
    assert breakdown.counted_columns == 4
    assert breakdown.datagen_share == pytest.approx(0.6)
    assert breakdown.generator_sum == pytest.approx(0.23)
    assert breakdown.pre_gate_composite == pytest.approx(0.77)
    # Bit for bit the reported (self-masked) composite.
    assert breakdown.composite == DuelingReport.model_validate(raw).self_composite()
    assert breakdown.composite == pytest.approx(0.79832 * 0.77)
    assert breakdown.priced_cost_applied_factor_bps == 9392
    assert breakdown.priced_cost_factor_bps == 9392
    assert breakdown.priced_cost_factor == pytest.approx(0.9392)
    assert breakdown.priced_cost_window == "whole_run"
    assert breakdown.priced_cost_posture == "enforce"
    assert breakdown.other_gate_factor == pytest.approx(0.85)

    by_id = {c.column_id: c for c in breakdown.columns}
    assert [c.column_id for c in breakdown.columns] == ["c0", "c1", "c2", "c3", "c4"]
    assert by_id["c0"].public_default and not by_id["c0"].substituted
    assert by_id["c1"].self_masked and by_id["c1"].contribution == 0.0
    assert by_id["c1"].mean == 1.0
    assert by_id["c2"].grading_bench_version == 15
    assert by_id["c2"].mean == pytest.approx(0.6)
    assert by_id["c2"].contribution == pytest.approx(0.06)
    assert by_id["c3"].substituted and not by_id["c3"].public_default
    assert not any(by_id[c].self_masked for c in ("c0", "c2", "c3", "c4"))


def v19_dueling_report(*, datagen_raw: float = 0.6) -> dict[str, Any]:
    """The same slice at bench 19: w = 0.15 and the datagen qualifier."""
    return {
        **dueling_report(),
        "datagen_raw": datagen_raw,
        "column_weight": 0.15,
        "datagen_qualifier": dueling_datagen_qualifier(19, datagen_raw),
    }


def test_v19_breakdown_applies_the_datagen_qualifier() -> None:
    raw = v19_dueling_report()
    breakdown = public_dueling_breakdown(
        bench_version=19, dueling=raw, gate_evidence=priced_gate_evidence()
    )
    assert breakdown is not None
    assert breakdown.formula == DUELING_HELD_OUT_BREAKDOWN_FORMULA
    assert breakdown.column_weight == 0.15
    assert breakdown.datagen_qualifier == pytest.approx(0.75)
    assert breakdown.counted_columns == 4
    assert breakdown.datagen_share == pytest.approx(0.4)
    # 0.15 x (0.8 + 0.6 + 0.5 + 0.4): c1, the agent's own, is masked.
    assert breakdown.generator_sum == pytest.approx(0.345)
    assert breakdown.pre_gate_composite == pytest.approx(0.4 * 0.6 + 0.345)
    # Bit for bit the reported composite, the qualifier included.
    assert breakdown.composite == DuelingReport.model_validate(raw).self_composite()
    assert breakdown.composite == pytest.approx(0.79832 * 0.75 * (0.24 + 0.345))
    assert breakdown.model_dump(mode="json")["datagen_qualifier"] == pytest.approx(0.75)
    # Above the 0.8 floor the qualifier is 1.
    high = public_dueling_breakdown(
        bench_version=19,
        dueling=v19_dueling_report(datagen_raw=0.9),
        gate_evidence=None,
    )
    assert high is not None and high.datagen_qualifier == 1.0
    assert high.composite == pytest.approx(0.79832 * (0.4 * 0.9 + 0.345))


def test_breakdown_below_v19_publishes_no_qualifier() -> None:
    breakdown = public_dueling_breakdown(
        bench_version=18, dueling=dueling_report(), gate_evidence=None
    )
    assert breakdown is not None
    assert breakdown.formula == DUELING_BREAKDOWN_FORMULA
    assert "datagen_qualifier" not in breakdown.model_dump(mode="json")


def test_breakdown_masks_like_the_canary_read() -> None:
    breakdown = public_dueling_breakdown(
        bench_version=16, dueling=dueling_report(), gate_evidence=priced_gate_evidence()
    )
    assert breakdown is not None
    published = json.dumps(breakdown.model_dump(mode="json"))
    for secret in (
        OWNER,
        RIVAL,
        OTHER,
        "own-gen",
        "rival-gen",
        "other-gen",
        "default-gen",
        "ab" * 32,
    ):
        assert secret not in published
    for key in ("owner_key", "submission_id", "sum_sq", "slice_sha256"):
        assert f'"{key}"' not in published


def test_shadow_priced_cost_reports_a_neutral_applied_factor() -> None:
    breakdown = public_dueling_breakdown(
        bench_version=16,
        dueling=dueling_report(),
        gate_evidence=priced_gate_evidence(applied_bps=10_000),
    )
    assert breakdown is not None
    assert breakdown.priced_cost_applied_factor_bps == 10_000
    assert breakdown.priced_cost_factor_bps == 9392
    assert breakdown.other_gate_factor == pytest.approx(0.79832)


@pytest.mark.parametrize(
    ("bench_version", "dueling", "gate_evidence"),
    [
        (15, dueling_report(), priced_gate_evidence()),
        (None, dueling_report(), None),
        (16, None, priced_gate_evidence()),
        (16, {"season_id": _SEASON}, None),
        (16, {**dueling_report(), "column_weight": 0.2}, None),
        # A block weighted for another bench version than its row.
        (19, dueling_report(), None),
        (18, v19_dueling_report(), None),
    ],
)
def test_breakdown_fails_closed(
    bench_version: int | None, dueling: object, gate_evidence: object
) -> None:
    assert (
        public_dueling_breakdown(
            bench_version=bench_version, dueling=dueling, gate_evidence=gate_evidence
        )
        is None
    )


def test_breakdown_without_priced_cost_still_explains_the_dueling_half() -> None:
    breakdown = public_dueling_breakdown(
        bench_version=16, dueling=dueling_report(), gate_evidence={"bogus": True}
    )
    assert breakdown is not None
    assert breakdown.priced_cost_applied_factor_bps is None
    assert breakdown.priced_cost_factor is None
    assert breakdown.other_gate_factor is None


def _validator_score(**overrides: Any):
    row = {
        "validator_hotkey": _VALIDATOR,
        "composite": 0.614706,
        "tool_mean": 0.9,
        "memory_mean": 0.9,
        "median_ms": 500,
        "n": 350,
        "seed": 42,
        "run_id": "run-v16",
        "signature": "ab" * 64,
        "generated_at": datetime(2026, 11, 4, tzinfo=UTC),
        "details": {"bench_version": 16},
        "dueling": dueling_report(),
        "gate_evidence": priced_gate_evidence(),
    }
    row.update(overrides)
    return public_endpoint._public_validator_score(SimpleNamespace(**row))


def test_public_validator_score_carries_the_v16_breakdown() -> None:
    published = _validator_score().model_dump(mode="json")
    breakdown = published["dueling_breakdown"]
    assert breakdown["formula"] == DUELING_BREAKDOWN_FORMULA
    assert breakdown["priced_cost_applied_factor_bps"] == 9392
    assert len(breakdown["columns"]) == 5
    assert OWNER not in json.dumps(published)


def test_pre_v16_validator_score_json_is_unchanged() -> None:
    """Below v16 the new field is omitted, not null: the row's JSON is the
    same bytes it was before the field existed."""
    legacy = _validator_score(
        details={"bench_version": 15}, dueling=None, gate_evidence=None
    )
    published = legacy.model_dump(mode="json")
    assert "dueling_breakdown" not in published
    assert "dueling_breakdown" not in legacy.model_dump_json()
    # A stray aggregate on a pre-16 row is never projected.
    stray = _validator_score(details={"bench_version": 15})
    assert "dueling_breakdown" not in stray.model_dump(mode="json")
