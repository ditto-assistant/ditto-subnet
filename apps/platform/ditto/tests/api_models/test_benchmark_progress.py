"""Contract tests for privacy-safe signed benchmark progress."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ditto.api_models.benchmark_progress import (
    BenchmarkProgress,
    benchmark_progress_signing_token,
)

_DEADLINE = datetime(2030, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    ("stage", "completed", "total"),
    [
        ("preparing", None, None),
        ("building_harness", None, None),
        ("starting_harness", None, None),
        ("running_benchmark", None, None),
        ("running_benchmark", 51, 114),
        ("waiting_for_relay", 51, 114),
        ("finalizing", 114, 114),
        ("submitting_result", 114, 114),
        ("failed_retrying", None, None),
        ("failed_retrying", 51, 114),
    ],
)
def test_accepts_every_allowlisted_stage(
    stage: str, completed: int | None, total: int | None
) -> None:
    progress = BenchmarkProgress(
        stage=stage,  # type: ignore[arg-type]
        completed=completed,
        total=total,
        ticket_deadline=_DEADLINE,
    )
    assert progress.stage == stage


@pytest.mark.parametrize(
    "payload",
    [
        {"stage": "unknown"},
        {"stage": "running_benchmark", "completed": 1},
        {"stage": "running_benchmark", "total": 10},
        {"stage": "running_benchmark", "completed": 11, "total": 10},
        {"stage": "running_benchmark", "completed": 1, "total": 10_001},
        {"stage": "running_benchmark", "completed": -1, "total": 10},
        {"stage": "running_benchmark", "completed": 1.0, "total": 10},
        {"stage": "running_benchmark", "completed": float("nan"), "total": 10},
        {"stage": "running_benchmark", "completed": float("inf"), "total": 10},
        {"stage": "preparing", "completed": 0, "total": 10},
        {"stage": "finalizing", "completed": 9, "total": 10},
        {"stage": "submitting_result"},
    ],
)
def test_rejects_malformed_or_malicious_progress(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        BenchmarkProgress.model_validate({**payload, "ticket_deadline": _DEADLINE})


def test_ignores_additive_progress_fields() -> None:
    progress = BenchmarkProgress.model_validate(
        {
            "stage": "running_benchmark",
            "completed": 1,
            "total": 10,
            "ticket_deadline": _DEADLINE,
            "display": "private text",
        }
    )

    assert "display" not in progress.model_dump()


def test_requires_timezone_and_canonicalizes_signing_token() -> None:
    with pytest.raises(ValidationError):
        BenchmarkProgress(
            stage="preparing",
            ticket_deadline=datetime(2030, 1, 1),
        )
    progress = BenchmarkProgress(
        stage="running_benchmark",
        completed=51,
        total=114,
        ticket_deadline=_DEADLINE,
    )
    assert benchmark_progress_signing_token(progress) == (
        "running_benchmark,51,114,2030-01-01T00:00:00.000000+00:00"
    )
    assert benchmark_progress_signing_token(None) == "-"


def test_run_token_is_optional_and_backward_compatible() -> None:
    """A None run_token must yield the exact pre-token signing bytes."""
    without_token = BenchmarkProgress(
        stage="running_benchmark",
        completed=51,
        total=114,
        ticket_deadline=_DEADLINE,
    )
    assert without_token.run_token is None
    # Byte-identical to the historical (pre-run_token) format so every existing
    # v4+ heartbeat signature still verifies.
    assert benchmark_progress_signing_token(without_token) == (
        "running_benchmark,51,114,2030-01-01T00:00:00.000000+00:00"
    )

    with_token = BenchmarkProgress(
        stage="running_benchmark",
        completed=51,
        total=114,
        ticket_deadline=_DEADLINE,
        run_token="0123456789abcdef",
    )
    assert benchmark_progress_signing_token(with_token) == (
        "running_benchmark,51,114,2030-01-01T00:00:00.000000+00:00,0123456789abcdef"
    )


@pytest.mark.parametrize(
    "run_token",
    ["", "XYZ", "abc", "g" * 8, "a" * 65, "ABCDEF01"],
)
def test_rejects_malformed_run_token(run_token: str) -> None:
    with pytest.raises(ValidationError):
        BenchmarkProgress(
            stage="running_benchmark",
            completed=51,
            total=114,
            ticket_deadline=_DEADLINE,
            run_token=run_token,
        )
