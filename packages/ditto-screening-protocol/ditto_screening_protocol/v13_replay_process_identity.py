"""Domain-separated bytes for an independently authenticated V13 replay process.

This contract does not grant replay authority. Platform must pin a public key
to one enrolled node and worker instance, verify the signature and a fresh
heartbeat from that same key, and consume the nonce before accepting a claim.
"""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ReplayPurpose = Literal[
    "heartbeat", "claim", "renew", "inputs", "build-upload", "build-verify",
    "receipts", "finish",
]
_PATHS = {
    "heartbeat": "/screener/heartbeat",
    "claim": "/screener/verification-replays/claim",
}
_NODE = re.compile(r"subnet-screener-[1-9][0-9]*\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_NONCE = re.compile(r"[0-9a-f]{32}\Z")
_LEASE_PATH = re.compile(
    r"/screener/verification-replays/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}/"
    r"(renew|inputs|build-upload|build-verify|receipts|finish)\Z"
)


class V13ReplayProcessProof(BaseModel):
    """The signed claim/heartbeat envelope; signature is transported separately."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    purpose: ReplayPurpose
    node_id: str = Field(min_length=1, max_length=63)
    instance_id: str = Field(min_length=1, max_length=63)
    method: Literal["POST", "GET"] = "POST"
    path: str
    body_sha256: str
    issued_at: int = Field(gt=0)
    nonce: str

    @field_validator("node_id")
    @classmethod
    def valid_node(cls, value: str) -> str:
        if _NODE.fullmatch(value) is None:
            raise ValueError("invalid enrolled node ID")
        return value

    @field_validator("body_sha256")
    @classmethod
    def valid_digest(cls, value: str) -> str:
        if _DIGEST.fullmatch(value) is None:
            raise ValueError("body SHA-256 must be lowercase hex")
        return value

    @field_validator("nonce")
    @classmethod
    def valid_nonce(cls, value: str) -> str:
        if _NONCE.fullmatch(value) is None:
            raise ValueError("nonce must be 16 random bytes in lowercase hex")
        return value

    @model_validator(mode="after")
    def exact_scope(self) -> V13ReplayProcessProof:
        instance_pattern = rf"{re.escape(self.node_id)}-worker-[1-9][0-9]*"
        if re.fullmatch(instance_pattern, self.instance_id) is None:
            raise ValueError("worker instance does not belong to enrolled node")
        if self.purpose in _PATHS:
            path_matches = self.path == _PATHS[self.purpose]
        else:
            match = _LEASE_PATH.fullmatch(self.path)
            path_matches = match is not None and match.group(1) == self.purpose
        if not path_matches or (self.method == "GET") != (self.purpose == "inputs"):
            raise ValueError("replay proof path does not match purpose")
        return self

    def signing_bytes(self) -> bytes:
        """Stable bytes shared by worker and Platform; no signature in payload."""

        payload = self.model_dump(mode="json")
        return b"ditto-v13-replay-process-proof:v1\n" + json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")

    def is_fresh(self, *, now: int, max_age_seconds: int = 30) -> bool:
        """Bound signed proof age; Platform must also consume the nonce once."""

        return (
            max_age_seconds > 0 and now - max_age_seconds <= self.issued_at <= now + 5
        )

    def matches_request(
        self,
        *,
        purpose: ReplayPurpose,
        node_id: str,
        instance_id: str,
        body_sha256: str,
        method: str = "POST",
        path: str | None = None,
    ) -> bool:
        """Compare against authenticated identity and the received body hash."""

        return (
            self.purpose == purpose
            and self.node_id == node_id
            and self.instance_id == instance_id
            and self.body_sha256 == body_sha256
            and self.method == method
            and (path is None or self.path == path)
        )
