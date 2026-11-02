"""Default-off verifier for an independently authenticated V13 replay process.

Endpoints must supply an operator-pinned registration, a persisted *verified*
heartbeat, and an atomic durable nonce consumer. The node bearer is not a
process credential. No live route calls this module until those exist.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ditto_screening_protocol.v13_replay_process_identity import (
    ReplayPurpose,
    V13ReplayProcessProof,
)

_HEX_SIGNATURE = re.compile(r"[0-9a-f]{128}\Z")
_HEX_KEY = re.compile(r"[0-9a-f]{64}\Z")
_HEARTBEAT_MAX_AGE_SECONDS = 300


class ReplayProcessProofError(ValueError):
    """A process proof is insufficient to admit report-only replay work."""


@dataclass(frozen=True)
class ReplayProcessRegistration:
    """Public key pinned by a guarded operator action, never a node bearer."""

    node_id: str
    instance_id: str
    public_key_hex: str
    revoked: bool = False

    @property
    def key_sha256(self) -> str:
        if _HEX_KEY.fullmatch(self.public_key_hex) is None:
            raise ReplayProcessProofError("invalid registered replay process key")
        return hashlib.sha256(bytes.fromhex(self.public_key_hex)).hexdigest()


@dataclass(frozen=True)
class VerifiedReplayProcessHeartbeat:
    """A persisted heartbeat whose process signature was already verified."""

    node_id: str
    instance_id: str
    key_sha256: str
    seen_at: int
    policy_version: int
    release: tuple[int, int, int]


NonceConsumer = Callable[[str, str, str], Awaitable[bool]]


async def verify_replay_process_proof(
    *,
    proof: V13ReplayProcessProof,
    signature_hex: str,
    registration: ReplayProcessRegistration,
    authenticated_node_id: str,
    purpose: ReplayPurpose,
    body: bytes,
    method: str = "POST",
    path: str | None = None,
    now: int,
    consume_nonce: NonceConsumer,
    heartbeat: VerifiedReplayProcessHeartbeat | None = None,
    minimum_release: tuple[int, int, int] | None = None,
) -> None:
    """Verify exact request/process and consume a nonce once, fail closed.

    ``consume_nonce`` must atomically insert a unique
    ``(node_id, instance_id, nonce)`` row in durable storage and return false
    on conflict. Do not replace it with a read-then-write or process-local set.
    """

    if registration.revoked:
        raise ReplayProcessProofError("replay process key revoked")
    if (
        registration.node_id != authenticated_node_id
        or registration.instance_id != proof.instance_id
        or not proof.matches_request(
            purpose=purpose,
            node_id=authenticated_node_id,
            instance_id=registration.instance_id,
            body_sha256=hashlib.sha256(body).hexdigest(),
            method=method,
            path=path,
        )
    ):
        raise ReplayProcessProofError("replay process request binding mismatch")
    if not proof.is_fresh(now=now):
        raise ReplayProcessProofError("replay process proof outside time window")
    if _HEX_SIGNATURE.fullmatch(signature_hex) is None:
        raise ReplayProcessProofError("invalid replay process signature")
    try:
        public_key = Ed25519PublicKey.from_public_bytes(
            bytes.fromhex(registration.public_key_hex)
        )
        public_key.verify(bytes.fromhex(signature_hex), proof.signing_bytes())
    except (InvalidSignature, ValueError) as exc:
        raise ReplayProcessProofError("invalid replay process signature") from exc

    if purpose == "claim":
        if minimum_release is None:
            raise ReplayProcessProofError("replay process runner unavailable")
        if heartbeat is None:
            raise ReplayProcessProofError("exact replay process heartbeat unavailable")
        if (
            heartbeat.node_id != registration.node_id
            or heartbeat.instance_id != registration.instance_id
            or heartbeat.key_sha256 != registration.key_sha256
            or heartbeat.policy_version != 13
            or heartbeat.release < minimum_release
            or not now - _HEARTBEAT_MAX_AGE_SECONDS <= heartbeat.seen_at <= now + 5
        ):
            raise ReplayProcessProofError("exact replay process heartbeat unavailable")

    if not await consume_nonce(proof.node_id, proof.instance_id, proof.nonce):
        raise ReplayProcessProofError("replayed process proof nonce")
