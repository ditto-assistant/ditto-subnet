"""Cross-repo wire round-trip: the Go scoring engine's ScoreReport survives ingest.

``fixtures/score_report_v3.json`` is emitted by the Go source of truth
(``dittobench-datagen/protocol``) with every wire field populated, including the
bench_version 3 audit fields (``result_usage``, ``twin_group``, ``confidence``,
``observed``, ``injection``). Pydantic's default ``extra="ignore"`` silently
drops any key the model does not declare, so this test is the drift guard the
v3 review asked for (finding 16): if the Go protocol grows a field, regenerate
the fixture and this test fails until the model declares it. The ditto-platform
copy of these models (the source of truth for this repo's hand-maintained
mirror) carries the same fixture and test, and the validator contract golden
guards the platform<->subnet structural equality.

Regenerate the fixture from a dittobench-datagen checkout with a small program
that marshals a fully-populated ``protocol.ScoreReport`` (see the PR that added
this file), or copy the emitted report of any bench_version 3 run.
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ditto.api_models.validator import ScoreReport

FIXTURE = Path(__file__).parent / "fixtures" / "score_report_v3.json"

# Wire keys the Go engine emits whose absence from the parsed model would mean
# silent data loss. ``details`` is deliberately opaque (dict), so its inner
# keys always survive and are asserted verbatim instead.
V3_CASE_AUDIT_FIELDS = {
    "result_usage",
    "twin_group",
    "confidence",
    "observed",
    "injection",
}


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text())


def test_no_wire_key_is_silently_dropped() -> None:
    raw = _fixture()
    report = ScoreReport.model_validate(raw)

    unknown_top = set(raw) - set(ScoreReport.model_fields)
    assert not unknown_top, (
        f"ScoreReport silently drops wire keys: {sorted(unknown_top)}"
    )

    case_fields = set(type(report.per_case[0]).model_fields)
    for case in raw["per_case"]:
        unknown = set(case) - case_fields
        assert not unknown, f"CaseScore silently drops wire keys: {sorted(unknown)}"

    assert report.structural_fingerprint is not None
    fp_fields = set(type(report.structural_fingerprint).model_fields)
    unknown_fp = set(raw["structural_fingerprint"]) - fp_fields
    assert not unknown_fp, (
        f"CodeFingerprint silently drops wire keys: {sorted(unknown_fp)}"
    )

    assert report.per_category is not None
    cat_fields = set(type(report.per_category[0]).model_fields)
    for stat in raw["per_category"]:
        unknown = set(stat) - cat_fields
        assert not unknown, f"CategoryStat silently drops wire keys: {sorted(unknown)}"


def test_v3_audit_fields_round_trip() -> None:
    report = ScoreReport.model_validate(_fixture())
    assert set(type(report.per_case[0]).model_fields) >= V3_CASE_AUDIT_FIELDS

    observed_tool = report.per_case[0]
    assert observed_tool.result_usage == 1.0
    assert observed_tool.observed is True
    assert observed_tool.confidence == 0.9
    assert observed_tool.injection is False

    memory = report.per_case[1]
    assert memory.twin_group == "twin-7f3a"
    assert memory.confidence == 0.75
    # Go omits `expected` (null) on memory cases; ingest coerces to [].
    assert memory.expected == []

    bait = report.per_case[2]
    assert bait.injection is True
    assert bait.observed is True
    # Confidence is a pointer on the wire: not-reported must stay None, not 0.0.
    assert bait.confidence is None


def test_report_round_trips_by_value() -> None:
    raw = _fixture()
    report = ScoreReport.model_validate(raw)
    dumped = report.model_dump(mode="json")

    # Every wire value survives ingest and re-serialization. Compare per-key so
    # a failure names the lost field; skip model-side defaults for keys the Go
    # engine omitted (omitempty) and the null-coerced list fields.
    for i, case in enumerate(raw["per_case"]):
        for key, value in case.items():
            if value is None:
                continue
            assert dumped["per_case"][i][key] == value, f"per_case[{i}].{key} mutated"

    for i, stat in enumerate(raw["per_category"]):
        for key, value in stat.items():
            assert dumped["per_category"][i][key] == value, (
                f"per_category[{i}].{key} mutated"
            )
    for key, value in raw["structural_fingerprint"].items():
        assert dumped["structural_fingerprint"][key] == value, (
            f"structural_fingerprint.{key} mutated"
        )
    # The opaque details blob is preserved verbatim.
    assert dumped["details"] == raw["details"]
    assert report.composite == raw["composite"]
    assert report.seed == raw["seed"]


def test_every_benchmark_version_rejects_composite_above_one() -> None:
    raw = _fixture()
    raw.update({"bench_version": 5, "composite": 0.855})
    assert ScoreReport.model_validate(raw).composite == 0.855

    for bench_version in (4, 5, 6):
        raw.update({"bench_version": bench_version, "composite": 1.001})
        with pytest.raises(ValidationError):
            ScoreReport.model_validate(raw)

    raw.update({"bench_version": 5, "composite": float("inf")})
    with pytest.raises(ValidationError):
        ScoreReport.model_validate(raw)
