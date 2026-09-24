"""Request-bound identity for a default-off independent V13 replay process."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from pathlib import Path
from secrets import token_hex
from typing import Literal

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ditto_screening_protocol.v13_replay_process_identity import (
    ReplayPurpose,
    V13ReplayProcessProof,
)


class ReplayProcessIdentity:
    """Sign one HTTP request with a dedicated process key, never a node token."""

    def __init__(self, *, node_id: str, instance_id: str, key_file: Path) -> None:
        descriptor = os.open(key_file, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
                raise ValueError("replay process key must be a private regular file")
            if metadata.st_uid != os.getuid():
                raise ValueError("replay process key owner differs from runner")
            key_bytes = stream.read(33)
        if len(key_bytes) != 32:
            raise ValueError("replay process key must contain a raw Ed25519 seed")
        self._key = Ed25519PrivateKey.from_private_bytes(key_bytes)
        self._node_id = node_id
        self._instance_id = instance_id
        public_key = self._key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        self.public_key_hex = public_key.hex()
        self.key_sha256 = hashlib.sha256(public_key).hexdigest()

    def headers(
        self,
        *,
        purpose: ReplayPurpose,
        path: str,
        body: bytes = b"",
        method: Literal["GET", "POST"] = "POST",
        now: int | None = None,
    ) -> dict[str, str]:
        proof = V13ReplayProcessProof(
            purpose=purpose,
            node_id=self._node_id,
            instance_id=self._instance_id,
            method=method,
            path=path,
            body_sha256=hashlib.sha256(body).hexdigest(),
            issued_at=int(time.time()) if now is None else now,
            nonce=token_hex(16),
        )
        return {
            "X-Replay-Process-Instance": self._instance_id,
            "X-Replay-Process-Proof": json.dumps(
                proof.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ),
            "X-Replay-Process-Signature": self._key.sign(proof.signing_bytes()).hex(),
        }
