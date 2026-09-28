"""Exact replay binding must survive signatures, retries, and worker changes."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from ditto_screening_protocol.v13_replay_observation import (
    V13ReplayBinding,
    V13ReplayObservation,
    authentic_replay_observation,
)


def _fixture() -> tuple[V13ReplayBinding, V13ReplayObservation, datetime]:
    start = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    binding = V13ReplayBinding(
        replay_id=uuid4(),
        agent_id=uuid4(),
        attempt_id=uuid4(),
        artifact_sha256="a" * 64,
        image_sha256="b" * 64,
        image_id="sha256:" + "c" * 64,
    )
    observation = V13ReplayObservation(
        binding=binding,
        check_code="health",
        status="passed",
        evidence_sha256="d" * 64,
        runner_hotkey="independent-runner",
        observed_at=start + timedelta(minutes=1),
        signature="e" * 128,
    )
    observation = observation.model_copy(
        update={
            "signature": hashlib.sha512(
                b"test-only-key:" + observation.signing_message()
            ).hexdigest()
        }
    )
    return binding, observation, start


def _accepted(
    observation: V13ReplayObservation,
    binding: V13ReplayBinding,
    start: datetime,
) -> bool:
    return authentic_replay_observation(
        observation,
        expected=binding,
        enrolled_runner_hotkey="independent-runner",
        source_worker_hotkey="original-worker",
        lease_started_at=start,
        lease_deadline=start + timedelta(minutes=30),
        verify_signature=lambda hotkey, message, signature: (
            hotkey == "independent-runner"
            and signature == hashlib.sha512(b"test-only-key:" + message).hexdigest()
        ),
    )


def test_signed_replay_observation_is_bound_to_exact_attempt_and_image() -> None:
    binding, observation, start = _fixture()
    assert _accepted(observation, binding, start)
    assert observation.signing_message().startswith(b"ditto-v13-replay-observation:v1:")
    assert b"signature" not in observation.signing_message()
    for change in (
        {"replay_id": uuid4()},
        {"agent_id": uuid4()},
        {"attempt_id": uuid4()},
        {"artifact_sha256": "f" * 64},
        {"image_sha256": "f" * 64},
        {"image_id": "sha256:" + "f" * 64},
    ):
        assert not _accepted(observation, binding.model_copy(update=change), start)


def test_signature_covers_check_status_evidence_and_time() -> None:
    binding, observation, start = _fixture()
    signed = observation.signing_message()
    for change in (
        {"check_code": "tool_selection_run"},
        {"status": "failed"},
        {"evidence_sha256": "f" * 64},
        {"observed_at": start + timedelta(minutes=2)},
    ):
        changed = observation.model_copy(update=change)
        assert changed.signing_message() != signed
        assert not _accepted(changed, binding, start)


def test_replay_observation_rejects_self_review_and_out_of_lease_time() -> None:
    binding, observation, start = _fixture()
    assert not authentic_replay_observation(
        observation,
        expected=binding,
        enrolled_runner_hotkey="independent-runner",
        source_worker_hotkey="independent-runner",
        lease_started_at=start,
        lease_deadline=start + timedelta(minutes=30),
        verify_signature=lambda *_: True,
    )
    assert not _accepted(
        observation.model_copy(update={"observed_at": start + timedelta(minutes=31)}),
        binding,
        start,
    )
    assert not _accepted(
        observation.model_copy(update={"observed_at": start.replace(tzinfo=None)}),
        binding,
        start,
    )
