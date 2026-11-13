"""edge/v1 wire models shared by Platform, the reference node, and Desktop.

Every model ignores unknown fields so either side can add one without breaking
the other. Requests are bounded here; Platform still re-checks anything that
depends on state (assignment ownership, lane mode, quotas).

What a node is told and what it never sees is part of the contract: no model
here carries a URL, share id, title, owner, or account. A node learns only a
digest, a size, a media type and a lease.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from ditto_edge_protocol.limits import (
    MAX_ACK_ITEMS,
    MAX_ASSIGNMENTS_PER_POLL,
    MAX_CHALLENGES_PER_POLL,
    MAX_OBJECT_BYTES_CEILING,
    MAX_POSSESSION_RANGE,
    MAX_RETRIEVAL_RANGE,
    MEDIA_TYPES,
)

Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
"""Lowercase hex SHA-256 of an object's exact bytes."""

NodeId = Annotated[str, StringConstraints(pattern=r"^edn_[0-9a-f]{32}$")]
"""``edn_`` + the first 32 hex characters of SHA-256(device public key)."""

PublicKeyHex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
"""Raw 32-byte Ed25519 public key, lowercase hex."""

ChallengeId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]

LaneMode = Literal["off", "shadow"]
"""There is deliberately no ``enforce``: leaving shadow needs a new revision of
this contract and Peyton's approval, not a settings write."""

NodeStatus = Literal["active", "suspended", "retired"]


class _Wire(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class ClientInfo(_Wire):
    """Which node implementation is talking. A label, never a scoring input."""

    name: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    version: Annotated[str, StringConstraints(min_length=1, max_length=32)]


class NodePlatform(_Wire):
    """What the node says about its machine.

    Self-reported, so it is shown to operators and never scored: a root-owned
    box can report anything about itself.
    """

    os: Annotated[str, StringConstraints(max_length=32)] = ""
    arch: Annotated[str, StringConstraints(max_length=16)] = ""
    model: Annotated[str, StringConstraints(max_length=64)] = ""
    memory_gb: Annotated[int, Field(ge=0, le=4096)] | None = None


class HotkeyBinding(_Wire):
    """An SN118 hotkey vouching for this device key.

    The hotkey signs :func:`ditto_edge_protocol.signing.binding_message`. In
    shadow it only labels the node so the ledger can show what a hotkey would
    earn; one hotkey may bind many devices (a pool).
    """

    hotkey_ss58: Annotated[str, StringConstraints(min_length=40, max_length=64)]
    issued_at: Annotated[int, Field(ge=0)]
    signature_hex: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{128}$")]


class EnrollRequest(_Wire):
    protocol: Literal["edge/v1"]
    public_key_hex: PublicKeyHex
    quota_bytes: Annotated[int, Field(ge=0)]
    client: ClientInfo
    platform: NodePlatform = NodePlatform()
    hotkey_binding: HotkeyBinding | None = None


class LaneLimits(_Wire):
    """The slice of lane settings a node needs to behave."""

    mode: LaneMode
    poll_interval_seconds: Annotated[int, Field(ge=5, le=3600)]
    lease_seconds: Annotated[int, Field(ge=60, le=86_400)]
    max_object_bytes: Annotated[int, Field(ge=1, le=MAX_OBJECT_BYTES_CEILING)]
    node_quota_cap_bytes: Annotated[int, Field(ge=0)]
    challenge_deadline_seconds: Annotated[int, Field(ge=5, le=3600)]


class EnrollResponse(_Wire):
    node_id: NodeId
    status: NodeStatus
    limits: LaneLimits
    server_time: int


class Assignment(_Wire):
    """One object the node should hold until its lease lapses."""

    digest: Digest
    size_bytes: Annotated[int, Field(ge=1, le=MAX_OBJECT_BYTES_CEILING)]
    media_type: str
    lease_expires_at: int

    @field_validator("media_type")
    @classmethod
    def _known_media_type(cls, value: str) -> str:
        if value not in MEDIA_TYPES:
            raise ValueError(f"media type {value!r} is not allowed")
        return value


class Challenge(_Wire):
    """Prove possession of a byte range, or return it.

    ``possession`` asks for ``sha256(nonce || bytes[offset:offset+length])``;
    ``retrieval`` asks for the exact bytes. A challenge for a digest the node
    was told to revoke is an ordinary possession challenge: the only correct
    answer is a refusal.
    """

    challenge_id: ChallengeId
    kind: Literal["possession", "retrieval"]
    digest: Digest
    nonce_hex: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")] | None = (
        None
    )
    offset: Annotated[int, Field(ge=0)]
    length: Annotated[int, Field(ge=1, le=MAX_RETRIEVAL_RANGE)]
    deadline_at: int

    @model_validator(mode="after")
    def _answerable(self) -> Challenge:
        """A possession challenge needs a nonce and a range a proof may hash."""
        if self.kind == "possession":
            if self.nonce_hex is None:
                raise ValueError("a possession challenge carries a nonce")
            if self.length > MAX_POSSESSION_RANGE:
                raise ValueError(
                    f"a possession range is at most {MAX_POSSESSION_RANGE} bytes"
                )
        return self


class PollRequest(_Wire):
    protocol: Literal["edge/v1"]
    free_bytes: Annotated[int, Field(ge=0)]
    """Quota the node can still fill. Platform also caps by settings."""


class PollResponse(_Wire):
    server_time: int
    window_index: int
    status: NodeStatus
    limits: LaneLimits
    assignments: Annotated[list[Assignment], Field(max_length=MAX_ASSIGNMENTS_PER_POLL)]
    """Every object the node should hold now, each with a renewed lease. An
    object the node holds that is absent here and absent from ``revocations``
    stays until its lease lapses, then is deleted."""
    revocations: Annotated[list[Digest], Field(max_length=MAX_ASSIGNMENTS_PER_POLL)]
    """Delete these now and refuse every later challenge for them."""
    challenges: Annotated[list[Challenge], Field(max_length=MAX_CHALLENGES_PER_POLL)]
    next_poll_seconds: Annotated[int, Field(ge=1)]


class AckRequest(_Wire):
    held: Annotated[list[Digest], Field(max_length=MAX_ACK_ITEMS)] = []
    """Filled and verified locally (SHA-256 matched)."""
    released: Annotated[list[Digest], Field(max_length=MAX_ACK_ITEMS)] = []
    """Deleted: revoked, lease lapsed, or quota trimmed."""


class AckResponse(_Wire):
    accepted: int
    ignored: int


Refusal = Literal["revoked", "not_held", "busy"]


class ChallengeAnswer(_Wire):
    outcome: Literal["proof", "bytes", "refused"]
    proof_hex: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")] | None = (
        None
    )
    data_b64: (
        Annotated[str, StringConstraints(max_length=MAX_RETRIEVAL_RANGE * 2)] | None
    ) = None
    refusal: Refusal | None = None


class AnswerResponse(_Wire):
    """Receipt only. Platform never tells a node whether an answer passed."""

    received: bool


class LeaveRequest(_Wire):
    reason: Annotated[str, StringConstraints(max_length=200)] = ""


class WindowSummary(_Wire):
    window_index: int
    scheduled: int
    passed: int
    missed: int
    failed: int


class NodeStatusResponse(_Wire):
    """What the node's own status screen shows (Desktop, CLI)."""

    node_id: NodeId
    status: NodeStatus
    mode: LaneMode
    held_objects: int
    held_bytes: int
    current_window: WindowSummary | None = None
    payouts: Literal["none"] = "none"
