"""Reviewer values of the wrong JSON type are contract misses, not crashes.

ditto-subnet#2987: reviewer tool schemas are not strict, so a model can send a
list or dict where a string is expected. A set-membership test then raised
``TypeError: unhashable type``, which no review path caught, so the review was
parked as ``unexpected-infrastructure`` and its notes were lost.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

import ditto_screener.adjudicator as adjudicator_module
import ditto_screener.source_review as source_review_module
from ditto_screener.errors import model_output_contract

_UNHASHABLE = [["concern"], {"kind": "concern"}]


@pytest.mark.parametrize("kind", _UNHASHABLE)
def test_l1_note_with_unhashable_kind_is_dropped(kind: object) -> None:
    note = source_review_module._note_from_arguments(
        {"kind": kind, "summary": "reads the generator's record sentences"}
    )

    assert note is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("risk_level", ["high"]),
        ("categories", [{"category": "none"}]),
        ("categories", [["benchmark_emulation"]]),
    ],
)
def test_l1_review_with_unhashable_field_is_a_contract_miss(
    field: str, value: object
) -> None:
    review: dict[str, object] = {
        "risk_level": "low",
        "confidence": 0.5,
        "categories": ["none"],
        "evidence": [],
        "invariants": [],
        "summary": "nothing of note",
    }
    review[field] = value

    with pytest.raises(ValueError, match="source review fields are invalid"):
        source_review_module._parse_l1_review(
            review,
            artifact_sha256="0" * 64,
            repository=cast(Any, None),
        )


@pytest.mark.parametrize("decision", [["clear"], {"decision": "clear"}])
def test_l4_verdict_with_unhashable_decision_is_a_contract_miss(
    decision: object,
) -> None:
    with pytest.raises(ValueError, match="adjudicator decision is invalid"):
        adjudicator_module._verdict_from({"decision": decision, "reason": "fine"})


def test_contract_wrapper_converts_only_type_errors() -> None:
    @model_output_contract("value is invalid")
    def parse(value: object) -> bool:
        if value == "boom":
            raise RuntimeError("not a contract miss")
        return value in {"a", "b"}

    assert parse("a") is True
    with pytest.raises(ValueError, match="value is invalid") as error:
        parse(["a"])
    assert isinstance(error.value.__cause__, TypeError)
    with pytest.raises(RuntimeError):
        parse("boom")
    assert parse.__name__ == "parse"
