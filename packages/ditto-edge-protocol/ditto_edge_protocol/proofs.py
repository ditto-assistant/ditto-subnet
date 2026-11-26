"""Content addressing and challenge proofs.

Everything here is checked against bytes the verifier holds, never against
anything a node says about itself. A possession proof is keyed by a fresh
32-byte nonce, so a node cannot precompute answers; it must read the range at
answer time. Platform logs every fill, so a node that holds nothing and
re-downloads after a challenge is caught by timestamp, not by this module.
"""

from __future__ import annotations

import hashlib
import re

from ditto_edge_protocol.limits import (
    CHALLENGE_NONCE_BYTES,
    MAX_POSSESSION_RANGE,
    MAX_RETRIEVAL_RANGE,
)

_HEX_NONCE = re.compile(rf"[0-9a-f]{{{CHALLENGE_NONCE_BYTES * 2}}}\Z")


class ProofError(ValueError):
    """A challenge range or nonce is malformed."""


def digest_of(data: bytes) -> str:
    """The object's name on the wire: lowercase hex SHA-256 of its bytes."""
    return hashlib.sha256(data).hexdigest()


def check_range(*, size: int, offset: int, length: int, kind: str) -> None:
    """Raise :class:`ProofError` unless the range lies inside the object."""
    limit = MAX_POSSESSION_RANGE if kind == "possession" else MAX_RETRIEVAL_RANGE
    if size < 1:
        raise ProofError("object is empty")
    if not 1 <= length <= limit:
        raise ProofError(f"{kind} range length must be 1..{limit}")
    if offset < 0 or offset + length > size:
        raise ProofError("range falls outside the object")


def possession_proof(*, nonce_hex: str, data: bytes, offset: int, length: int) -> str:
    """``sha256(nonce || data[offset:offset+length])`` as lowercase hex."""
    if _HEX_NONCE.fullmatch(nonce_hex) is None:
        raise ProofError(
            f"challenge nonce must be {CHALLENGE_NONCE_BYTES} bytes of lowercase hex"
        )
    check_range(size=len(data), offset=offset, length=length, kind="possession")
    hasher = hashlib.sha256(bytes.fromhex(nonce_hex))
    hasher.update(data[offset : offset + length])
    return hasher.hexdigest()


def retrieval_slice(*, data: bytes, offset: int, length: int) -> bytes:
    """The exact bytes a retrieval challenge asks for."""
    check_range(size=len(data), offset=offset, length=length, kind="retrieval")
    return data[offset : offset + length]
