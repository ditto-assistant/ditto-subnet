"""Challenge scheduling, drawing and judging.

Each node gets a fixed number of challenge slots per ledger window, due at
secret random times drawn when its first poll of the window arrives. A slot
that comes due while the node is away is *missed*. Challenges are therefore
not issued only when the node chooses to show up, and availability measures
time actually online, not polling frequency.

A slot's purpose is drawn with it:

- ``possession``: hash a random range of a held object under a fresh nonce.
- ``retrieval``: return a random range of a held object verbatim.
- ``revoked``: a possession challenge for an object this node was told to
  revoke. The only correct answer is a refusal; a proof means the node still
  holds revoked content.

On the wire both possession and revoked slots are ``kind="possession"``.

The randomness source is injected: Platform passes an unpredictable one
(``random.SystemRandom`` plus ``secrets``), tests pass a seeded one.
"""

from __future__ import annotations

import base64
import binascii
import hmac
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from random import Random
from typing import Literal

from ditto_edge_protocol.limits import (
    CHALLENGE_NONCE_BYTES,
    MAX_POSSESSION_RANGE,
    MAX_RETRIEVAL_RANGE,
)
from ditto_edge_protocol.proofs import possession_proof, retrieval_slice
from ditto_edge_protocol.wire import ChallengeAnswer

Purpose = Literal["possession", "retrieval", "revoked"]

Outcome = Literal[
    "pending",
    "passed",
    "failed",
    "missed",
    "refilled",
    "refused",
    "refused_ok",
    "violation",
    "void",
]
"""How one slot ended.

* ``passed``: correct answer before the deadline, from bytes the node already
  held when the challenge was issued.
* ``failed``: a wrong proof or wrong bytes, an integrity failure.
* ``missed``: no answer by the deadline, or the slot came due while away.
* ``refilled``: correct, but only after re-downloading the object once the
  challenge was out; the node held nothing.
* ``refused``: the node declined a challenge for an object it should hold.
* ``refused_ok``: the node correctly refused a revoked object.
* ``violation``: the node answered for an object it was told to revoke.
* ``void``: nothing to challenge (the node held no objects); not counted.
* ``pending``: issued and still inside its deadline.
"""


@dataclass(frozen=True)
class Slot:
    due_at: float
    purpose: Purpose


def schedule_window(
    *,
    window_start: float,
    window_seconds: int,
    slots: int,
    retrieval_share: float,
    revoked_share: float,
    rng: Random,
) -> list[Slot]:
    """Draw one window's slots, sorted by due time.

    Due times are uniform over the whole window, including any part already
    past when the schedule is drawn. A node that first appears mid-window has
    already missed the slots before it arrived.
    """
    if slots <= 0 or window_seconds <= 0:
        return []
    drawn: list[Slot] = []
    for _ in range(slots):
        due = window_start + rng.random() * window_seconds
        roll = rng.random()
        purpose: Purpose
        if roll < revoked_share:
            purpose = "revoked"
        elif roll < revoked_share + retrieval_share:
            purpose = "retrieval"
        else:
            purpose = "possession"
        drawn.append(Slot(due_at=due, purpose=purpose))
    drawn.sort(key=lambda slot: slot.due_at)
    return drawn


@dataclass(frozen=True)
class Held:
    digest: str
    size_bytes: int


@dataclass(frozen=True)
class DrawnChallenge:
    purpose: Purpose
    kind: Literal["possession", "retrieval"]
    digest: str
    offset: int
    length: int
    nonce_hex: str | None


def draw_challenge(
    *,
    purpose: Purpose,
    held: Sequence[Held],
    revoked: Sequence[Held],
    rng: Random,
    nonce: Callable[[int], bytes],
) -> DrawnChallenge | None:
    """Pick the object and range one due slot will ask about.

    A ``revoked`` slot with nothing revoked to ask about becomes a possession
    slot. Returns ``None`` when the node holds nothing to challenge, which the
    caller records as ``void``.
    """
    if purpose == "revoked" and revoked:
        target = revoked[rng.randrange(len(revoked))]
        offset, length = _range(target.size_bytes, MAX_POSSESSION_RANGE, rng)
        return DrawnChallenge(
            purpose="revoked",
            kind="possession",
            digest=target.digest,
            offset=offset,
            length=length,
            nonce_hex=nonce(CHALLENGE_NONCE_BYTES).hex(),
        )
    if not held:
        return None
    target = held[rng.randrange(len(held))]
    if purpose == "retrieval":
        offset, length = _range(target.size_bytes, MAX_RETRIEVAL_RANGE, rng)
        return DrawnChallenge(
            purpose="retrieval",
            kind="retrieval",
            digest=target.digest,
            offset=offset,
            length=length,
            nonce_hex=None,
        )
    offset, length = _range(target.size_bytes, MAX_POSSESSION_RANGE, rng)
    return DrawnChallenge(
        purpose="possession",
        kind="possession",
        digest=target.digest,
        offset=offset,
        length=length,
        nonce_hex=nonce(CHALLENGE_NONCE_BYTES).hex(),
    )


def _range(size: int, limit: int, rng: Random) -> tuple[int, int]:
    length = min(size, limit)
    offset = rng.randrange(size - length + 1)
    return offset, length


def judge_answer(
    *,
    challenge: DrawnChallenge,
    data: bytes,
    answer: ChallengeAnswer,
    issued_at: float,
    answered_at: float,
    deadline_at: float,
    last_filled_at: float | None,
) -> Outcome:
    """Decide one answer against the verifier's own copy of the object.

    ``data`` is the canonical object; ``last_filled_at`` is when this node last
    downloaded it. A correct answer that needed a download after the
    challenge went out is ``refilled``, not ``passed``.
    """
    if answered_at > deadline_at:
        return "missed"
    if challenge.purpose == "revoked":
        return "refused_ok" if answer.outcome == "refused" else "violation"
    if answer.outcome == "refused":
        return "refused"
    correct = False
    if challenge.kind == "possession" and answer.outcome == "proof":
        if challenge.nonce_hex is None:
            raise ValueError("a possession challenge always carries a nonce")
        expected = possession_proof(
            nonce_hex=challenge.nonce_hex,
            data=data,
            offset=challenge.offset,
            length=challenge.length,
        )
        correct = answer.proof_hex is not None and hmac.compare_digest(
            answer.proof_hex, expected
        )
    elif challenge.kind == "retrieval" and answer.outcome == "bytes":
        expected_bytes = retrieval_slice(
            data=data, offset=challenge.offset, length=challenge.length
        )
        try:
            returned = base64.b64decode(answer.data_b64 or "", validate=True)
        except (binascii.Error, ValueError):
            returned = b""
        correct = hmac.compare_digest(returned, expected_bytes)
    if not correct:
        return "failed"
    if last_filled_at is not None and last_filled_at > issued_at:
        return "refilled"
    return "passed"
