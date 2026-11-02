"""Fail-closed per-harness router failure classification (telemetry only)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from ditto.api_models.router_ledger import (
    RouterHarness,
    RouterHarnessResult,
    RouterLedgerEntry,
)
from ditto.validator.router_failure import (
    DEFAULT_HARNESS_WEIGHTS,
    RouterFailureStage,
    classify_harness_result,
    classify_router_entry,
)

_T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _result(
    harness: RouterHarness, *, operational: bool, floor_pass: bool
) -> RouterHarnessResult:
    return RouterHarnessResult(
        harness=harness,
        operational=operational,
        floor_pass=floor_pass,
        efficiency=0.5 if (operational and floor_pass) else 0.0,
        upstream_token_cost_micros=1000,
    )


def _entry(*results: RouterHarnessResult, combined: float = 0.5) -> RouterLedgerEntry:
    return RouterLedgerEntry(
        miner_hotkey="miner",
        agent_id=uuid4(),
        router_contract_version=1,
        weight_eligible=False,
        combined_score=combined,
        harnesses=results,
        first_seen=_T0,
    )


def test_classify_harness_result_gate_matrix() -> None:
    ok = _result(RouterHarness.CLAUDE_CODE, operational=True, floor_pass=True)
    floor = _result(RouterHarness.CLAUDE_CODE, operational=True, floor_pass=False)
    down = _result(RouterHarness.CLAUDE_CODE, operational=False, floor_pass=False)
    impossible = _result(RouterHarness.CLAUDE_CODE, operational=False, floor_pass=True)

    assert classify_harness_result(ok) is None
    assert classify_harness_result(floor) is RouterFailureStage.CORRECTNESS_FLOOR
    assert classify_harness_result(down) is RouterFailureStage.HARNESS_OPERATIONAL
    # floor_pass without operational is contract-impossible -> flagged, not trusted.
    assert classify_harness_result(impossible) is RouterFailureStage.HARNESS_RUNTIME


def test_all_pass_contributes_full_pool_no_dominant_stage() -> None:
    entry = _entry(
        *(_result(h, operational=True, floor_pass=True) for h in RouterHarness)
    )
    report = classify_router_entry(entry)
    assert report.dominant_stage is None
    assert all(outcome.contributed for outcome in report.outcomes)
    assert report.contributed_share == pytest.approx(1.0)
    assert report.forfeited_share == pytest.approx(0.0)


def test_grok_failure_forfeits_only_its_slice() -> None:
    results = []
    for harness in RouterHarness:
        passed = harness is not RouterHarness.GROK
        results.append(_result(harness, operational=passed, floor_pass=passed))
    report = classify_router_entry(_entry(*results))

    grok = next(o for o in report.outcomes if o.harness is RouterHarness.GROK)
    assert grok.contributed is False
    assert grok.stage is RouterFailureStage.HARNESS_OPERATIONAL
    assert grok.weight == pytest.approx(0.10)
    # The other three slices stay intact.
    assert report.forfeited_share == pytest.approx(0.10)
    assert report.contributed_share == pytest.approx(0.90)
    # Grok is the only (thus largest) forfeited slice.
    assert report.dominant_stage is RouterFailureStage.HARNESS_OPERATIONAL


def test_dominant_stage_is_the_largest_forfeited_slice() -> None:
    # Claude Code (0.40, floor fail) and Grok (0.10, down) both forfeit; CC wins.
    results = [
        _result(RouterHarness.CLAUDE_CODE, operational=True, floor_pass=False),
        _result(RouterHarness.CODEX, operational=True, floor_pass=True),
        _result(RouterHarness.OPENCODE, operational=True, floor_pass=True),
        _result(RouterHarness.GROK, operational=False, floor_pass=False),
    ]
    report = classify_router_entry(_entry(*results))
    assert report.dominant_stage is RouterFailureStage.CORRECTNESS_FLOOR
    assert report.forfeited_share == pytest.approx(0.50)


def test_nothing_operational_is_router_boot() -> None:
    results = [_result(h, operational=False, floor_pass=False) for h in RouterHarness]
    report = classify_router_entry(_entry(*results, combined=0.0))
    assert report.dominant_stage is RouterFailureStage.ROUTER_BOOT
    assert report.forfeited_share == pytest.approx(1.0)


def test_missing_expected_harness_is_telemetry_forfeit() -> None:
    # Scorer reported only three harnesses; the absent one fails closed.
    results = [
        _result(RouterHarness.CLAUDE_CODE, operational=True, floor_pass=True),
        _result(RouterHarness.CODEX, operational=True, floor_pass=True),
        _result(RouterHarness.OPENCODE, operational=True, floor_pass=True),
    ]
    report = classify_router_entry(_entry(*results))
    grok = next(o for o in report.outcomes if o.harness is RouterHarness.GROK)
    assert grok.contributed is False
    assert grok.stage is RouterFailureStage.TELEMETRY
    assert grok.weight == pytest.approx(0.10)
    assert report.dominant_stage is RouterFailureStage.TELEMETRY


def test_default_weights_sum_to_one() -> None:
    assert sum(DEFAULT_HARNESS_WEIGHTS.values()) == pytest.approx(1.0)
