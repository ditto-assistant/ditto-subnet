"""Strict privacy boundary for signed benchmark progress."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ditto.api_models.benchmark_progress import (
    BenchmarkProgress,
    benchmark_progress_signing_token,
)
from ditto.api_models.validator import ValidatorHeartbeatRequest

_DEADLINE = datetime(2026, 7, 14, 12, 30, tzinfo=UTC)
_HOTKEY = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
_AGENT = "550e8400-e29b-41d4-a716-446655440000"


@pytest.mark.parametrize(
    ("stage", "completed", "total"),
    [
        ("preparing", None, None),
        ("building_harness", None, None),
        ("generating_dataset", None, None),
        ("starting_harness", None, None),
        ("running_benchmark", None, None),
        ("running_benchmark", 51, 114),
        ("finalizing", 114, 114),
        ("submitting_result", 114, 114),
        ("failed_retrying", None, None),
        ("failed_retrying", 51, 114),
    ],
)
def test_all_allowed_progress_shapes(
    stage: str, completed: int | None, total: int | None
) -> None:
    progress = BenchmarkProgress.model_validate(
        {
            "stage": stage,
            "completed": completed,
            "total": total,
            "ticket_deadline": _DEADLINE,
        },
        strict=True,
    )
    assert progress.stage == stage


def test_json_wire_deadline_parses_and_normalizes_to_utc() -> None:
    progress = BenchmarkProgress.model_validate(
        {
            "stage": "running_benchmark",
            "completed": 51,
            "total": 114,
            "ticket_deadline": "2026-07-14T08:30:00-04:00",
        }
    )
    assert progress.ticket_deadline == _DEADLINE


@pytest.mark.parametrize(
    "payload",
    [
        {"stage": "case_42", "ticket_deadline": _DEADLINE},
        {"stage": "running_benchmark", "completed": 1, "ticket_deadline": _DEADLINE},
        {"stage": "running_benchmark", "total": 114, "ticket_deadline": _DEADLINE},
        {
            "stage": "running_benchmark",
            "completed": 115,
            "total": 114,
            "ticket_deadline": _DEADLINE,
        },
        {
            "stage": "running_benchmark",
            "completed": 1,
            "total": 10_001,
            "ticket_deadline": _DEADLINE,
        },
        {
            "stage": "preparing",
            "completed": 0,
            "total": 114,
            "ticket_deadline": _DEADLINE,
        },
        {
            "stage": "finalizing",
            "completed": 113,
            "total": 114,
            "ticket_deadline": _DEADLINE,
        },
        {
            "stage": "submitting_result",
            "ticket_deadline": _DEADLINE,
        },
        {
            "stage": "running_benchmark",
            "completed": True,
            "total": 114,
            "ticket_deadline": _DEADLINE,
        },
        {
            "stage": "running_benchmark",
            "completed": "51",
            "total": 114,
            "ticket_deadline": _DEADLINE,
        },
        {
            "stage": "running_benchmark",
            "completed": float("nan"),
            "total": 114,
            "ticket_deadline": _DEADLINE,
        },
        {
            "stage": "running_benchmark",
            "completed": 1,
            "total": float("inf"),
            "ticket_deadline": _DEADLINE,
        },
        {
            "stage": "running_benchmark",
            "ticket_deadline": _DEADLINE,
            "error": "private failure body",
        },
        {
            "stage": "running_benchmark",
            "ticket_deadline": datetime(2026, 7, 14, 12, 30),
        },
    ],
)
def test_malformed_or_malicious_progress_is_rejected(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        BenchmarkProgress.model_validate(payload)


def test_signing_token_contains_only_allowlisted_fields() -> None:
    progress = BenchmarkProgress(
        stage="running_benchmark",
        completed=51,
        total=114,
        ticket_deadline=_DEADLINE,
    )
    assert benchmark_progress_signing_token(progress) == (
        "running_benchmark,51,114,2026-07-14T12:30:00.000000+00:00"
    )
    assert benchmark_progress_signing_token(None) == "-"


def test_signing_token_omits_absent_run_token_byte_for_byte() -> None:
    # Backward-compat guard: a progress with no run_token must produce EXACTLY
    # the pre-run_token v4 token, or every existing heartbeat signature breaks.
    progress = BenchmarkProgress(
        stage="running_benchmark",
        completed=51,
        total=114,
        ticket_deadline=_DEADLINE,
    )
    assert progress.run_token is None
    assert benchmark_progress_signing_token(progress) == (
        "running_benchmark,51,114,2026-07-14T12:30:00.000000+00:00"
    )


def test_signing_token_appends_present_run_token() -> None:
    progress = BenchmarkProgress(
        stage="running_benchmark",
        completed=51,
        total=114,
        ticket_deadline=_DEADLINE,
        run_token="0123456789abcdef",
    )
    assert benchmark_progress_signing_token(progress) == (
        "running_benchmark,51,114,2026-07-14T12:30:00.000000+00:00,0123456789abcdef"
    )


@pytest.mark.parametrize("bad", ["", "XYZ", "0123", "g" * 16, "AB" * 8])
def test_run_token_rejects_non_hex_or_out_of_range(bad: str) -> None:
    with pytest.raises(ValidationError):
        BenchmarkProgress(
            stage="running_benchmark",
            completed=51,
            total=114,
            ticket_deadline=_DEADLINE,
            run_token=bad,
        )


def _heartbeat(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "validator_hotkey": _HOTKEY,
        "software_version": "0.4.2",
        "protocol_version": 4,
        "code_digest": "ab" * 32,
        "state": "running_benchmark",
        "active_agent_id": _AGENT,
        "timestamp": 1_752_443_200,
        "signature": "cd" * 64,
    }
    payload.update(updates)
    return payload


def test_v4_active_heartbeat_may_omit_progress_for_unknown_compatibility() -> None:
    heartbeat = ValidatorHeartbeatRequest.model_validate(_heartbeat())
    assert heartbeat.active_agent_id is not None
    assert heartbeat.benchmark_progress is None


def test_progress_requires_v4_active_running_context() -> None:
    progress = BenchmarkProgress(
        stage="running_benchmark",
        completed=51,
        total=114,
        ticket_deadline=_DEADLINE,
    )
    for updates in (
        {"protocol_version": 3, "benchmark_progress": progress},
        {"active_agent_id": None, "benchmark_progress": progress},
        {"state": "idle", "benchmark_progress": progress},
    ):
        with pytest.raises(ValidationError):
            ValidatorHeartbeatRequest.model_validate(_heartbeat(**updates))
