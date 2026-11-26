"""SN-500 step 2: the P95 harness-memory factor on Platform's projections.

The scorer reports ``details.memory_factor``. Platform stores it on the
operator gate evidence, re-derives ``factor_bps`` with the shared Python
mirror of the Go curve, and keeps it off the public gate projection. The
public dueling breakdown shows it only once it was applied.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ditto.api_models.validator import ScoreReport
from ditto.api_server.gate_evidence import build_gate_evidence, public_gate_evidence

_FIXTURE = (
    Path(__file__).resolve().parents[1] / "api_models/fixtures/score_report_v13.json"
)
_MIB = 1 << 20


def _block(**overrides: Any) -> dict[str, Any]:
    block: dict[str, Any] = {
        "posture": "shadow",
        "applied": False,
        "result": "measured",
        "p95_bytes": 1280 * _MIB,
        "peak_bytes": 1400 * _MIB,
        "limit_bytes": 2048 * _MIB,
        "free_bytes": 512 * _MIB,
        "samples": 300,
        "floor_bps": 9000,
        "factor_bps": 9500,
        "applied_factor_bps": 10_000,
    }
    block.update(overrides)
    return block


def _report_with(memory_factor: object | None) -> ScoreReport:
    raw = json.loads(_FIXTURE.read_text())
    if memory_factor is not None:
        raw["details"]["memory_factor"] = memory_factor
    return ScoreReport.model_validate(raw)


def test_shadow_block_is_stored_checked_and_kept_off_the_public_projection() -> None:
    stored = build_gate_evidence(_report_with(_block()), bench_version=18)
    assert stored is not None
    memory = stored["memory_factor"]
    assert memory["posture"] == "shadow"
    assert memory["applied"] is False
    assert memory["factor_bps"] == 9500
    assert memory["applied_factor_bps"] == 10_000
    assert memory["rule_consistent"] is True
    public = public_gate_evidence(stored)
    assert public is not None
    assert "memory_factor" not in public.model_dump()


def test_enforced_block_is_consistent_when_it_applies_the_curve() -> None:
    block = _block(posture="enforce", applied=True, applied_factor_bps=9500)
    stored = build_gate_evidence(_report_with(block), bench_version=18)
    assert stored is not None
    assert stored["memory_factor"]["rule_consistent"] is True
    assert stored["posture"] == "enforce"


def test_a_block_the_curve_does_not_reproduce_is_flagged_not_corrected() -> None:
    for bad in (
        _block(factor_bps=9900),
        _block(posture="enforce", applied=True, applied_factor_bps=10_000),
        _block(posture="shadow", applied=True, applied_factor_bps=9500),
        _block(result="no_telemetry", factor_bps=9500),
    ):
        stored = build_gate_evidence(_report_with(bad), bench_version=18)
        assert stored is not None
        assert stored["memory_factor"]["rule_consistent"] is False
        assert stored["memory_factor"]["factor_bps"] == bad["factor_bps"]


def test_an_unmeasured_run_is_consistent_at_full_factor() -> None:
    block = _block(
        result="no_telemetry",
        p95_bytes=0,
        peak_bytes=0,
        samples=0,
        factor_bps=10_000,
        posture="enforce",
    )
    stored = build_gate_evidence(_report_with(block), bench_version=18)
    assert stored is not None
    assert stored["memory_factor"]["rule_consistent"] is True


def test_pre_v16_and_absent_blocks_keep_the_earlier_shape() -> None:
    early = build_gate_evidence(_report_with(_block()), bench_version=15)
    assert early is None or "memory_factor" not in early
    absent = build_gate_evidence(_report_with(None), bench_version=18)
    assert absent is None or "memory_factor" not in absent


def test_malformed_block_drops_only_itself() -> None:
    stored = build_gate_evidence(
        _report_with({"posture": "sometimes", "result": "measured"}), bench_version=18
    )
    assert stored is None or "memory_factor" not in stored
