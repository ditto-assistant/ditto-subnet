from __future__ import annotations

import base64
import hashlib
from random import Random

import pytest

from ditto_edge_protocol.challenges import (
    DrawnChallenge,
    Held,
    draw_challenge,
    judge_answer,
    schedule_window,
)
from ditto_edge_protocol.limits import MAX_POSSESSION_RANGE, MAX_RETRIEVAL_RANGE
from ditto_edge_protocol.proofs import (
    ProofError,
    check_range,
    digest_of,
    possession_proof,
    retrieval_slice,
)
from ditto_edge_protocol.wire import ChallengeAnswer

DATA = bytes(range(256)) * 1200  # 307,200 bytes
NONCE = "11" * 32


def _nonce(size: int) -> bytes:
    return b"\x11" * size


def _possession(offset: int = 100, length: int = 4096) -> DrawnChallenge:
    return DrawnChallenge(
        purpose="possession",
        kind="possession",
        digest=digest_of(DATA),
        offset=offset,
        length=length,
        nonce_hex=NONCE,
    )


def _retrieval(offset: int = 10, length: int = 1000) -> DrawnChallenge:
    return DrawnChallenge(
        purpose="retrieval",
        kind="retrieval",
        digest=digest_of(DATA),
        offset=offset,
        length=length,
        nonce_hex=None,
    )


def _judge(
    challenge: DrawnChallenge, answer: ChallengeAnswer, **overrides: float | None
) -> str:
    args: dict[str, float | None] = {
        "issued_at": 100.0,
        "answered_at": 110.0,
        "deadline_at": 160.0,
        "last_filled_at": 50.0,
    }
    args.update(overrides)
    return judge_answer(challenge=challenge, data=DATA, answer=answer, **args)  # type: ignore[arg-type]


def test_possession_proof_is_nonce_keyed_hash_of_the_range() -> None:
    expected = hashlib.sha256(bytes.fromhex(NONCE) + DATA[5:15]).hexdigest()
    assert possession_proof(nonce_hex=NONCE, data=DATA, offset=5, length=10) == expected
    other = possession_proof(nonce_hex="22" * 32, data=DATA, offset=5, length=10)
    assert other != expected


@pytest.mark.parametrize(
    ("offset", "length", "kind"),
    [
        (-1, 10, "possession"),
        (0, 0, "possession"),
        (len(DATA) - 5, 10, "possession"),
        (0, MAX_POSSESSION_RANGE + 1, "possession"),
        (0, MAX_RETRIEVAL_RANGE + 1, "retrieval"),
    ],
)
def test_ranges_must_lie_inside_the_object(offset: int, length: int, kind: str) -> None:
    with pytest.raises(ProofError):
        check_range(size=len(DATA), offset=offset, length=length, kind=kind)


def test_proof_rejects_a_malformed_nonce() -> None:
    with pytest.raises(ProofError):
        possession_proof(nonce_hex="zz" * 32, data=DATA, offset=0, length=1)


def test_schedule_covers_the_whole_window_sorted() -> None:
    slots = schedule_window(
        window_start=1000.0,
        window_seconds=3600,
        slots=200,
        retrieval_share=0.25,
        revoked_share=0.1,
        rng=Random(7),
    )
    assert len(slots) == 200
    assert [s.due_at for s in slots] == sorted(s.due_at for s in slots)
    assert all(1000.0 <= s.due_at < 4600.0 for s in slots)
    purposes = {s.purpose for s in slots}
    assert purposes == {"possession", "retrieval", "revoked"}


def test_schedule_is_empty_for_zero_slots() -> None:
    assert (
        schedule_window(
            window_start=0,
            window_seconds=3600,
            slots=0,
            retrieval_share=0.2,
            revoked_share=0.1,
            rng=Random(1),
        )
        == []
    )


def test_draw_possession_and_retrieval_stay_in_bounds() -> None:
    held = [Held(digest_of(DATA), len(DATA)), Held("ab" * 32, 10)]
    rng = Random(3)
    for _ in range(200):
        for purpose in ("possession", "retrieval"):
            drawn = draw_challenge(
                purpose=purpose, held=held, revoked=[], rng=rng, nonce=_nonce
            )
            assert drawn is not None
            size = next(h.size_bytes for h in held if h.digest == drawn.digest)
            check_range(
                size=size, offset=drawn.offset, length=drawn.length, kind=drawn.kind
            )
            assert (drawn.nonce_hex is None) == (drawn.kind == "retrieval")


def test_revoked_slot_targets_revoked_objects_as_plain_possession() -> None:
    revoked = [Held("cd" * 32, 5000)]
    drawn = draw_challenge(
        purpose="revoked",
        held=[Held("ab" * 32, 10)],
        revoked=revoked,
        rng=Random(1),
        nonce=_nonce,
    )
    assert drawn is not None
    assert drawn.purpose == "revoked"
    assert drawn.kind == "possession"
    assert drawn.digest == "cd" * 32


def test_revoked_slot_without_revocations_becomes_possession() -> None:
    drawn = draw_challenge(
        purpose="revoked",
        held=[Held("ab" * 32, 10)],
        revoked=[],
        rng=Random(1),
        nonce=_nonce,
    )
    assert drawn is not None and drawn.purpose == "possession"


def test_nothing_held_is_void() -> None:
    assert (
        draw_challenge(
            purpose="possession", held=[], revoked=[], rng=Random(1), nonce=_nonce
        )
        is None
    )


def test_correct_proof_passes() -> None:
    proof = possession_proof(nonce_hex=NONCE, data=DATA, offset=100, length=4096)
    answer = ChallengeAnswer(outcome="proof", proof_hex=proof)
    assert _judge(_possession(), answer) == "passed"


def test_wrong_proof_is_an_integrity_failure() -> None:
    answer = ChallengeAnswer(outcome="proof", proof_hex="00" * 32)
    assert _judge(_possession(), answer) == "failed"


def test_wrong_answer_kind_fails() -> None:
    answer = ChallengeAnswer(
        outcome="bytes", data_b64=base64.b64encode(DATA[100:4196]).decode()
    )
    assert _judge(_possession(), answer) == "failed"


def test_correct_bytes_pass_and_tampered_bytes_fail() -> None:
    good = base64.b64encode(retrieval_slice(data=DATA, offset=10, length=1000)).decode()
    assert (
        _judge(_retrieval(), ChallengeAnswer(outcome="bytes", data_b64=good))
        == "passed"
    )
    bad = bytearray(DATA[10:1010])
    bad[0] ^= 1
    tampered = base64.b64encode(bytes(bad)).decode()
    assert (
        _judge(_retrieval(), ChallengeAnswer(outcome="bytes", data_b64=tampered))
        == "failed"
    )
    assert (
        _judge(_retrieval(), ChallengeAnswer(outcome="bytes", data_b64="!!notbase64"))
        == "failed"
    )


def test_late_answer_is_missed() -> None:
    proof = possession_proof(nonce_hex=NONCE, data=DATA, offset=100, length=4096)
    answer = ChallengeAnswer(outcome="proof", proof_hex=proof)
    assert _judge(_possession(), answer, answered_at=161.0) == "missed"


def test_answer_after_a_post_challenge_download_is_refilled() -> None:
    proof = possession_proof(nonce_hex=NONCE, data=DATA, offset=100, length=4096)
    answer = ChallengeAnswer(outcome="proof", proof_hex=proof)
    assert _judge(_possession(), answer, last_filled_at=105.0) == "refilled"
    assert _judge(_possession(), answer, last_filled_at=None) == "passed"


def test_refusing_a_held_object_is_refused() -> None:
    answer = ChallengeAnswer(outcome="refused", refusal="not_held")
    assert _judge(_possession(), answer) == "refused"


def test_revoked_objects_must_be_refused() -> None:
    revoked = DrawnChallenge(
        purpose="revoked",
        kind="possession",
        digest=digest_of(DATA),
        offset=0,
        length=64,
        nonce_hex=NONCE,
    )
    refused = ChallengeAnswer(outcome="refused", refusal="revoked")
    assert _judge(revoked, refused) == "refused_ok"
    proof = possession_proof(nonce_hex=NONCE, data=DATA, offset=0, length=64)
    assert (
        _judge(revoked, ChallengeAnswer(outcome="proof", proof_hex=proof))
        == "violation"
    )
    assert (
        _judge(revoked, ChallengeAnswer(outcome="proof", proof_hex="00" * 32))
        == "violation"
    )


def test_judging_a_nonceless_possession_challenge_is_a_verifier_bug() -> None:
    broken = DrawnChallenge(
        purpose="possession",
        kind="possession",
        digest=digest_of(DATA),
        offset=0,
        length=64,
        nonce_hex=None,
    )
    with pytest.raises(ValueError, match="nonce"):
        _judge(broken, ChallengeAnswer(outcome="proof", proof_hex="00" * 32))
