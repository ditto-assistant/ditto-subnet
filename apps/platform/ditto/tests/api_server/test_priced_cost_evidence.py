"""Bench v16 priced inference cost on the stored, public and owner projections.

The scorer reports ``details.priced_cost`` (whole-run inference priced at the
contract's locked rates and the factor it maps to). Platform carries the block
on every gate-evidence surface, adds the dollar figures, and recomputes the
factor from the block's own token counts with the shared Python mirror of the
Go rule; a block the published rule does not reproduce is flagged, never
corrected. Inputs are the shared Go/Python vectors.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ditto.api_models.validator import ScoreReport
from ditto.api_server.gate_evidence import build_gate_evidence, public_gate_evidence

_FIXTURE = (
    Path(__file__).resolve().parents[1] / "api_models/fixtures/score_report_v13.json"
)
_VECTORS = (
    Path(__file__).resolve().parents[5]
    / "services/dittobench-api/internal/scoregates/testdata/priced_cost_vectors.json"
)


def _vector(name: str) -> dict[str, Any]:
    vectors = json.loads(_VECTORS.read_text())["vectors"]
    return next(v for v in vectors if v["name"] == name)


def _report_with(priced_cost: object) -> ScoreReport:
    raw = json.loads(_FIXTURE.read_text())
    raw["details"]["priced_cost"] = priced_cost
    return ScoreReport.model_validate(raw)


def test_stored_and_public_projection_carry_the_priced_cost() -> None:
    block = _vector("v15_full_catalog")["summary"]
    stored = build_gate_evidence(_report_with(block), bench_version=16)
    assert stored is not None
    priced = stored["priced_cost"]
    assert priced["posture"] == "enforce"
    assert priced["applied"] is True
    assert priced["result"] == "priced"
    assert priced["window"] == "whole_run"
    assert priced["factor_bps"] == priced["applied_factor_bps"] == 6733
    assert priced["prompt_tokens"] == 22_318_268
    assert priced["cost_usd"] == pytest.approx(1.81699, abs=1e-5)
    assert priced["normalized_cost_usd"] == pytest.approx(1.81699, abs=1e-5)
    assert priced["rule_consistent"] is True
    # An enforced cost factor makes the run posture enforce.
    assert stored["posture"] == "enforce"
    public = public_gate_evidence(stored)
    assert public is not None and public.priced_cost is not None
    assert public.priced_cost.factor_bps == 6733
    assert public.priced_cost.rule_consistent is True


def test_a_block_the_rule_does_not_reproduce_is_flagged() -> None:
    block = dict(_vector("v15_full_catalog")["summary"])
    block["factor_bps"] = block["applied_factor_bps"] = 10_000
    stored = build_gate_evidence(_report_with(block), bench_version=16)
    assert stored is not None
    assert stored["priced_cost"]["factor_bps"] == 10_000
    assert stored["priced_cost"]["rule_consistent"] is False


def test_shadow_and_insufficient_evidence_apply_nothing() -> None:
    shadow = build_gate_evidence(
        _report_with(_vector("shadow_reports_not_applies")["summary"]),
        bench_version=16,
    )
    assert shadow is not None
    assert shadow["priced_cost"]["applied"] is False
    assert shadow["priced_cost"]["applied_factor_bps"] == 10_000
    assert shadow["priced_cost"]["factor_bps"] == 6733
    assert shadow["priced_cost"]["rule_consistent"] is True
    incomplete = build_gate_evidence(
        _report_with(_vector("incomplete_chat_accounting")["summary"]),
        bench_version=16,
    )
    assert incomplete is not None
    assert incomplete["priced_cost"]["result"] == "insufficient_evidence"
    assert incomplete["priced_cost"]["applied_factor_bps"] == 10_000


def test_malformed_block_drops_only_itself() -> None:
    stored = build_gate_evidence(_report_with({"posture": "loud"}), bench_version=16)
    assert stored is not None
    assert stored["priced_cost"] is None
    assert stored["catalog_gate"] is not None


def test_pre_v16_reports_carry_no_priced_cost() -> None:
    stored = build_gate_evidence(_report_with(None), bench_version=15)
    assert stored is not None
    assert stored["priced_cost"] is None
    # A block on a v13-v15 report is outside its contract and never projected.
    block = _vector("v15_full_catalog")["summary"]
    for version in (13, 14, 15):
        stored = build_gate_evidence(_report_with(block), bench_version=version)
        assert stored is not None
        assert stored["priced_cost"] is None
        public = public_gate_evidence(stored)
        assert public is not None and public.priced_cost is None
