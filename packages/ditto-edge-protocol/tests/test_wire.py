from __future__ import annotations

import pytest
from pydantic import ValidationError

from ditto_edge_protocol.limits import (
    MAX_ACK_ITEMS,
    MAX_CHALLENGES_PER_POLL,
    MAX_OBJECT_BYTES_CEILING,
    MAX_POSSESSION_RANGE,
    MAX_RETRIEVAL_RANGE,
)
from ditto_edge_protocol.wire import (
    AckRequest,
    Assignment,
    Challenge,
    ChallengeAnswer,
    EnrollRequest,
    LaneLimits,
    NodeStatusResponse,
    PollResponse,
)

DIGEST = "ab" * 32


def test_assignment_accepts_known_media_types_only() -> None:
    Assignment(digest=DIGEST, size_bytes=10, media_type="text/html", lease_expires_at=1)
    with pytest.raises(ValidationError):
        Assignment(
            digest=DIGEST,
            size_bytes=10,
            media_type="application/x-sh",
            lease_expires_at=1,
        )


@pytest.mark.parametrize("digest", ["AB" * 32, "ab" * 31, "g" * 64, ""])
def test_digests_are_lowercase_sha256_hex(digest: str) -> None:
    with pytest.raises(ValidationError):
        Assignment(
            digest=digest, size_bytes=10, media_type="text/plain", lease_expires_at=1
        )


def test_object_size_is_bounded() -> None:
    with pytest.raises(ValidationError):
        Assignment(
            digest=DIGEST,
            size_bytes=MAX_OBJECT_BYTES_CEILING + 1,
            media_type="text/plain",
            lease_expires_at=1,
        )


def test_unknown_fields_are_ignored_for_forward_compatibility() -> None:
    request = EnrollRequest.model_validate(
        {
            "protocol": "edge/v1",
            "public_key_hex": "cd" * 32,
            "quota_bytes": 1,
            "client": {"name": "n", "version": "1", "future": True},
            "future_field": [1, 2],
        }
    )
    assert request.platform.os == ""
    assert request.hotkey_binding is None


def test_lane_modes_never_include_enforce() -> None:
    with pytest.raises(ValidationError):
        LaneLimits(
            mode="enforce",  # type: ignore[arg-type]
            poll_interval_seconds=30,
            lease_seconds=900,
            max_object_bytes=10,
            node_quota_cap_bytes=10,
            challenge_deadline_seconds=60,
        )


def test_ack_lists_are_bounded() -> None:
    with pytest.raises(ValidationError):
        AckRequest(held=[DIGEST] * (MAX_ACK_ITEMS + 1))


def test_challenge_ids_and_nonces_are_strict_hex() -> None:
    Challenge(
        challenge_id="0f" * 16,
        kind="possession",
        digest=DIGEST,
        nonce_hex="aa" * 32,
        offset=0,
        length=1,
        deadline_at=1,
    )
    with pytest.raises(ValidationError):
        Challenge(
            challenge_id="not-hex",
            kind="possession",
            digest=DIGEST,
            nonce_hex="aa" * 32,
            offset=0,
            length=1,
            deadline_at=1,
        )


def _challenge(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "challenge_id": "0f" * 16,
        "kind": "possession",
        "digest": DIGEST,
        "nonce_hex": "aa" * 32,
        "offset": 0,
        "length": 1,
        "deadline_at": 1,
    }
    base.update(overrides)
    return base


def test_possession_challenges_need_a_nonce_and_a_provable_range() -> None:
    with pytest.raises(ValidationError, match="nonce"):
        Challenge.model_validate(_challenge(nonce_hex=None))
    with pytest.raises(ValidationError, match="possession range"):
        Challenge.model_validate(_challenge(length=MAX_POSSESSION_RANGE + 1))
    Challenge.model_validate(_challenge(length=MAX_POSSESSION_RANGE))
    Challenge.model_validate(
        _challenge(kind="retrieval", nonce_hex=None, length=MAX_RETRIEVAL_RANGE)
    )


def test_poll_responses_bound_their_challenges() -> None:
    limits = {
        "mode": "shadow",
        "poll_interval_seconds": 30,
        "lease_seconds": 900,
        "max_object_bytes": 10,
        "node_quota_cap_bytes": 10,
        "challenge_deadline_seconds": 60,
    }
    response: dict[str, object] = {
        "server_time": 1,
        "window_index": 1,
        "status": "active",
        "limits": limits,
        "assignments": [],
        "revocations": [],
        "next_poll_seconds": 30,
    }
    at_limit = [_challenge()] * MAX_CHALLENGES_PER_POLL
    PollResponse.model_validate({**response, "challenges": at_limit})
    with pytest.raises(ValidationError):
        PollResponse.model_validate(
            {**response, "challenges": [*at_limit, _challenge()]}
        )


def test_answers_carry_one_refusal_vocabulary() -> None:
    ChallengeAnswer(outcome="refused", refusal="revoked")
    with pytest.raises(ValidationError):
        ChallengeAnswer(outcome="refused", refusal="maybe")  # type: ignore[arg-type]


def test_models_are_frozen() -> None:
    answer = ChallengeAnswer(outcome="refused", refusal="busy")
    with pytest.raises(ValidationError):
        answer.refusal = "revoked"  # type: ignore[misc]


def test_status_always_says_no_payouts() -> None:
    status = NodeStatusResponse(
        node_id="edn_" + "0" * 32,
        status="active",
        mode="shadow",
        held_objects=0,
        held_bytes=0,
    )
    assert status.payouts == "none"
