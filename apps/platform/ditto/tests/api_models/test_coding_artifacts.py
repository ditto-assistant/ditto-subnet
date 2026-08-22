"""Cross-language contract tests for coding artifact delivery."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from ditto.api_models.coding_artifacts import (
    CODING_ARTIFACT_AUDIENCE,
    CODING_ARTIFACT_MAX_BYTES,
    CODING_ARTIFACT_PHASES,
    CodingArtifactCapabilityEnvelope,
    CodingArtifactDeliveryPhase,
    CodingArtifactKind,
    parse_coding_artifact_capability_json,
)

_VECTOR_PATH = (
    Path(__file__).parents[5]
    / "packages"
    / "dittobench-coding-contract"
    / "testdata"
    / "coding_artifact_capability_v1.json"
)


def _vectors() -> dict:
    vectors = json.loads(_VECTOR_PATH.read_text(encoding="utf-8"))
    assert vectors["schema"] == "dittobench-coding-artifact-capability-vector-v1"
    assert vectors["coding_contract_version"] == 1
    assert vectors["weight_eligible"] is False
    return vectors


def test_python_accepts_every_shared_capability_vector() -> None:
    vectors = _vectors()
    observed: set[tuple[str, str]] = set()
    for raw in vectors["capabilities"]:
        capability = parse_coding_artifact_capability_json(json.dumps(raw))
        observed.add((capability.delivery_phase.value, capability.artifact_kind.value))
        assert "synthetic-" not in repr(capability)
        assert capability.model_dump(mode="json", by_alias=True)["url"] == raw["url"]
        extended = {**raw, "future_transport_hint": {"ignored": True}}
        parsed = parse_coding_artifact_capability_json(json.dumps(extended))
        assert "future_transport_hint" not in parsed.model_fields_set

    assert observed == {
        ("authoring", "visible-bundle"),
        ("authoring", "memory-bundle"),
        ("authoring", "resource-profile"),
        ("grading", "visible-bundle"),
        ("grading", "resource-profile"),
        ("grading", "grader-bundle"),
    }


def test_python_policies_match_shared_vector() -> None:
    policies = _vectors()["policies"]
    assert len(policies) == 4
    seen: set[CodingArtifactKind] = set()
    for policy in policies:
        kind = CodingArtifactKind(policy["artifact_kind"])
        assert kind not in seen
        seen.add(kind)
        assert CODING_ARTIFACT_AUDIENCE[kind].value == policy["audience"]
        assert CODING_ARTIFACT_MAX_BYTES[kind] == policy["maximum_size_bytes"]
        assert sorted(phase.value for phase in CODING_ARTIFACT_PHASES[kind]) == sorted(
            policy["delivery_phases"]
        )
    assert seen == set(CodingArtifactKind)


@pytest.mark.parametrize(
    ("label", "mutate", "match"),
    [
        (
            "weighted",
            lambda value: value.update(weight_eligible=True),
            "False",
        ),
        (
            "boolean contract version",
            lambda value: value.update(coding_contract_version=True),
            "integer",
        ),
        (
            "numeric weight flag",
            lambda value: value.update(weight_eligible=0),
            "boolean",
        ),
        (
            "zero ticket",
            lambda value: value.update(
                ticket_id="00000000-0000-0000-0000-000000000000"
            ),
            "nonzero",
        ),
        (
            "audience",
            lambda value: value.update(audience="protected-grader"),
            "audience",
        ),
        (
            "grader during authoring",
            lambda value: value.update(
                artifact_kind="grader-bundle",
                audience="protected-grader",
            ),
            "phase",
        ),
        (
            "oversized",
            lambda value: value.update(size_bytes=(2 << 30) + 1),
            "size",
        ),
        (
            "non-integer size",
            lambda value: value.update(size_bytes=1024.0),
            "integer",
        ),
        (
            "expiry",
            lambda value: value.update(expires_at="2026-08-21T13:00:01Z"),
            "outlives",
        ),
        (
            "path",
            lambda value: value.update(
                url=value["url"].replace("visible-bundle", "memory-bundle", 1)
            ),
            "path",
        ),
        (
            "query escaping",
            lambda value: value.update(url=value["url"] + "&future=%ZZ"),
            "URL",
        ),
        (
            "numeric deadline",
            lambda value: value.update(ticket_deadline=1_787_317_200),
            "strings or datetimes",
        ),
        (
            "date-only expiry",
            lambda value: value.update(expires_at="2026-08-21"),
            "RFC3339",
        ),
        (
            "nanosecond deadline",
            lambda value: value.update(
                ticket_deadline="2026-08-21T13:00:00.123456789Z"
            ),
            "RFC3339",
        ),
        (
            "signed duration",
            lambda value: value.update(
                url=value["url"].replace("X-Amz-Expires=300", "X-Amz-Expires=%2B300")
            ),
            "expiry",
        ),
    ],
)
def test_python_rejects_contract_drift(label: str, mutate, match: str) -> None:
    del label
    raw = deepcopy(_vectors()["capabilities"][0])
    mutate(raw)
    with pytest.raises((ValueError, ValidationError), match=match):
        CodingArtifactCapabilityEnvelope.model_validate(raw)


def test_python_rejects_memory_during_grading() -> None:
    raw = deepcopy(_vectors()["capabilities"][1])
    raw["delivery_phase"] = CodingArtifactDeliveryPhase.GRADING.value
    with pytest.raises(ValidationError, match="phase"):
        CodingArtifactCapabilityEnvelope.model_validate(raw)


def test_raw_parser_rejects_duplicate_invalid_and_oversized_json() -> None:
    raw = json.dumps(_vectors()["capabilities"][0])
    duplicate = raw.replace(
        '"coding_contract_version": 1,',
        '"coding_contract_version": 1, "coding_contract_version": 1,',
        1,
    )
    with pytest.raises(ValueError, match="repeats field"):
        parse_coding_artifact_capability_json(duplicate)
    with pytest.raises(ValueError, match="surrogate"):
        parse_coding_artifact_capability_json(raw[:-1] + ', "future": "\\ud800"}')
    with pytest.raises(ValueError, match="size"):
        parse_coding_artifact_capability_json(b" " * ((32 << 10) + 1))
    with pytest.raises(ValueError, match="nesting"):
        parse_coding_artifact_capability_json("[" * 40 + "0" + "]" * 40)


def test_raw_parser_redacts_invalid_bearer_url() -> None:
    raw = deepcopy(_vectors()["capabilities"][0])
    raw["url"] = raw["url"].replace("visible-bundle", "memory-bundle", 1)
    with pytest.raises(ValueError, match="known fields") as captured:
        parse_coding_artifact_capability_json(json.dumps(raw))
    assert "synthetic-visible-authoring" not in str(captured.value)
    assert raw["url"] not in str(captured.value)
