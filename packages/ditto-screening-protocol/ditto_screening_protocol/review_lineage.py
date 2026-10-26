"""Lineage-scoped source review: a resubmission reviewed against its history.

When a miner resubmits a new version of an agent, Platform names the prior
version (same hotkey and name, highest lower version) as the attempt's lineage
parent. The worker fetches one bounded packet for the attempt: the source diff
from the parent, every prior ruling on the agent's versions, and the parent's
automated findings. The model review always runs; the packet scopes it.

``scope`` is ``diff`` when the review may focus on the changed files plus the
open leads in the history, and ``full`` when Platform found a reason the
parent's review cannot carry (``full_review_reasons``). Under either scope the
reviewer can read any file, and unchanged code inherits only what the history
settles. A cleared parent is context, never an automatic pass.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

REVIEW_LINEAGE_PACKET_VERSION = 1

# Bounds the packet so one long lineage can never crowd the review context.
MAX_LINEAGE_HISTORY = 12
MAX_LINEAGE_RULINGS = 24
MAX_LINEAGE_FINDINGS = 12
MAX_LINEAGE_CHANGES = 64
MAX_LINEAGE_DIFF_LINES = 6000
MAX_LINEAGE_TEXT = 4000


class ReviewLineageScope(StrEnum):
    DIFF = "diff"
    FULL = "full"


class ReviewLineageFullReviewReason(StrEnum):
    """Why the parent's review cannot scope this attempt to its diff."""

    BUILD_SURFACE_CHANGED = "build-surface-changed"
    DIFF_TOO_LARGE = "diff-too-large"
    TOO_MANY_FILES_CHANGED = "too-many-files-changed"
    OPAQUE_FILE_CHANGED = "opaque-file-changed"
    SOURCE_NOT_COMPARED = "source-not-compared"
    PARENT_POLICY_DIFFERS = "parent-policy-differs"
    NO_REVIEW_HISTORY = "no-review-history"


class ReviewLineageAgent(BaseModel):
    """One prior version in the lineage, newest first."""

    model_config = ConfigDict(frozen=True)

    agent_id: UUID
    version: Annotated[int, Field(ge=1)]
    artifact_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    status: Annotated[str, Field(min_length=1, max_length=64)]
    screening_policy_version: Annotated[int | None, Field(ge=1)] = None
    submitted_at: datetime


class ReviewLineageRuling(BaseModel):
    """An operator or court decision on one prior version."""

    model_config = ConfigDict(frozen=True)

    agent_id: UUID
    version: Annotated[int, Field(ge=1)]
    court: Literal["quarantine", "ath", "dispute"]
    resolution: Annotated[str, Field(min_length=1, max_length=64)]
    reason: str
    evidence_references: Annotated[list[str], Field(max_length=64)] = []
    policy_version: Annotated[int | None, Field(ge=1)] = None
    decided_at: datetime


class ReviewLineageFinding(BaseModel):
    """One automated review outcome on the parent (L1/L2/L3 or the court)."""

    model_config = ConfigDict(frozen=True)

    agent_id: UUID
    version: Annotated[int, Field(ge=1)]
    attempt_id: UUID | None = None
    policy_version: Annotated[int | None, Field(ge=1)] = None
    reviewer: Annotated[str | None, Field(max_length=128)] = None
    outcome: Annotated[str, Field(min_length=1, max_length=64)]
    reason_code: Annotated[str | None, Field(max_length=128)] = None
    # Operator reason text stays unbounded on the wire (Platform clips it to
    # MAX_LINEAGE_TEXT when building the packet).
    reason: str = ""
    finding: Annotated[str, Field(max_length=MAX_LINEAGE_TEXT)] = ""
    recorded_at: datetime


class ReviewLineageFileChange(BaseModel):
    """One changed path between the parent and this artifact."""

    model_config = ConfigDict(frozen=True)

    path: Annotated[str, Field(min_length=1, max_length=512)]
    change: Literal["added", "removed", "modified", "renamed"]
    from_path: Annotated[str | None, Field(max_length=512)] = None
    added_lines: Annotated[int, Field(ge=0)]
    removed_lines: Annotated[int, Field(ge=0)]
    diff_lines: Annotated[list[str], Field(max_length=MAX_LINEAGE_DIFF_LINES)] = []
    truncated: bool = False


class ReviewLineagePacketBody(BaseModel):
    """Every packet field except its digest.

    Unknown fields are ignored, as on every wire model in this repository
    (additive JSON compatibility across rolling upgrades). A newer Platform
    that adds a field seals its digest over that field, so an older worker's
    re-computed digest will not match and it falls back to a full review.
    """

    model_config = ConfigDict(frozen=True)

    packet_version: Literal[1] = REVIEW_LINEAGE_PACKET_VERSION
    agent_id: UUID
    attempt_id: UUID
    artifact_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    policy_version: Annotated[int, Field(ge=1)]
    parent: ReviewLineageAgent
    history: Annotated[list[ReviewLineageAgent], Field(max_length=MAX_LINEAGE_HISTORY)]
    scope: ReviewLineageScope
    full_review_reasons: list[ReviewLineageFullReviewReason] = []
    changes: Annotated[
        list[ReviewLineageFileChange], Field(max_length=MAX_LINEAGE_CHANGES)
    ] = []
    changed_file_count: Annotated[int, Field(ge=0)] = 0
    unchanged_file_count: Annotated[int, Field(ge=0)] = 0
    changed_line_count: Annotated[int, Field(ge=0)] = 0
    rulings: Annotated[
        list[ReviewLineageRuling], Field(max_length=MAX_LINEAGE_RULINGS)
    ] = []
    findings: Annotated[
        list[ReviewLineageFinding], Field(max_length=MAX_LINEAGE_FINDINGS)
    ] = []

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        if (self.scope == ReviewLineageScope.FULL) != bool(self.full_review_reasons):
            raise ValueError("a full lineage scope must name its reasons, and only it")
        if self.parent.agent_id == self.agent_id:
            raise ValueError("a submission cannot be its own lineage parent")
        if not self.history or self.history[0].agent_id != self.parent.agent_id:
            raise ValueError("lineage history must start at the parent")
        if (
            self.scope == ReviewLineageScope.DIFF
            and not self.rulings
            and not self.findings
        ):
            # Platform names NO_REVIEW_HISTORY for this; a diff scope with
            # nothing to carry forward is not a packet it would seal.
            raise ValueError("a diff lineage scope requires rulings or findings")
        return self


class ReviewLineagePacket(ReviewLineagePacketBody):
    """Exact lineage packet bound to one screening attempt."""

    packet_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

    @model_validator(mode="after")
    def validate_digest(self) -> Self:
        if self.packet_sha256 != review_lineage_packet_digest(self):
            raise ValueError("lineage packet digest mismatch")
        return self


def _canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def review_lineage_packet_digest(packet: ReviewLineagePacketBody) -> str:
    """SHA-256 over the canonical packet JSON, excluding the digest field."""
    body = packet.model_dump(mode="json", exclude={"packet_sha256"})
    return hashlib.sha256(_canonical(body)).hexdigest()


def seal_review_lineage_packet(body: dict[str, Any]) -> ReviewLineagePacket:
    """Validate a packet body and attach its digest."""
    validated = ReviewLineagePacketBody.model_validate(body)
    return ReviewLineagePacket.model_validate(
        {
            **validated.model_dump(mode="json"),
            "packet_sha256": review_lineage_packet_digest(validated),
        }
    )


__all__ = [
    "MAX_LINEAGE_CHANGES",
    "MAX_LINEAGE_DIFF_LINES",
    "MAX_LINEAGE_FINDINGS",
    "MAX_LINEAGE_HISTORY",
    "MAX_LINEAGE_RULINGS",
    "MAX_LINEAGE_TEXT",
    "REVIEW_LINEAGE_PACKET_VERSION",
    "ReviewLineageAgent",
    "ReviewLineageFileChange",
    "ReviewLineageFinding",
    "ReviewLineageFullReviewReason",
    "ReviewLineagePacket",
    "ReviewLineagePacketBody",
    "ReviewLineageRuling",
    "ReviewLineageScope",
    "review_lineage_packet_digest",
    "seal_review_lineage_packet",
]
