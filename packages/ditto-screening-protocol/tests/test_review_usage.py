"""Bounds of the per-attempt review usage telemetry model."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from ditto_screening_protocol import ReviewUsageStageEntry, ScreeningReviewUsageRequest


def _request(**overrides: object) -> ScreeningReviewUsageRequest:
    payload: dict[str, object] = {
        "attempt_id": str(uuid4()),
        "artifact_sha256": "ab" * 32,
        "stages": [
            {
                "stage": "l1",
                "model": "openai/gpt-6-luna",
                "requests": 3,
                "input_tokens": 1000,
                "cached_input_tokens": 400,
                "output_tokens": 50,
                "estimated_cost_usd": 0.2,
                "reported_cost_usd": 0.1,
            },
            {
                "stage": "l2_analyst",
                "model": "openai/gpt-6-sol",
                "input_tokens": 500,
                "output_tokens": 20,
                "estimated_cost_usd": 0.3,
            },
        ],
        **overrides,
    }
    return ScreeningReviewUsageRequest.model_validate(payload)


def test_totals_prefer_reported_cost() -> None:
    request = _request()
    assert request.total_input_tokens == 1500
    assert request.total_output_tokens == 70
    assert request.total_cost_usd == pytest.approx(0.4)


@pytest.mark.parametrize(
    "overrides",
    [
        {"stages": []},
        {"artifact_sha256": "AB" * 32},
        {"lineage_scope": "partial"},
        {"elapsed_ms": -1},
        {"stages": [{"stage": "l9", "input_tokens": 1}]},
        {"stages": [{"stage": "l1", "input_tokens": 1, "cached_input_tokens": 2}]},
        {"stages": [{"stage": "l1", "input_tokens": 10**12}]},
        {"stages": [{"stage": "l1", "estimated_cost_usd": 1e6}]},
        {"stages": [{"stage": "l1", "model": "has space"}]},
        {"stages": [{"stage": "l1", "prompt": "never carried"}]},
        {"stages": [{"stage": "l1"}, {"stage": "l1"}]},
        {"extra_field": 1},
    ],
)
def test_out_of_bounds_reports_are_refused(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _request(**overrides)


def test_same_stage_on_two_models_is_allowed() -> None:
    request = _request(
        stages=[
            {"stage": "l2_analyst", "model": "openai/gpt-6-sol", "input_tokens": 1},
            {"stage": "l2_analyst", "model": "z-ai/glm-5.2", "input_tokens": 2},
        ]
    )
    assert [entry.model for entry in request.stages] == [
        "openai/gpt-6-sol",
        "z-ai/glm-5.2",
    ]
    assert ReviewUsageStageEntry(stage="l1").cost_usd == 0.0
