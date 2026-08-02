"""Wire-shape tests for the validator ScoreReport / CaseScore models.

Focus: the per-case breakdown must round-trip the DittoBench Go scorer's
``CaseScore`` for *both* case families — in particular a memory case (where the
signal is ``kind=memory`` + ``correct`` + ``score``, not ``tool_score``), and
the Go scorer's null/absent list fields.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from ditto.api_models.validator import ArtifactResponse, CaseScore, ScoreReport


def _artifact_response(**overrides: object) -> ArtifactResponse:
    values = {
        "agent_id": uuid4(),
        "sha256": "ab" * 32,
        "download_url": "https://objects.example/agent.tar.gz",
        "expires_at": datetime.now(UTC),
        **overrides,
    }
    return ArtifactResponse.model_validate(values)


def test_artifact_response_accepts_legacy_all_none_image_metadata() -> None:
    response = _artifact_response()

    assert response.screened_image_url is None
    assert response.screened_image_sha256 is None
    assert response.screened_image_size_bytes is None
    assert response.screened_image_id is None
    assert response.screened_image_ref is None


def test_artifact_response_accepts_complete_screened_image_metadata() -> None:
    response = _artifact_response(
        screened_image_url="https://objects.example/screened-image.tar",
        screened_image_sha256="12" * 32,
        screened_image_size_bytes=123,
        screened_image_id="sha256:" + "34" * 32,
        screened_image_ref="ditto-screen/agent:latest",
    )

    assert response.screened_image_size_bytes == 123


@pytest.mark.parametrize(
    "only_field",
    [
        {"screened_image_url": "https://objects.example/screened-image.tar"},
        {"screened_image_sha256": "12" * 32},
        {"screened_image_size_bytes": 123},
        {"screened_image_id": "sha256:" + "34" * 32},
        {"screened_image_ref": "ditto-screen/agent:latest"},
    ],
)
def test_artifact_response_rejects_partial_screened_image_metadata(
    only_field: dict[str, object],
) -> None:
    with pytest.raises(
        ValidationError, match="screened image metadata must be complete"
    ):
        _artifact_response(**only_field)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("screened_image_url", ""),
        ("screened_image_sha256", ""),
        ("screened_image_id", ""),
        ("screened_image_ref", ""),
    ],
)
def test_artifact_response_rejects_empty_screened_image_strings(
    field: str, value: str
) -> None:
    complete = {
        "screened_image_url": "https://objects.example/screened-image.tar",
        "screened_image_sha256": "12" * 32,
        "screened_image_size_bytes": 123,
        "screened_image_id": "sha256:" + "34" * 32,
        "screened_image_ref": "ditto-screen/agent:latest",
    }
    complete[field] = value

    with pytest.raises(ValidationError):
        _artifact_response(**complete)


def test_tool_case_round_trips() -> None:
    cs = CaseScore.model_validate(
        {
            "case_id": "web_search-1-0001",
            "category": "web_search",
            "kind": "tool",
            "score": 0.9,
            "tool_score": 1.0,
            "quality": 0.8,
            "latency_ms": 42,
            "called": ["search_web"],
            "expected": ["search_web"],
        }
    )
    assert cs.kind == "tool"
    assert cs.score == 0.9
    assert cs.correct is False  # default, absent on tool cases


def test_memory_case_preserves_signal() -> None:
    # As the Go scorer emits a memory case: kind=memory, correct verdict, the
    # meaningful number in `score`, tool_score 0, and expected as [].
    cs = CaseScore.model_validate(
        {
            "case_id": "m-0003",
            "category": "multi-session",
            "kind": "memory",
            "score": 1.0,
            "tool_score": 0.0,
            "correct": True,
            "latency_ms": 20,
            "called": [],
            "expected": [],
        }
    )
    assert cs.kind == "memory"
    assert cs.correct is True
    assert cs.score == 1.0  # not lost despite tool_score == 0


def test_null_list_fields_coerced_to_empty() -> None:
    # A nil Go slice serializes to JSON null; the model must accept it.
    cs = CaseScore.model_validate(
        {
            "case_id": "m-0004",
            "category": "memory",
            "kind": "memory",
            "score": 0.0,
            "tool_score": 0.0,
            "latency_ms": 5,
            "called": None,
            "expected": None,
            "notes": None,
        }
    )
    assert cs.called == []
    assert cs.expected == []
    assert cs.notes == []


def test_scorereport_with_mixed_per_case() -> None:
    report = ScoreReport.model_validate(
        {
            "run_id": "r1",
            "seed": 8675309,
            "composite": 0.6,  # 0.6*1.0 + 0.4*0.0
            "tool_mean": 1.0,
            "memory_mean": 0.0,
            "median_ms": 30,
            "n": 2,
            "generated_at": "2026-06-30T00:00:00Z",
            "per_case": [
                {
                    "case_id": "t1",
                    "category": "web_search",
                    "kind": "tool",
                    "score": 1.0,
                    "tool_score": 1.0,
                    "quality": 1.0,
                    "latency_ms": 10,
                    "called": ["search_web"],
                    "expected": ["search_web"],
                },
                {
                    "case_id": "m1",
                    "category": "multi-session",
                    "kind": "memory",
                    "score": 0.0,
                    "tool_score": 0.0,
                    "correct": False,
                    "latency_ms": 50,
                    "called": [],
                    "expected": [],
                },
            ],
        }
    )
    assert report.seed == 8675309
    assert [c.kind for c in report.per_case] == ["tool", "memory"]


def test_scorereport_rejects_above_one_for_every_benchmark_version() -> None:
    base = {
        "run_id": "r-v5",
        "seed": 42,
        "composite": 0.855,
        "tool_mean": 0.9,
        "memory_mean": 0.9,
        "median_ms": 30,
        "n": 2,
        "generated_at": "2026-07-20T00:00:00Z",
        "per_case": [],
    }
    report = ScoreReport.model_validate({**base, "bench_version": 5})
    assert report.composite == 0.855

    with pytest.raises(ValidationError):
        ScoreReport.model_validate({**base, "bench_version": 5, "composite": 1.001})
